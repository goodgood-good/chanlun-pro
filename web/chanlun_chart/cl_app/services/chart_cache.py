"""图表数据缓存层（service，cache 全链路）。

L1 Phase 2 + Tier 4 P1 重构：把 tv.py 里 chart cache 相关的状态 + 函数集中迁出，
形成完整的 cache 层（读 + 写 + 异步落盘 + 负缓存），让 tv.py 真正回归"路由层"。

状态：
- ``chart_data_cache``: RAM 热层（TTLCache）；与 fdb.get_chart_cache 的磁盘冷层
  组成两层缓存（RAM miss → disk → 回填 RAM）。
- ``cache_lock``: RLock；多线程读写 chart_data_cache 时的粗粒度互斥。
- ``_CACHE_REVALIDATION_INTERVAL``: 30s，缓存在此时间内被验证过则视为有效。
- ``_chart_cache_disk_executor``: 异步落盘线程池（4 worker）。
- ``_negative_cache``: 空数据 cache_key 短期负缓存（5 min TTL），防新上市标的反复拉空。

工具函数（纯）：
- ``_stable_hash`` / ``_build_cache_key``: 跨进程稳定的 cache_key 构造
- ``_build_chart_cache_entry`` / ``_normalize_cache_entry``: entry 字段规范化
- ``_cache_entry_recently_validated``: 验证时间戳判断

业务函数：
- ``_get_chart_cache_entry``: 两层读取（RAM → disk → warm RAM）
- ``_set_chart_cache_entry``: 两层写入（RAM 立即可见 + 异步落盘）
- ``_mark_chart_cache_validated``: 更新 entry.validated_at
- ``_persist_chart_cache_async``: 提交磁盘写入，异常 fallback 同步
- ``_is_negatively_cached`` / ``_mark_negative_cache``: 空数据负缓存
"""
import copy
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from typing import Dict, Optional

from cachetools import TTLCache

from chanlun.file_db import fdb
from chanlun.tools.log_util import LogUtil

# ---------------- 状态 ----------------

# 图表数据计算结果缓存（RAM 热层）。
# RAM 仅做热点加速，持久化由 fdb.set/get_chart_cache 兜底（RAM 淘汰后磁盘仍可命中）。
chart_data_cache: TTLCache = TTLCache(maxsize=512, ttl=3600)

cache_lock: RLock = RLock()

# 缓存数据最近验证时间戳（防止非交易时段 DataPulse 反复 cache miss）
# H4: 验证时间戳直接放在 chart_data_cache 的 entry["validated_at"] 中，
# 不再单独维护 chart_data_validated_at TTLCache。
_CACHE_REVALIDATION_INTERVAL = 30  # 秒，缓存在此时间内被验证过则视为有效

# firstDataRequest=true 路径下 is_full_snapshot 快照的过期阈值 (远大于 polling 30s,
# 远小于"停机数天"; 重启后磁盘冷层旧 entry 能识别为过期, 强制 cache miss 拉新数据)。
_SNAPSHOT_STALE_AFTER = 3600  # 秒


# ---------------- 工具函数 ----------------

