from typing import Union
import openai
import requests

from chanlun.core.types import BI, ICL, XD
from chanlun.db_models.ai_analyse import TableByAIAnalyse
from chanlun.exchange import get_exchange, Market
from chanlun.cl_utils import query_cl_chart_config, web_batch_get_cl_datas
from chanlun import config, fun
import json
import datetime
from chanlun.persistence.db import db
from chanlun.db_models.base import Base
from chanlun.tools.log_util import LogUtil

# AI 分析记录表是后加的：db.py 的全局 create_all 在该模块尚未被 import 时已经执行完毕，
# 因此 TableByAIAnalyse 不会被注册到那次 create_all 调用里，导致 SQLite 上首次访问报
# "no such table: cl_ai_analyses"。
# 这里在模块首次 import 时按需建表（仅建本表，避免影响其他模型）；
# create_all 自身对已存在的表是 no-op，幂等安全。
try:
    Base.metadata.create_all(db.engine, tables=[TableByAIAnalyse.__table__])
except Exception as _e:
    LogUtil.warning(f"[ai_analyse] ensure table cl_ai_analyses failed: {_e}")


def _zs_dates(zs):
    """ZS 端点日期(R2-F1-1 同款): 核心重做后 ZS.start/end 是 LINE(BI/XD)无 .k,优先从
    zs.lines 取端点 FX 日期(与 golden/_zs_k_index_range 同口径),空 lines 回退旧式 FX 型
    start/end,均不可用返回 (None, None)。"""
    lines = getattr(zs, "lines", None) or []
    if lines:
        return lines[0].start.k.date, lines[-1].end.k.date
    s, e = getattr(zs, "start", None), getattr(zs, "end", None)
    if s is not None and e is not None and hasattr(s, "k") and hasattr(e, "k"):
        return s.k.date, e.k.date
    return None, None


