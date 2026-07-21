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
from threading import RLock
from typing import Dict, Optional

from cachetools import TTLCache

from chanlun.persistence.file_db import fdb
from chanlun.tools.daemon_executor import DaemonExecutor
from chanlun.tools.cache_version import source_fingerprint
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


def _cfg_int(name: str, default: int) -> int:
    """从 chanlun.config 读 int 配置, 缺失/异常回退 default(不让坏 config 炸 import)。"""
    try:
        from chanlun import config
        return int(getattr(config, name, default))
    except Exception:
        return default


# 方向2: firstDataRequest 全量快照的过期阈值, 按交易时段区分。
# - 盘中 (market_now_trading=True): 数据每根 K 线在变, 用短阈值 → 尽快后台刷新到最新
#   (serve-stale 已先秒显, 刷新在后台; 短阈值只是让缓存更快追平实时)。
# - 收盘/非交易时段: 数据静止, 用长阈值 → 避免对不变数据反复重算 (省 QMT/CPU)。
# 两个阈值都只决定"是否派后台刷新", 任一情况都先返回旧快照秒显, 绝不阻塞用户 (方向1)。
_SNAPSHOT_STALE_AFTER_TRADING = _cfg_int("CHART_SNAPSHOT_STALE_AFTER_TRADING", 300)
_SNAPSHOT_STALE_AFTER_CLOSED = _cfg_int("CHART_SNAPSHOT_STALE_AFTER_CLOSED", 3600)

# serve-stale 过期"上限": 超过此幅度的重度过期不再 serve-stale, 回退旧版 MISS-阻塞重算。
# 根因(2026-06-29): 上面两个阈值只决定"是否 serve-stale", serve-stale 本身**无上限**——
# 非活跃周期的缓存(1m 隔午休/隔夜、30m 隔日)会停更几小时~几天, firstDataRequest 仍把那份
# 旧快照原样返回, 其"未完成笔"是几小时/几天前的(用户报"未完成笔滞后/该实仍虚")。
# serve-stale 只在"只差几根 bar"时有意义(秒显近似正确、悄悄自愈); 差到几小时/几天时
# 必须回退阻塞重算保证新鲜。窗口语义:
#   age < STALE_AFTER          → fresh    (直接返回, 不刷新)
#   STALE_AFTER ≤ age < MAX    → serve_stale (秒显旧 + 后台刷新)
#   age ≥ MAX (或 validated_at 未知) → too_stale (MISS → 阻塞重算, 必新鲜)
# 盘中 MAX 取 30min: 既保留"差几~30min"的 serve-stale 秒显, 又拦住隔午休/隔夜的重度过期;
# 收盘 MAX 取 1 天: 收盘数据静止、serve-stale 无滞后, 仅拦"隔多日缺整段交易日"的缓存。
_SNAPSHOT_SERVE_STALE_MAX_TRADING = _cfg_int("CHART_SERVE_STALE_MAX_TRADING", 1800)
_SNAPSHOT_SERVE_STALE_MAX_CLOSED = _cfg_int("CHART_SERVE_STALE_MAX_CLOSED", 86400)


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
# - v35 (2026-06-11) ── kuozhan 级买卖点/背驰**段粒度化**(V3 审计修复):kuozhan_level_signals_ex
#   次级别=下级中枢摆动腿(topic2 C2.10:对 5m 中枢,3买回试=「1m 走势类型」,非单根线段;旧
#   xds[b0+1]/[b0+2] 单线段口径与 30m 修复前同质的级别错配)。影响 1m 图 recursive_levels
#   L1(5m) 的 bsp/bcs(中枢序列不变);5m/30m 图升级链走 tongjibie 不受影响。强制旧缓存失效。
# - v36 (2026-06-24) ── 递归配色:kuozhan 高级别(5m/30m…)补 ``zslx_lines``(取同级分支 zslxs),
#   供前端在低周期图上画各级走势类型「线段」线条(原仅 L0 有线条、高级别空)。字段已存在、
#   现在被填充;source_fingerprint 已随 tv_chart 改动变化,显式 bump 求稳。
# - v37 (2026-07-20) ── 中枢图形补充 tower/recursive_level/ZD/ZG/done、进入段、离开段与
#   关联买卖点元数据，供右栏双塔审计详情直接展示。输出字段结构变化，显式失效旧磁盘缓存。
# - v39 (2026-07-21) ── 严格结构快照携带可还原的价格基准元数据；旧缓存无法证明复权
#   历史与当前 QMT 数据同属一个价格纪元，必须失效后用当前因子账本重建。
_CHART_CACHE_SCHEMA_VERSION = "v40"