def _stable_hash(obj) -> str:
    """
    生成稳定的 hash（不受 PYTHONHASHSEED 影响，跨进程/重启一致）。
    这样多 worker 部署、进程重启后 cache_key 仍然稳定，缓存命中率不会被打穿。
    """
    try:
        s = json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        s = str(obj)
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# chart_data 序列化结构版本号。
#
# 后端 ``cl_data_to_tv_chart`` 输出的 chart_data dict 结构改动(新增/重命名字段、
# 字段语义变化)时,**必须 bump 这个版本**——否则启动 ``chart_warm`` 会把磁盘
# (fdb)里旧版本的 chart_data 回填 RAM,endpoint cache hit 永远拿到 stale 数据。
#
# 历史:
# - v5 (2026-05) ── 加入版本号机制本身。本次 bump 让 ``recursive_levels`` /
#   ``interval_nest`` / ``xd_zslx`` / ``bi_mmds`` / ``xd_mmds`` / ``bi_bcs`` /
#   ``xd_bcs`` 等原文化新字段在旧 entry 上全部失效,杜绝 endpoint 漏字段。
# - v6 (2026-05) ── 新增 ``xd_zslx_lines`` 以及 recursive_levels[*].zslx_lines,
#   让当前级别走势类型以线段形式参与下一层中枢显示。
# - v7 (2026-06) ── 级别纠正(递归 L0=线段中枢 / 笔中枢走 bi_zss 观察层 / 多周期
#   higher_zs)+ 中枢区间改用核心区 [ZD,ZG]。均改 chart_data 内容但不进 config,
#   bump 强制旧磁盘缓存失效重算,否则用户看不到这些改动。
# - v8 (2026-06) ── P8 中枢扩展实体化:高级别中枢改由 recursive_levels L1/L2/L3 承载,
#   P7 higher_zs 停用;图表渲染逻辑随之更新,旧 cache 需强制失效。
# - v9 (2026-06) ── P8 中枢扩展(错机制)拆除;中枢升级按 line4898 重做中,图表暂时只画 L0。
# - v10 (2026-06) ── P9 中枢升级·扩展(line4898 三段重合)上线: L1 由 zs_upgrade.kuozhan_zhongshu 产。
# - v11 (2026-06) ── 扩展区间改「摆动分段」(进入段+前3走势, 非全局底/顶): 301004 z9-11 [38.06]→[39.01]。
# - v12 (2026-06) ── 扩展中枢「离开不回」结束(三类买卖点)+全段走势定区间: 301004 出 3 个依次下移中枢。
# - v13 (2026-06) ── P9 正常case:中枢强制方向交替(line7268)消走势递归假中枢 + 相邻同类型走势
#   类型合并为扩展(line7264):L0 走势类型 band 渲染随之改变(301004 [下跌×3,盘整]→[下跌,盘整]),
#   bump 强制旧 cache 失效。
# - v14 (2026-06) ── 「正在形成的未完成中枢」入图(虚线框), 两层都改, 旧 cache 强制失效:
#   (a) L0: recursive_branch 非终止级别原只取 done_zss、丢弃 live → 改经 LevelResult.live_zss
#       带出右边缘正在形成的 L0(本周期)中枢, recursive_levels[L0].zss 新增 done=False 项。
#   (b) L1(5m级别)=kuozhan 扩展中枢三修: 主修 guard off-by-overlap(原误杀右边缘整组扩展)、
#       次修延伸到末线段标 done=False(虚线)、第三修一个 is_kuozhan run 内逐个抽取扩展中枢
#       (原一个 run 只出首个、跳过剩余 → 右边缘正在形成的 5min 中枢被吞)。
#       000001 右边缘 5min 中枢空档从 36~42 段 → 1~5 段, recursive_levels[L1] 内容变化。
# - v15 (2026-06) ── kuozhan 三修是在 v14 之后才落地的: v14 缓存可能已写入旧 kuozhan 输出
#   (5 个 L1、无右边缘中枢), key 不变会继续命中陈旧数据。bump v15 强制失效, 让 L1 三修生效。
#   并含「完成度口径回归原文」: L1(5m)扩展中枢的 done 改由**中枢结束条件**判(原文 line10031
#   三类点 / line7260 走势终完美)——已完成须由后续中枢确认其离开,**序列最后一个中枢恒未完成
#   (done=False 虚线)**,替换原「离开不回」判据(会把右边缘提前1段离开的最后中枢误判为已完成)。
#   recursive_levels[L1] 末个中枢 linestyle 0→1。
# - v16 (2026-06) ── L1(5m)中枢几何重做(对齐原文 line31774/10029, 替代旧摆动 three_segment):
#   kuozhan 改「子中枢运行交集分组」——沿 is_kuozhan run 累积子中枢、维持包络交集 [max dd,min gg]
#   有效, 塌缩点切成多个中枢, 区间=组内子中枢包络重合(原旧摆动法过度框选成超宽框, 见 000001
#   出图对比)。完成度=line26870「2 子中枢=进行式」+ line7260 结束条件「序列最后一个=未完成」。
#   recursive_levels[L1] 的中枢个数/区间/linestyle 全面变化, 强制旧 cache 失效。
# - v17 (2026-06) ── L1 完成度口径修正:原 v16「2 子中枢=进行式也算未完成」会让历史中间的 2 子
#   中枢组全标虚线 → 图上多个未完成中枢(用户:只该有一个)。改为**纯结束条件**:仅序列最后一个
#   中枢未完成(done=False), 其余全已完成。recursive_levels[L1] 中间中枢 linestyle 1→0。
# - v18 (2026-06) ── 买卖点分级修正:原 branch core 开时 `get_branch_bspoints` 恒用笔级却全塞
#   xd_mmds(段)=笔买卖点冒充段买卖点。改为**笔级→bi_mmds、段级(线段)→xd_mmds** 各归其位,
#   且 branch core 开时不再叠 legacy line_mmds。bi_mmds/xd_mmds 内容全变, 强制失效。
# - v19 (2026-06) ── 背驰信号接新核心:原图表背驰走 legacy line_bcs(极稀疏 笔3/段2)、与新核心
#   一类买卖点不一致(用户:背驰信号没有)。branch core 开时改接 get_branch_bcs(笔→bi_bcs/段→
#   xd_bcs, done_divergence 里 is_beichi 的离开段, QS/PZ)。000001:bi_bcs 3→36、xd_bcs 2→6。
# - v20 (2026-06) ── L0 结构化二类买卖点:bs2_branch 是跨级(次级别一类=二类)、对 L0 跳过 →
#   段级/笔级 L0 原无二类。新增 bs_branch.second_class(一类后首次回调不破前低/高=二类),接进
#   get_branch_bspoints。000001:笔级 +2buy×4/2sell×4、段级 +2sell×1。bi_mmds/xd_mmds 增二类项。
# - v21 (2026-06) ── 走势类型分段重写(item2,趋势型L1):原 zslx_branch 用 classify_rel 逐对+
#   _merge_same_type → 过度合并(000001:21中枢压成2走势类型,升不出L1;且反转处L0中枢重叠时
#   classify_rel 返回 expand 对反转失明)。重写为**本体摆动**(本体分离反转,line24727/24736/30931)
#   + **同级别中枢细分**(趋势内连续重叠中枢=盘整,line24727/24728/24735) + **本体分离分类**
#   (line8152/21637)。000001:2→6 走势类型(方向交替)、recursive 升出 L1 中枢(get_recursive_
#   branch_levels[L1])、L1 买卖点(Bs3 三类)激活。L0 走势类型显示(xd_zslx)/买卖点变化 → 强制失效。
# - v22 (2026-06) ── 多级别(5m/30m)中枢+买卖点+背驰:1min 图叠加高级别结构。递归 kuozhan
#   (中心定理二 line10029 套用)L0→L1(5m)→L2(30m)→L3(日线),各级中枢入 recursive_levels;
#   各级背驰/买卖点(cl.get_kuozhan_levels:kuozhan 中枢补进入/离开段→is_beichi 背驰+一类、
#   几何三类)带 freq 级别标入 xd_mmds/xd_bcs(level=5m/30m/日线)。000001:L1=7/L2=2 中枢、
#   5m 买卖点 10(含 1sell 顶背驰=L0 漏的)+背驰 3、30m 3buy×1。recursive_levels/xd_mmds/
#   xd_bcs 内容全变,强制旧缓存失效。
# - v23 (2026-06) ── 30m 中枢改**同级别分解**(原文 line24727/24735,用户硬性要求):升级链
#   封顶 30m 操作级——<30m(1m→5m)用 kuozhan(非同级别,延伸/扩展),30m 用 tongjibie_zhongshu
#   (次级别走势类型恰好3段重合、不延伸、允许盘整+盘整)。get_kuozhan_levels 按 _UPGRADE_CHAIN
#   分方法;30m/日线图无升级链(只 base)。L2(30m)中枢区间/个数与 v22(纯 kuozhan)不同 → 失效。
# - v24 (2026-06) ── get_kuozhan_levels 即使某级空也出层(升级链=该周期可用级别):短数据下 30m
#   同级别分解=0 中枢时,recursive_levels 仍含(空)level=2 → 前端菜单恒有 30m(zs_L2)选项(修
#   「1min图看不到30min选项」)。数据够了自动填充。recursive_levels 结构变(多空层)→ 失效。
# - v25 (2026-06) ── QMT 1m/5m 回看 90→365 天(exchange_qmt 专属覆盖):lookback 不进 cache_key
#   (key 只含 config hash),故 lookback 改了旧缓存仍命中陈旧 90 天数据 → 本 bump 强制失效,
#   让 365 天更长历史(更多 5m/30m 中枢)在重启后立即生效、免手动清缓存。
# - v26 (2026-06) ── get_kuozhan_levels 逐级容错:某级(尤其 30m 同级别 zslx/tongjibie)在边缘
#   实时数据上抛异常时,原整个函数抛出→cl_utils 静默吞→recursive_levels 只剩[0]、5m/30m 中枢/
#   买卖点/背驰全没(用户「看不到5m/30m买卖点背驰」真凶)。改逐级 try/except:只丢出错级、其他
#   级照常产出。用户数据 recursive_levels 从[0]变回[0,1,2]→强制旧缓存失效。
# - v27 (2026-06) ── 30m 同级别分解改**严格交替腿**(原文 line24727 上下上/下上下、24751 操作
#   程式严格交替、25123「更大就分解成小的」):原 tongjibie 喂 ZslxBranchCalculator 合并走势类型
#   (趋势含多中枢、方向不交替)→ 凑不出上下上 → 30m 中枢恒 0(000001 5m 图实测)。改为从中枢序列
#   直接建严格交替腿(连续同向并一腿、反转处断开共享极值中枢)→ 三腿重合=中枢。000001:5m 图 30m
#   中枢 0→2、1m 图 30m 中枢 1→3(+买卖点/背驰)。recursive_levels[30m] 内容变,强制旧缓存失效。
# - v28 (2026-06) ── 中枢升级(非同级别)重做为**延伸+扩张+优先级**(原文 line8157/23045/10029,
#   用户口径):① 延伸=单中枢 line_num≥9 → 3+3+3 分 3 组重合(原 TODO 未实现);② 扩张=相邻两同级别
#   中枢 GG/DD 重叠 → 三走势[A·连接·B]重合(原用「运行交集」把 N 个囫囵分组);③ 延伸优先于扩张。
#   000001 1m 图 5m 中枢 22→29(延伸10+扩张19)、30m 中枢随之变。recursive_levels 内容变,强制失效。
# - v29 (2026-06) ── 扩张升级的「三走势」改**按股价分**(原文10012,用户口径「复用走势类型分解」):
#   原写死 [中枢A本体·连接·中枢B本体],改为 _three_zoushi_overlap——把跨两中枢的区间在最高线段(顶)/
#   最低线段(底)两处转折切 3 段(上涨/下跌/盘整任意组合、段数可变),取三段重合。扩张中枢区间变(数量
#   不变),30m tongjibie 随之 3→4。recursive_levels 内容变,强制旧缓存失效。
# - v30 (2026-06) ── 30m 同级别分解修正(用户指出原 30m 中枢不对):原「交替腿」从原始线段中枢按中心
#   分组=级别错。按原文 38/39 课重做:次级别单位=5 分钟走势类型(zslx,原文 25178「Ai 是 5 分钟走势
#   类型」),经结合运算(line25179 合并相邻同方向)成严格交替段(Ai 奇下偶上),连续 3 段上下上/下上下
#   重合=中枢(line24727)。000001:5m 图 30m 中枢 2→1[3947,3984]、1m 图 30m 3→1。强制旧缓存失效。
# - v31 (2026-06) ── 同级别分解两修:① 段区间改**整段高低点**(原文20课 gn/dn=Zn 的高低点,原用段内
#   中枢 gg/dd 包络=口径过严,趋势段两端远超包络→三段重合饿死);② 30m 买卖点/背驰改**段粒度**
#   (tongjibie_level_signals:回抽=中枢后第一个交替段整段,原 kuozhan_level_signals 用单根 5m 线段
#   =级别错配恒空)。000001 5m 图:30m 中枢 [3817,3984]+3buy@2026-04-08(原买卖点恒空)。强制失效。
# - v32 (2026-06) ── 扩张升级区间改 **[max(前DD,后DD), min(前GG,后GG)]**(原文 line10018 三段重叠
#   简化公式,Z段=前/后中枢段整段极值;非空性⟺中心定理二触及条件,与 is_kuozhan 自洽)。原「顶/底切
#   三走势再交集」把中枢本体劈开(violates Z段=完整次级别走势类型),区间系统性偏窄/空(实测10/19流入
#   退化分支)。kuozhan L1 区间变宽→L2(tongjibie 基于 L1 走势类型)随之。强制失效。
# - v33 (2026-06) ── 全链整段口径收口:zslx_branch._finalize 喂回 zs_high/zs_low 改**走势类型
#   整段高低点**(原文20课 gn/dn,含进入/离开段端点;原中枢 gg/dd 包络=过严)。影响 L1+ 递归树与
#   bs2/bs3 跨级信号(实测10只:413信号不变/仅1增1减=0.5%,L0 全零变动;kuozhan 各级数量不变)。
# - v34 (2026-06-11) ── 同级别分解段语义对齐原文(fix/zhongshu-l0):① zslx 盘整段 _type 改净位移·
#   **转折点口径**(进入段起点→离开段**起点**,L25128 段起点=前段结束点/L8131 a1=b1;原继承摆动腿
#   方向对「横盘+暴跌收尾」错标,原净位移用离开段终点对 V 型链翻号);② tongjibie 交替段改
#   **本体摆动腿直出**(_swing_alternating_segs,39课L25179 Ai 严格交替/42课L26239 趋势仍是一段;
#   原「zslx 标签+_jiehe_segments 同向合并」对 V/Λ 型 expand 链方向歧义)。实测10只:L0 买卖点 64
#   个零变动;30m 中枢仅 600519 区间收紧[1322,1510]→[1322,1431]、510300 假窄条[4.71,4.78]消失
#   (z5 本体与前震荡区真实不重叠)。recursive_levels L1+/30m tongjibie 内容变化,强制旧缓存失效。
_CHART_CACHE_SCHEMA_VERSION = "v34"


