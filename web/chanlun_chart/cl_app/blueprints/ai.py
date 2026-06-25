"""
AI 分析相关接口蓝图。

  - `/ai/analyse`
  - `/ai/analyse_records/<market>`
"""

from flask import Blueprint, request
from flask_login import login_required

from chanlun.tools.ai_analyse import AIAnalyse

from ..services.constants import market_types


ai_bp = Blueprint("ai", __name__)


@ai_bp.route("/ai/analyse", methods=["POST"])
@login_required
def ai_analyse():
    # 输入校验：market 走白名单，code/frequency 必填，避免 KeyError 500 与未知 market
    # 透传到下游引发更难诊断的异常。
    market = (request.form.get("market") or "").strip().lower()
    if market not in market_types:
        return {"ok": False, "msg": f"未知市场: {market!r}"}, 400
    code = (request.form.get("code") or "").strip()
    frequency = (request.form.get("frequency") or "").strip()
    if not code or not frequency:
        return {"ok": False, "msg": "code 与 frequency 为必填字段"}, 400

    ai_analyse_obj = AIAnalyse(market)
    ai_res = ai_analyse_obj.analyse(code, frequency)
    return ai_res


@ai_bp.route("/ai/analyse_records/<market>", methods=["GET"])
@login_required
def ai_analyse_records(market: str = "a"):
    # market 走白名单(与 /ai/analyse 一致),否则非法 market 进 Market() 裸 500。
    market = (market or "").strip().lower()
    if market not in market_types:
        return {"code": 1, "msg": f"未知市场: {market!r}", "count": 0, "data": []}, 400

    # 分页参数 clamp:无上界时超大 limit 会拉全表+序列化=自我 DoS、负 page 产生负 offset
    # (审查 F2)。type=int 解析失败返回 None,用 `or` 兜底默认值。
    page = max(1, request.args.get("page", 1, type=int) or 1)
    limit = min(max(1, request.args.get("limit", 10, type=int) or 10), 200)

    # 调用分页查询
    ai_analyse_records, total = AIAnalyse(market=market).analyse_records(
        page=page, limit=limit
    )
    return {
        "code": 0,
        "msg": "",
        "count": total,
        "data": ai_analyse_records,
    }