def _build_cache_key(market: str, code: str, frequency: str, cl_config: dict) -> str:
    """统一构造 chart_data_cache 的 key,确保所有调用方一致。

    key = 版本号 + **源码指纹** + market/code/freq + cl_config hash:
    - ``_CHART_CACHE_SCHEMA_VERSION``:手动 schema 版本,输出**字段结构**变化时 bump。
    - ``source_fingerprint()``:核心计算/渲染源文件内容 md5,**计算逻辑**变化(线段划分算法等,
      值变但字段不变)时自动变 → 旧缓存自动失效,免手动 bump(原只靠手动版本号、改算法忘 bump
      会留陈旧线段,实例 R34/A-B-C 修复后 AAPL 仍显示旧交叉线段)。
    """
    return (f"{_CHART_CACHE_SCHEMA_VERSION}_{source_fingerprint()}"
            f"_{market}_{code}_{frequency}_{_stable_hash(cl_config)}")


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
        # 老格式(无 validated_at 的裸 chart_data)补 validated_at 设 0 而非 now():设 now() 会把
        # 一条实际很旧的磁盘老格式 entry 误标"刚验证过",绕过新鲜度兜底(审查 L-2)。设 0 →
        # _entry_freshness 判 stale → firstDataRequest 强制重算,安全。(v36+source_fingerprint
        # 已使绝大多数老格式 key 自然 miss,此为防御性收口。)
        return _build_chart_cache_entry(cached, is_full_snapshot=True, validated_at=0)
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
    # RAM 命中(锁内快查, 不含磁盘 IO)
    with cache_lock:
        entry = _normalize_cache_entry(chart_data_cache.get(cache_key))
        if entry is not None:
            return entry

    # HIGH-2: RAM miss → 磁盘读移出 cache_lock。pickle-load(数百KB~MB)绝不在全局锁内做,
    # 否则一次冷读把 cache_lock 持有到 IO 结束 → 串行化所有 tv_history/写/SSE, 且 IOLoop
    # 线程会被磁盘 IO 卡住整个 Tornado。注意: 若调用方自己已持 cache_lock(如 tv_history
    # 主路径), RLock 重入下本段仍在其锁内(无 perf 收益但功能正确); 不持锁的调用方
    # (prepend 等)则真正锁外读盘。IOLoop 路径改用 _get_chart_cache_entry_ram_only(只读 RAM)。
    try:
        disk_entry = fdb.get_chart_cache(cache_key)
    except Exception as e:
        LogUtil.warning(f"[chart_cache] disk read failed key={cache_key} err={e}")
        disk_entry = None
    entry = _normalize_cache_entry(disk_entry)
    if entry is None:
        return None

    # 回填 RAM(CAS): 锁外读盘期间别的线程可能已写入更新值(SSE recompute / 用户重算),
    # 优先用已有的, 不用旧磁盘值覆盖更新的内存值。
    with cache_lock:
        existing = _normalize_cache_entry(chart_data_cache.get(cache_key))
        if existing is not None:
            return existing
        chart_data_cache[cache_key] = entry

    # 机会型清理(磁盘 IO)也移出锁。
    if random.randint(0, 2000) <= 1:
        try:
            fdb.maybe_cleanup_chart_cache()
        except Exception:
            pass

    return entry