def _build_cache_key(market: str, code: str, frequency: str, cl_config: dict) -> str:
    """统一构造 chart_data_cache 的 key,确保所有调用方一致。

    key 含 ``_CHART_CACHE_SCHEMA_VERSION`` 前缀——bump 该版本号即可让所有旧
    磁盘 entry 失效,新版本路径无 stale 字段污染风险。
    """
    return f"{_CHART_CACHE_SCHEMA_VERSION}_{market}_{code}_{frequency}_{_stable_hash(cl_config)}"


def _build_chart_cache_entry(cl_chart_data: dict, is_full_snapshot: bool, validated_at: float = None):
    validated_at = time.time() if validated_at is None else validated_at
    bar_times = cl_chart_data.get("t", []) if isinstance(cl_chart_data, dict) else []
    return {
        "data": cl_chart_data,
        "min_time": bar_times[0] if len(bar_times) > 0 else None,
        "max_time": bar_times[-1] if len(bar_times) > 0 else None,
        "validated_at": validated_at,
        "is_full_snapshot": bool(is_full_snapshot),
    }


def _normalize_cache_entry(cached) -> Optional[dict]:
    """把任意来源（RAM / 磁盘）的 cache 对象规范化为带 validated_at 的 dict。

    None / 非 dict 一律视为 miss；老格式没有 validated_at 时补一个当前时间，
    保持下游 _cache_entry_recently_validated 等逻辑可用。
    """
    if cached is None:
        return None
    if isinstance(cached, dict) and "data" in cached and "validated_at" in cached:
        return cached
    if isinstance(cached, dict):
        return _build_chart_cache_entry(cached, is_full_snapshot=True, validated_at=time.time())
    return None