class AIAnalyse:
    """缠论 AI 分析器：将缠论数据（笔/线段/中枢）组装为 Markdown prompt 并调用大模型分析。"""

    def __init__(self, market: str):
        self.market = market
        self.ex = get_exchange(market=Market(self.market))

        self.map_dircetion_type = {"up": "向上", "down": "向下"}
        self.map_zs_type = {"up": "上涨中枢", "down": "下跌中枢", "zd": "震荡中枢"}
        self.map_zss_direction = {
            "up": "上涨趋势",
            "down": "下跌趋势",
            None: "中枢扩展",
        }
        self.map_config_zs_type = {
            "zs_type_bz": "标准中枢",
            "zs_type_dn": "段内中枢",
            "zs_type_fx": "方向中枢",
            "zs_type_fl": "分类中枢",
        }
        self.map_mmd_type = {
            "1buy": "一买",
            "1sell": "一卖",
            "2buy": "二买",
            "2sell": "二卖",
            "3buy": "三买",
            "3sell": "三卖",
            "l2buy": "类二买",
            "l2sell": "类二卖",
            "l3buy": "类三买",
            "l3sell": "类三卖",
        }
        self.map_bc_type = {
            "bi": "笔背驰",
            "xd": "线段背驰",
            "pz": "盘整背驰",
            "qs": "趋势背驰",
        }

    def analyse(self, code: str, frequency: str) -> dict:
        """获取缠论数据，生成 prompt，调用大模型分析，持久化记录并返回结果。"""

        cl_config = query_cl_chart_config(self.market, code)
        stock = self.ex.stock_info(code)
        if stock is None:
            # R14-C2: stock_info 瞬时失败返回 None 时早返回, 否则后续 stock["name"](:100)
            # 抛 TypeError → /ai/analyse 路由裸 500(prompt 内第二次调用即便恢复也救不了外层)。
            return {"ok": False, "msg": f"获取标的信息失败(stock_info 返回空): {code}"}
        klines = self.ex.klines(code, frequency)
        cds = web_batch_get_cl_datas(self.market, code, {frequency: klines}, cl_config)
        cd = cds[0]
        try:
            prompt = self.prompt(cd=cd)
        except Exception as e:
            return {"ok": False, "msg": f"获取缠论当前 Prompt 异常：{e}"}

        analyse_res = self.req_llm_ai_model(prompt)

        if analyse_res["ok"]:
            # 记录数据库
            with db.Session() as session:
                record = TableByAIAnalyse(
                    market=self.market,
                    stock_code=code,
                    stock_name=stock["name"],
                    frequency=frequency,
                    model=analyse_res["model"],
                    msg=analyse_res["msg"],
                    prompt=prompt,
                    dt=datetime.datetime.now(),
                )
                session.add(record)
                session.commit()

        # R17: 回传真实 ok(不硬编码 True), 否则 LLM 失败被吞成"成功"、前端(ai.js:122)误报
        # "分析成功"且丢弃真实错因, DB 又因 :98 判假而未落库 → 用户见"成功"却无新记录。
        return {"ok": analyse_res["ok"], "msg": analyse_res["msg"]}

    def analyse_records(self, page: int = 1, limit: int = 20):
        """返回当前市场的历史 AI 分析记录（按时间倒序分页）。

        :return: (记录列表, 总记录数)
        """
        with db.Session() as session:
            total = (
                session.query(TableByAIAnalyse)
                .filter(TableByAIAnalyse.market == self.market)
                .count()
            )

            offset = (page - 1) * limit
            records = (
                session.query(TableByAIAnalyse)
                .filter(TableByAIAnalyse.market == self.market)
                .order_by(TableByAIAnalyse.dt.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )

        record_dicts = []
        for _r in records:
            # 构造新 dict，不直接 mutate ORM 实例的 __dict__——删
            # _sa_instance_state 会破坏 mapped 实例的内部状态。
            _dr = {
                k: v
                for k, v in _r.__dict__.items()
                if k != "_sa_instance_state"
            }
            _dr["dt"] = fun.datetime_to_str(_dr["dt"])
            record_dicts.append(_dr)
        return record_dicts, total

    def get_line_mmds(self, line: Union[BI, XD]):
        mmds = list(set(line.line_mmds("|")))
        mmds = [self.map_mmd_type[_m] for _m in mmds]
        return "|".join(mmds)

    def get_line_bcs(self, line: Union[BI, XD]):
        bcs = list(set(line.line_bcs("|")))
        bcs = [self.map_bc_type[_b] for _b in bcs]
        return "|".join(bcs)

    def prompt(self, cd: ICL) -> str:
        """将缠论数据序列化为 Markdown 格式 prompt，供大模型分析。"""
        stock_info = self.ex.stock_info(cd.get_code())
        if stock_info is None:
            raise Exception(f"股票信息获取失败 {cd.get_code()}")
        stock_name = stock_info["name"]

        # precision 字段长度 - 1 得到小数位数（如 "0.001" → 长度4 - 1 = 3位）
        precision = (
            len(str(stock_info["precision"])) - 1
            if "precision" in stock_info.keys()
            else 2
        )

        k = cd.get_src_klines()[-1]
        prompt = "```markdown\n# 缠论技术分析\n\n"
        prompt += "请根据以下缠论数据，分析后续可能走势，并按照概率排序输出。\n\n"
        prompt += "**输出格式：Markdown**\n\n"
        prompt += f"## 当前品种\n- **代码/名称**：`{cd.get_code()} - {stock_name}`\n- **数据周期**：`{cd.get_frequency()}`\n- **当前时间**：`{fun.datetime_to_str(k.date)}`\n- **最新价格**：`{round(k.c, precision)}`\n\n"

        bis_count = 9 if len(cd.get_bis()) >= 9 else len(cd.get_bis())
        prompt += f"## 最新的 {bis_count} 条缠论笔数据\n\n"
        prompt += "| 起始时间 | 结束时间 | 方向 | 起始值 | 完成状态 | 买点 | 背驰 |\n"
        prompt += "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|\n"
        for bi in cd.get_bis()[-9:]:
            prompt += f"| {fun.datetime_to_str(bi.start.k.date)} | {fun.datetime_to_str(bi.end.k.date)} | {self.map_dircetion_type[bi.type]} | {round(bi.start.val, precision)} - {round(bi.end.val, precision)} | {bi.is_done()} | {self.get_line_mmds(bi)} | {self.get_line_bcs(bi)} |\n"
        prompt += "\n"

        xds_count = 3 if len(cd.get_xds()) >= 3 else len(cd.get_xds())
        prompt += f"## 最新的 {xds_count} 条缠论线段数据\n\n"
        prompt += "| 起始时间 | 结束时间 | 方向 | 起始值 | 完成状态 | 买点 | 背驰 |\n"
        prompt += "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|\n"
        for xd in cd.get_xds()[-3:]:
            prompt += f"| {fun.datetime_to_str(xd.start.k.date)} | {fun.datetime_to_str(xd.end.k.date)} | {self.map_dircetion_type[xd.type]} | {round(xd.start.val, precision)} - {round(xd.end.val, precision)} | {xd.is_done()} | {self.get_line_mmds(xd)} | {self.get_line_bcs(xd)} |\n"
        prompt += "\n"

        for zs_type in cd.get_config()["zs_bi_type"]:
            zss = cd.get_bi_zss(zs_type)
            if len(zss) >= 1:
                prompt += f"### 中枢信息：{self.map_config_zs_type[zs_type]}\n\n"
                if len(zss) >= 2:
                    zs_direction = cd.zss_is_qs(zss[-2], zss[-1])
                    prompt += f"- 最新两个中枢的位置关系：**{self.map_zss_direction[zs_direction]}**\n\n"
                else:
                    prompt += "- 目前只有单个中枢\n\n"
                prompt += "| 起始时间 | 结束时间 | 方向 | 最高值 | 最低值 | 高点 | 低点 | 级别 |\n"
                prompt += "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|\n"
                for zs in zss[-2:]:
                    _zs_sd, _zs_ed = _zs_dates(zs)
                    _zs_sd_str = fun.datetime_to_str(_zs_sd) if _zs_sd is not None else ""
                    _zs_ed_str = fun.datetime_to_str(_zs_ed) if _zs_ed is not None else ""
                    prompt += f"| {_zs_sd_str} | {_zs_ed_str} | {self.map_zs_type[zs.type]} | {round(zs.gg, precision)} | {round(zs.dd, precision)} | {round(zs.zg, precision)} | {round(zs.zd, precision)} | {zs.level} |\n"
                prompt += "\n"

        prompt += "> **数据说明**：中枢级别的意思，1表示是本级别，根据中枢内的线段数量计算，小于等于9表示本级别，大于1表示中枢内的线段大于9，中枢级别升级 (计算公式: `round(max([1, zs.line_num / 9]), 2)`)\n\n"
        prompt += "---\n"
        prompt += "请根据以上提供的笔/线段/中枢数据，进行分析。"
        return prompt

    def req_llm_ai_model(self, prompt: str) -> dict:
        """按配置优先级路由到 OpenRouter 或 SiliconFlow 大模型服务。"""
        if config.OPENROUTER_AI_KEYS != "" and config.OPENROUTER_AI_MODEL != "":
            return self.req_openrouter_ai_model(prompt)
        if config.AI_TOKEN != "" and config.AI_MODEL != "":
            return self.req_siliconflow_ai_model(prompt)

        return {
            "ok": False,
            "msg": "未正确配置大模型的 API key 和模型名称",
            "model": "",
        }

    def req_siliconflow_ai_model(self, prompt: str) -> dict:
        url = "https://api.siliconflow.cn/v1/chat/completions"

        payload = {
            "model": config.AI_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "stream": False,
            "max_tokens": 4096,
            "stop": ["null"],
            "temperature": 0.7,
            "top_p": 0.7,
            "top_k": 50,
            "frequency_penalty": 0.5,
            "n": 1,
            "response_format": {"type": "text"},
        }
        headers = {
            "Authorization": f"Bearer {config.AI_TOKEN}",
            "Content-Type": "application/json",
        }

        # 双段超时：connect 10s 防 DNS/握手卡死；read 180s 容忍 R1 等推理较慢的模型。
        try:
            response = requests.request(
                "POST", url, json=payload, headers=headers, timeout=(10, 180)
            )
        except requests.RequestException as e:
            return {
                "ok": False,
                "msg": f"AI 接口调用网络异常：{e}",
                "model": config.AI_MODEL,
            }

        try:
            ai_res = json.loads(response.text)
        except Exception as e:
            return {
                "ok": False,
                "msg": f"JSON 解析异常：{e}",
                "model": config.AI_MODEL,
            }

        if response.status_code != 200:
            err_msg = ai_res.get("message") if isinstance(ai_res, dict) else str(ai_res)
            return {
                "ok": False,
                "msg": f"AI 接口调用失败：{err_msg}",
                "model": config.AI_MODEL,
            }

        try:
            msg = ai_res["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            return {
                "ok": False,
                "msg": f"AI 接口返回结构异常：{e}",
                "model": config.AI_MODEL,
            }
        return {"ok": True, "msg": msg, "model": config.AI_MODEL}

    def req_openrouter_ai_model(self, prompt: str) -> dict:
        """调用 OpenRouter API 发送 prompt，返回模型回答内容。"""

        try:
            client = openai.OpenAI(
                api_key=config.OPENROUTER_AI_KEYS,
                base_url="https://openrouter.ai/api/v1",
            )
            response = client.chat.completions.create(
                model=config.OPENROUTER_AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
            if (
                response.choices[0].message.content == ""
                and response.choices[0].message.refusal is not None
            ):
                return {
                    "ok": False,
                    "msg": f"**[OpenAI API 错误]**: {response.choices[0].message.refusal}",
                    "model": config.OPENROUTER_AI_MODEL,
                }

            return {
                "ok": True,
                "msg": response.choices[0].message.content,
                "model": config.OPENROUTER_AI_MODEL,
            }
        except openai.OpenAIError as oe:
            return {
                "ok": False,
                "msg": f"**[OpenAI API 错误]**: {str(oe)}",
                "model": config.OPENROUTER_AI_MODEL,
            }
        except Exception as e:
            return {
                "ok": False,
                "msg": f"**[系统异常]**: {str(e)}",
                "model": config.OPENROUTER_AI_MODEL,
            }


if __name__ == "__main__":
    ai = AIAnalyse("a")
    print(ai.analyse("SH.000001", "d"))