def _get_chart_cache_entry_ram_only(cache_key: str):
    """只读 RAM 热层(绝不触磁盘)。供 IOLoop 线程(SSE _send_current_snapshot)调用——
    IOLoop 线程同步 pickle-load 磁盘会卡住整个 Tornado(所有 SSE 客户端, HIGH-2)。
    RAM miss 返回 None(调用方跳过补发, 由 firstDataRequest / 轮询兜底)。"""
    with cache_lock:
        return _normalize_cache_entry(chart_data_cache.get(cache_key))


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


def _first_request_freshness(
    cache_entry: dict, market_is_trading: bool, now: float = None
) -> str:
    """firstDataRequest 路径: 全量快照的新鲜度分档。

    返回三态之一(见上方常量块的窗口语义):
    - ``"fresh"``: 足够新鲜, 直接返回, 不必后台刷新;
    - ``"serve_stale"``: 小幅过期, 秒显旧快照 + 派后台重验证(方向1);
    - ``"too_stale"``: 重度过期(超 ``_SNAPSHOT_SERVE_STALE_MAX_*`` 上限, 或
      validated_at 缺失/<=0 的老格式未知时效), 不能 serve-stale(会把几小时/几天前
      的旧未完成笔发给前端), 交由调用方走 MISS-阻塞重算保证新鲜。

    阈值按交易时段区分(方向2): 盘中短/收盘长。重度过期上限同样按时段区分:
    盘中拦截隔午休/隔夜, 收盘拦截隔多日缺整段交易日。

    Args:
        cache_entry: chart_data_cache 条目(含 ``validated_at``)。
        market_is_trading: 当前该 market 是否处交易时段(由调用方算好传入,
            保持本函数纯净可测)。
        now: 当前时间戳(秒); None 时取 ``time.time()``(单测可注入固定时钟)。
    """
    validated_at = cache_entry.get("validated_at")
    if not isinstance(validated_at, (int, float)) or validated_at <= 0:
        # 老格式/未知时效: 无法判断有多旧, 保守走阻塞重算(等价旧 _full_snapshot_is_stale
        # 的 MISS), 一次重算后写入真实 validated_at, 后续即可正常分档。
        return "too_stale"
    now = time.time() if now is None else now
    age = now - validated_at
    if market_is_trading:
        soft, hard = _SNAPSHOT_STALE_AFTER_TRADING, _SNAPSHOT_SERVE_STALE_MAX_TRADING
    else:
        soft, hard = _SNAPSHOT_STALE_AFTER_CLOSED, _SNAPSHOT_SERVE_STALE_MAX_CLOSED
    if age < soft:
        return "fresh"
    if age < hard:
        return "serve_stale"
    return "too_stale"