def _get_chart_cache_entry(cache_key: str):
    """两层缓存读取：先 RAM、miss 再走磁盘并回填 RAM。

    磁盘命中后立刻 warm 回 RAM（直接赋值不触发异步落盘——entry 来自磁盘已经持久化），
    后续相同 cache_key 的访问就走 RAM 热层。

    磁盘读失败（损坏/IO 异常）由 fdb.get_chart_cache 内部处理，这里看到的是 None。

    线程安全：``chart_data_cache`` 是非线程安全的 TTLCache，所有读写都必须在
    ``cache_lock`` 内。本函数自带 ``cache_lock``（可重入 RLock），调用方无需
    （也可重复）持锁——既保护直接调用方，也保护经 kline_recompute / symbols
    等不持锁路径进来的访问。
    """
    with cache_lock:
        entry = _normalize_cache_entry(chart_data_cache.get(cache_key))
        if entry is not None:
            return entry

        # RAM miss → 尝试磁盘冷层
        try:
            disk_entry = fdb.get_chart_cache(cache_key)
        except Exception as e:
            LogUtil.warning(f"[chart_cache] disk read failed key={cache_key} err={e}")
            disk_entry = None
        entry = _normalize_cache_entry(disk_entry)
        if entry is None:
            return None

        # 回填 RAM；不再异步写盘（来源就是磁盘）。
        chart_data_cache[cache_key] = entry

        # 机会型清理：极低概率触发，避免 chart_cache 目录膨胀。
        if random.randint(0, 2000) <= 1:
            try:
                fdb.maybe_cleanup_chart_cache()
            except Exception:
                pass

        return entry


def _entry_freshness(cache_entry: dict, mode: str) -> str:
    """统一的 cache entry 新鲜度判定。

    Args:
        cache_entry: chart_data_cache 条目，含 ``validated_at`` 浮点字段。
        mode: ``"polling"``（30s 阈值，TV polling 路径）或
              ``"first_request"``（3600s 阈值，firstDataRequest=true 路径，
              用于重启后识别停机期间过期的磁盘快照）。

    Returns:
        ``"fresh"`` / ``"stale"`` / ``"unknown"``（缺字段时按 stale 处理）。
    """
    if not isinstance(cache_entry, dict):
        return "unknown"
    validated_at = cache_entry.get("validated_at")
    if not isinstance(validated_at, (int, float)) or validated_at <= 0:
        return "unknown"

    threshold = (
        _CACHE_REVALIDATION_INTERVAL if mode == "polling" else _SNAPSHOT_STALE_AFTER
    )
    return "fresh" if (time.time() - validated_at) < threshold else "stale"


def _cache_entry_recently_validated(cache_entry: dict) -> bool:
    """polling 路径专用: 30s 内验证过即视为有效。委托给 _entry_freshness。"""
    return _entry_freshness(cache_entry, mode="polling") == "fresh"


def _full_snapshot_is_stale(cache_entry: dict) -> bool:
    """全量快照是否过期: validated_at 距今超过 _SNAPSHOT_STALE_AFTER。

    用于 tv_history 在 firstDataRequest=true 路径下校验从磁盘冷层加载的 entry
    时效: 程序停机期间没有 polling 推 validated_at, 重启后第一个请求若不做时效
    校验会直接命中老快照, 导致缺停机期间产生的 K 线。
    None / 非 dict / 缺字段一律视为过期 (保守降级, 触发 cache miss 重新拉取)。

    委托给 _entry_freshness("first_request"); ``unknown`` 也按过期处理。
    """
    return _entry_freshness(cache_entry, mode="first_request") != "fresh"