def evaluate_cache_for_tv_history(
    cache_entry: Optional[dict],
    from_ts: int,
    to_ts: int,
    is_range_request: bool,
    *,
    market_is_trading: bool = True,
    now: float = None,
    force_refresh: bool = False,
) -> tuple:
    """评估 chart_data_cache entry 是否能满足 tv_history 当前请求。

    P5 (2026-05-15): 从 ``tv.py::tv_history`` 内嵌 ``_evaluate_cache`` 闭包提取
    成 module-level 纯函数。原内嵌实现依赖 ``_from``/``_to``/``is_range_request``
    三个 outer var; 提取后通过参数显式传递, 不再隐式依赖 closure 状态, 单测可独立。

    2026-06-27 (方向1+2): firstDataRequest 全量快照小幅过期不再 MISS-阻塞重算, 改为
    serve-stale(立即返回旧快照秒显)+ ``needs_refresh=True`` 让调用方派后台重验证。
    过期阈值按交易时段区分(``market_is_trading``)。range(polling)路径不变。

    2026-06-29 (过期上限): serve-stale 加幅度上限。重度过期(超
    ``_SNAPSHOT_SERVE_STALE_MAX_*``, 或 validated_at 未知)回退 MISS-阻塞重算 ——
    否则非活跃周期停更几小时/几天的旧快照会被原样返回, 其"未完成笔"是几小时/几天前的
    (用户报"未完成笔滞后/该实仍虚")。见 ``_first_request_freshness`` 三态语义。

    Args:
        cache_entry: chart_data_cache 中的 entry (None 表示 cache miss)
        from_ts: 请求 from 时间戳 (unix 秒, 0/负数表示未指定)
        to_ts: 请求 to 时间戳 (unix 秒)
        is_range_request: 是否窄范围请求 (firstDataRequest=false 且 from/to 都 >0)
        market_is_trading: 当前该 market 是否处交易时段(决定 serve-stale 的过期阈值)。
        now: 当前时间戳(秒); None 取 ``time.time()`` (单测可注入)。仅作用于
            firstDataRequest 过期判定; range 路径的 recently_validated 仍用真实时钟。

    Returns:
        (is_hit, cached_data, miss_reason, needs_refresh):
        - is_hit=True: 命中, cached_data 为 chart_data dict 可直接返回前端;
          needs_refresh=True 表示这是"过期快照即时返回", 调用方应派后台重验证。
        - is_hit=False: cache miss, miss_reason 是字符串原因 ("cache_empty" /
          "cache_partial_snapshot" / "cache_stale_snapshot"(重度过期回退阻塞重算)/
          "cache_no_coverage" / "cache_head_gap" / "cache_tail_gap"); needs_refresh 恒 False。
    """
    # H1(阶段E): 前端断档 gap-reset 主动要求绕过缓存重算 —— 无条件 MISS,让调用方重拉+重算。
    # 绕过而非删除缓存:重算失败时旧 entry 仍在(下次正常请求仍可 serve),符合 C1"绝不丢好缓存"。
    if cache_entry is None:
        return False, None, "cache_empty", False
    # H1(阶段E,F-2):force_refresh 无条件 MISS,放在 cache_entry is None 之后——空缓存仍报
    # cache_empty(语义更准),有缓存才报 cache_force_refresh。绕过而非删缓存(符合 C1"绝不丢好缓存")。
    if force_refresh:
        return False, None, "cache_force_refresh", False
    cached_data = cache_entry.get("data", {})
    cache_min_time = cache_entry.get("min_time")
    cache_max_time = cache_entry.get("max_time")
    if not is_range_request:
        if not cache_entry.get("is_full_snapshot", False):
            # 部分快照(可能只有几根 K 线)不能冒充全量返回, 仍走同步 MISS 重算。
            return False, None, "cache_partial_snapshot", False
        # 方向1+2 + 上限(2026-06-29): 按新鲜度三档处理。
        # - serve_stale(小幅过期): 秒显旧图 + 后台重验证, ≤数秒自愈, 不阻塞。
        # - too_stale(重度过期/老格式未知时效): 回退旧版 MISS-阻塞重算 —— serve-stale 无上限
        #   会把几小时/几天前的旧未完成笔发给前端(用户报"未完成笔滞后/该实仍虚"), 重度过期
        #   下必须重算保证新鲜(冷加载阻塞 0.5-2s 可接受, 远胜显示明显错误的旧笔)。
        freshness = _first_request_freshness(cache_entry, market_is_trading, now)
        if freshness == "too_stale":
            return False, None, "cache_stale_snapshot", False
        if freshness == "serve_stale":
            return True, cached_data, None, True
        return True, cached_data, None, False
    if cache_min_time is None or cache_max_time is None:
        return False, None, "cache_no_coverage", False
    if from_ts < cache_min_time:
        return False, None, "cache_head_gap", False
    if to_ts > cache_max_time:
        if _cache_entry_recently_validated(cache_entry):
            return True, cached_data, None, False
        return False, None, "cache_tail_gap", False
    return True, cached_data, None, False


# ---------------- 写入：RAM + 异步落盘 ----------------