def evaluate_cache_for_tv_history(
    cache_entry: Optional[dict],
    from_ts: int,
    to_ts: int,
    is_range_request: bool,
) -> tuple:
    """评估 chart_data_cache entry 是否能满足 tv_history 当前请求。

    P5 (2026-05-15): 从 ``tv.py::tv_history`` 内嵌 ``_evaluate_cache`` 闭包提取
    成 module-level 纯函数。原内嵌实现依赖 ``_from``/``_to``/``is_range_request``
    三个 outer var; 提取后通过参数显式传递, 不再隐式依赖 closure 状态, 单测可独立。

    Args:
        cache_entry: chart_data_cache 中的 entry (None 表示 cache miss)
        from_ts: 请求 from 时间戳 (unix 秒, 0/负数表示未指定)
        to_ts: 请求 to 时间戳 (unix 秒)
        is_range_request: 是否窄范围请求 (firstDataRequest=false 且 from/to 都 >0)

    Returns:
        (is_hit, cached_data, miss_reason):
        - is_hit=True: cache 命中, cached_data 为 chart_data dict
        - is_hit=False: cache miss, miss_reason 是字符串原因 ("cache_empty" /
          "cache_partial_snapshot" / "cache_stale_snapshot" / "cache_no_coverage" /
          "cache_head_gap" / "cache_tail_gap")
    """
    if cache_entry is None:
        return False, None, "cache_empty"
    cached_data = cache_entry.get("data", {})
    cache_min_time = cache_entry.get("min_time")
    cache_max_time = cache_entry.get("max_time")
    if not is_range_request:
        if not cache_entry.get("is_full_snapshot", False):
            return False, None, "cache_partial_snapshot"
        # 即便 is_full_snapshot=True, 也要校验时效; 否则程序停机数天后第一个
        # firstDataRequest=true 请求会直接命中过期 snapshot (磁盘冷层),
        # 用户看到的图表缺停机期间产生的 K 线。
        if _full_snapshot_is_stale(cache_entry):
            return False, None, "cache_stale_snapshot"
        return True, cached_data, None
    if cache_min_time is None or cache_max_time is None:
        return False, None, "cache_no_coverage"
    if from_ts < cache_min_time:
        return False, None, "cache_head_gap"
    if to_ts > cache_max_time:
        if _cache_entry_recently_validated(cache_entry):
            return True, cached_data, None
        return False, None, "cache_tail_gap"
    return True, cached_data, None