# 磁盘异步写入器（chart_data_cache 落盘）。
#
# 为什么异步：单条 entry pickle 后 ~100-500KB，原子写盘 50-100ms，绝对不能让用户
# tv_history 请求等磁盘 fsync。失败仅记录 error 级日志，不影响 RAM 命中链路。
# 4 worker 足够撑住批量预热（symbols.py 全局 inflight 也才 2-4）+ 用户实时写入。
_CHART_CACHE_DISK_WORKERS = 4
_CHART_CACHE_MAX_PENDING_WRITES = 64
_chart_cache_disk_lock = threading.Lock()
_chart_cache_disk_closed = True
_chart_cache_accepting_writes = True
_chart_cache_disk_futures = set()
_chart_cache_disk_future_keys = {}
_chart_cache_disk_slots = threading.BoundedSemaphore(_CHART_CACHE_MAX_PENDING_WRITES)
_chart_cache_disk_executor = None


def _persist_chart_cache_async(cache_key: str, entry: dict) -> None:
    """Submit a best-effort write with a strict pending-task capacity."""
    snapshot = copy.deepcopy(entry)
    with _chart_cache_disk_lock:
        global _chart_cache_disk_closed, _chart_cache_disk_executor
        if _chart_cache_disk_closed:
            if not _chart_cache_accepting_writes:
                return
            _chart_cache_disk_executor = DaemonExecutor(
                max_workers=_CHART_CACHE_DISK_WORKERS,
                thread_name_prefix="ChartCacheDisk",
            )
            _chart_cache_disk_closed = False
        if _chart_cache_disk_closed:
            return
        executor = _chart_cache_disk_executor
        slots = _chart_cache_disk_slots
        if not slots.acquire(blocking=False):
            LogUtil.warning(
                f"[chart_cache] pending write limit reached, skip key={cache_key}"
            )
            return
        try:
            future = executor.submit(fdb.set_chart_cache, cache_key, snapshot)
        except Exception as exc:
            slots.release()
            LogUtil.warning(
                f"[chart_cache] async submit failed key={cache_key} err={exc}"
            )
            return
        _chart_cache_disk_futures.add(future)
        _chart_cache_disk_future_keys[future] = cache_key

    def _completed(done_future):
        try:
            error = None if done_future.cancelled() else done_future.exception()
            if error is not None:
                LogUtil.error(
                    f"[chart_cache] async write failed key={cache_key} err={error}"
                )
        except Exception as exc:
            LogUtil.warning(
                f"[chart_cache] async write completion failed key={cache_key} err={exc}"
            )
        finally:
            with _chart_cache_disk_lock:
                _chart_cache_disk_futures.discard(done_future)
                _chart_cache_disk_future_keys.pop(done_future, None)
            slots.release()

    future.add_done_callback(_completed)


def start_chart_cache_runtime():
    global _chart_cache_disk_closed, _chart_cache_disk_executor
    global _chart_cache_accepting_writes, _chart_cache_disk_slots
    with _chart_cache_disk_lock:
        if not _chart_cache_disk_closed:
            return
        if _chart_cache_disk_futures:
            raise RuntimeError("cannot restart chart cache writer with active writes")
        _chart_cache_disk_executor = DaemonExecutor(
            max_workers=_CHART_CACHE_DISK_WORKERS,
            thread_name_prefix="ChartCacheDisk",
        )
        _chart_cache_disk_slots = threading.BoundedSemaphore(
            _CHART_CACHE_MAX_PENDING_WRITES
        )
        _chart_cache_accepting_writes = True
        _chart_cache_disk_closed = False


def allow_lazy_chart_cache_writes():
    """Allow a newly created application to open the writer on first use."""
    global _chart_cache_accepting_writes
    with _chart_cache_disk_lock:
        if _chart_cache_disk_closed and not _chart_cache_disk_futures:
            _chart_cache_accepting_writes = True


def shutdown_chart_cache_runtime(wait=False):
    """Reject new writes and cancel queued disk-cache work."""
    global _chart_cache_accepting_writes, _chart_cache_disk_closed
    global _chart_cache_disk_executor
    with _chart_cache_disk_lock:
        _chart_cache_accepting_writes = False
        if _chart_cache_disk_closed:
            return not _chart_cache_disk_futures
        _chart_cache_disk_closed = True
        executor = _chart_cache_disk_executor
        _chart_cache_disk_executor = None
    if executor is not None:
        executor.shutdown(wait=bool(wait), cancel_futures=True)
    with _chart_cache_disk_lock:
        return not _chart_cache_disk_futures

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


def _delete_chart_cache_entry(cache_key: str) -> None:
    """Invalidate one RAM/disk entry after its price basis becomes unsafe."""

    with cache_lock:
        chart_data_cache.pop(cache_key, None)

    # A previous _set may still be writing this key asynchronously. Wait only
    # for those rare same-key writes before deleting, otherwise a late writer
    # could resurrect the unsafe snapshot after the unlink.
    with _chart_cache_disk_lock:
        pending = [
            future
            for future in _chart_cache_disk_futures
            if _chart_cache_disk_future_keys.get(future) == cache_key
        ]
    for future in pending:
        try:
            future.result()
        except Exception as exc:
            LogUtil.warning(
                f"[chart_cache] pending write failed before delete "
                f"key={cache_key} error={type(exc).__name__}: {exc}"
            )
    try:
        fdb.delete_chart_cache(cache_key)
    except Exception as exc:
        LogUtil.warning(
            f"[chart_cache] disk delete failed key={cache_key} "
            f"error={type(exc).__name__}: {exc}"
        )


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
# 异常空(数据源暂时不可用,如 cq 分段拉取失败返回 attrs['fetch_incomplete'])用短退避,与
# "真空(新股/退市,真没数据)"的 300s 区分:真空 5min 抑制防无限重拉;异常空 30s 快速自愈,不被
# 5min 抑制卡住恢复(C1+M3 协同,审查 M3)。
_TRANSIENT_NEGATIVE_TTL_SECONDS = 30.0
# value = (mark_ts, ttl)：每项按各自 ttl 独立失效,支持真空/异常空并存于同一表。
_NEGATIVE_CACHE_MAX_SIZE = 500
_negative_cache: Dict[str, tuple] = {}
_negative_cache_lock = threading.Lock()


def _is_negatively_cached(cache_key: str) -> bool:
    """检查 cache_key 是否在负缓存中（未超各自 ttl 内被确认无数据/暂不可用）。"""
    now = time.time()
    with _negative_cache_lock:
        item = _negative_cache.get(cache_key)
        if item is None:
            return False
        mark_ts, ttl = item
        if now - mark_ts > ttl:
            _negative_cache.pop(cache_key, None)
            return False
        return True


def _mark_negative_cache(cache_key: str, ttl: float = _NEGATIVE_CACHE_TTL_SECONDS) -> None:
    """Record a negative result while enforcing a strict oldest-first capacity."""
    now = time.time()
    with _negative_cache_lock:
        # Reinsert existing keys so dict insertion order reflects the latest mark.
        _negative_cache.pop(cache_key, None)
        _negative_cache[cache_key] = (now, ttl)
        stale = [
            key
            for key, (marked_at, item_ttl) in _negative_cache.items()
            if now - marked_at > item_ttl
        ]
        for key in stale:
            _negative_cache.pop(key, None)
        limit = max(1, int(_NEGATIVE_CACHE_MAX_SIZE))
        while len(_negative_cache) > limit:
            oldest = next(iter(_negative_cache))
            _negative_cache.pop(oldest, None)

def _klines_fetch_incomplete(klines) -> bool:
    """cq 源级完整性闸门信号：拉取带洞时返回空 DataFrame 且 attrs['fetch_incomplete']=True(C1)。

    web 层据此走短退避(_TRANSIENT_NEGATIVE_TTL_SECONDS)而非真空 5min 负缓存,避免数据源暂时失败
    被当真空抑制 5min 不自愈(M3)。回测/monitor 不查此标记,klines 契约不变。
    """
    attrs = getattr(klines, "attrs", None)
    return bool(attrs) and attrs.get("fetch_incomplete") is True