# ---------------- 写入：RAM + 异步落盘 ----------------

# 磁盘异步写入器（chart_data_cache 落盘）。
#
# 为什么异步：单条 entry pickle 后 ~100-500KB，原子写盘 50-100ms，绝对不能让用户
# tv_history 请求等磁盘 fsync。失败仅记录 error 级日志，不影响 RAM 命中链路。
# 4 worker 足够撑住批量预热（symbols.py 全局 inflight 也才 2-4）+ 用户实时写入。
_CHART_CACHE_DISK_WORKERS = 4
_chart_cache_disk_executor = ThreadPoolExecutor(
    max_workers=_CHART_CACHE_DISK_WORKERS,
    thread_name_prefix="ChartCacheDisk",
)


def _persist_chart_cache_async(cache_key: str, entry: dict) -> None:
    """提交一次磁盘写入；调用方不阻塞。

    deepcopy entry 后再提交, 避免与主线程并发 in-place 修改 ``entry["data"]``
    (例如 tv_history cache hit 路径 lazy 补算 ``apply_higher_macd_to_chart_data``、
    或 ``_merge_chart_data`` 之后某些 prepend 后续操作) 产生
    ``RuntimeError: dictionary changed size during iteration`` 写盘失败。

    调用方(``_set_chart_cache_entry``)在 cache_lock 内调本函数, 此处 deepcopy
    在 cache_lock 保护下做, 期间没有其他线程能改 entry, 拿到的 snapshot 安全。
    成本: chart_data 通常 1KB~几十 KB, deepcopy < 1ms, 主线程同步代价可接受。
    """
    snapshot = copy.deepcopy(entry)
    try:
        _chart_cache_disk_executor.submit(fdb.set_chart_cache, cache_key, snapshot)
    except Exception as e:
        # executor 已关闭 / 队列满等极端场景：直接同步 fallback 写一次，
        # 写失败也只是丢这条，下次预热会重新算。
        LogUtil.warning(
            f"[chart_cache] async submit failed, fallback sync write key={cache_key} err={e}"
        )
        try:
            fdb.set_chart_cache(cache_key, snapshot)
        except Exception as e2:
            LogUtil.error(f"[chart_cache] fallback sync write failed key={cache_key} err={e2}")


def _set_chart_cache_entry(cache_key: str, cl_chart_data: dict, is_full_snapshot: bool):
    """两层缓存写入：RAM 立即可见，磁盘异步持久化。

    本函数自带 ``cache_lock``（可重入 RLock），调用方无需（也可重复）持锁——
    ``deepcopy`` 在锁内做，与 ``_persist_chart_cache_async`` 的 snapshot 不变量一致。
    """
    entry = _build_chart_cache_entry(cl_chart_data, is_full_snapshot=is_full_snapshot)
    with cache_lock:
        chart_data_cache[cache_key] = entry
        _persist_chart_cache_async(cache_key, entry)
    return entry


def _mark_chart_cache_validated(cache_key: str):
    # H4: validated_at 只更新到 entry 内部；entry 本身的 TTL 由 chart_data_cache 统一管理。
    # 若 cache 已被 TTL 淘汰，没有 entry 可标记，直接返回（下次请求自然重算）。
    # 自带 cache_lock（可重入）；_get_chart_cache_entry 同样自锁，嵌套获取安全。
    with cache_lock:
        entry = _get_chart_cache_entry(cache_key)
        if entry is None:
            return
        entry["validated_at"] = time.time()
        chart_data_cache[cache_key] = entry


# ---------------- 负缓存（空数据短期记忆）----------------

# 2026-04 修复：空数据周期的负缓存。
# 问题：ZK.US 这种新上市标的，长桥 1m 接口返回不了那么久的历史 → ex.klines() 返回 []
# → web_batch_get_cl_datas 抛 "输入的K线数据为空" warning → 缓存里永远没有 1m 的 entry
# → 用户每 3 秒 polling 一次都会重新尝试算 1m → 每次又拉空 → 无限重试，浪费 HTTP 配额。
#
# 修复：klines 为空或 cl_chart_data 为空时，把 cache_key 加入负缓存集合，
# 5 分钟内同 cache_key 再来直接 return，不再调 ex.klines()。
# 5 分钟是权衡：太短退化成无效，太长会让"上市新股第一次有 1m 数据"延迟感知。
_NEGATIVE_CACHE_TTL_SECONDS = 300.0
_negative_cache: Dict[str, float] = {}
_negative_cache_lock = threading.Lock()


def _is_negatively_cached(cache_key: str) -> bool:
    """检查 cache_key 是否在负缓存中（最近 5 分钟内被确认无数据）。"""
    now = time.time()
    with _negative_cache_lock:
        ts = _negative_cache.get(cache_key)
        if ts is None:
            return False
        if now - ts > _NEGATIVE_CACHE_TTL_SECONDS:
            _negative_cache.pop(cache_key, None)
            return False
        return True


def _mark_negative_cache(cache_key: str) -> None:
    """标记 cache_key 为"无数据"，5 分钟内不再尝试拉取。"""
    now = time.time()
    with _negative_cache_lock:
        _negative_cache[cache_key] = now
        # 顺便清理过期项（懒清理，避免长期运行时无限增长）
        if len(_negative_cache) > 500:
            cutoff = now - _NEGATIVE_CACHE_TTL_SECONDS
            stale = [k for k, t in _negative_cache.items() if t < cutoff]
            for k in stale:
                _negative_cache.pop(k, None)
