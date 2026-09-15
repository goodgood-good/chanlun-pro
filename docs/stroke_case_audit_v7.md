当前旧笔构建的异常核查（v7，2026-09-14）

结论：**存在可复现异常。主要问题是合格反向结构的保护可以被后续候选路径绕过，以及同一已确认线段 ID 的确认时间会发生变化。** 本次新增独立诊断和复现脚本，没有修改生产构笔逻辑。现有 1,052 项核心与 Web 测试仍全部通过，说明这两类问题没有被现有断言完整覆盖。

核查版本为 `old-source-adjacent-strokes-v7`，配置指纹 `sha256:69de2abbe178bdc25cae555e72401d0d7cbe39d3df3963bdc9eb34cdcd7a057f`。本报告中的行情事实来自本地测试文件或明确列出的合成 OHLC；涉及缠论定义的依据仅来自本次实际读取的 `D:\缠论` 原文。

异常一：被保护的合格反向笔，下一次选择时仍会被吞掉（高优先级）

21 根无包含 K 线即可复现，上下镜像均发生。中心序号从 0 开始，分型端点为：

| 分型 | A 底 | B 顶 | C 底 | D 顶 | E 底 | F 顶 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 中心序号 | 1 | 5 | 9 | 13 | 15 | 19 |
| 端点价格 | 6 | 20 | 10 | 18 | 4 | 16 |

A→B、B→C、C→D 的中心间隔均为 4，并通过完整区间极值检查。D→E 的中心间隔只有 2，不构成旧笔。E→F 的中心间隔为 4。

| 已输入 K 线数量 | 实际输出 | 实际状态 |
| --- | --- | --- |
| 15 | A1→B5→C9→D13 | A→B、B→C 已标记确认；C→D 是末笔。 |
| 17 | A1→B5→C9→D13 | 新底 E15 已可见，程序明确执行 `retain_qualified_reversal`，保护 B→C→D。 |
| 21 | A1→B5→E15→F19 | 程序撤去 B→C、C→D，选入长边 B→E，B→C 原有的确认记录也被撤换。 |

![17 根与 21 根输入下的实际输出](D:/project/chanlun-pro/output/bi_code_review/v7_protected_reversal_case.png)

图中的灰色竖线是每根输入 K 线的高低区间，蓝线和红线是当前代码输出，虚线是先前保留的结构。完整 OHLC、上下镜像和三次快照在 [复现数据](D:/project/chanlun-pro/output/bi_code_review/v7_reproducible_cases.json)，[SVG 图](D:/project/chanlun-pro/output/bi_code_review/v7_protected_reversal_case.svg)可单独查看。

原因位于 [stroke_resolver.py](D:/project/chanlun-pro/src/chanlun/core/stroke_resolver.py:180)：保护条件只检查新分型的**直接前驱**是否位于当前路径较早的位置。E15 出现时，虽然拒绝立即选用 B5→E15，但节点仍保存了这个前驱关系。F19 出现后，其直接前驱是尚未选中的 E15，条件不再命中；沿 E15 的祖先回溯，又到达 B5，于是间接绕过保护。`_select` 随后截掉 B5 之后的旧路径。问题在于完整拟选路径与受保护关系不一致，不能只检查当前一步的直接前驱。

原文依据：[第 62 课正文 64–70 行](<D:/缠论/chanlun_lesson_corpus/L062_分型、笔与线段(2007-06-30094951).md:64>)要求相邻顶底；[第 65 课正文 166–214 行](<D:/缠论/chanlun_lesson_corpus/L065_再说说分型、笔、线段(2007-07-16221416).md:166>)区分把多笔当成一笔与转折过小的情形。这里使用作者正文，不使用该段 172–175 行的编者注释。将“不能用短促转折吞掉中间合格反向结构”转成完整路径校验，是实现推导；原文没有规定候选节点、位图或固定回退笔数。

本次指出的是已核定保护条件被自身后续分支绕过，**不据此臆造 E、F 出现后的唯一完整续接方案**，也不通过放宽旧笔间隔解决这个冲突。

这并非仅有合成样本：对四份行情逐根扫描，记录了 200 次原先标记确认的笔被撤换的更新事件。统计对象是“发生撤换的前缀更新次数”，不是独立问题类别数，也不能把每一次重选都直接认定为同一种原文违规。

| 本地样本 | 原始 K 线数 | 已确认笔撤换事件 | 单次最多撤换的已确认笔数 |
| --- | ---: | ---: | ---: |
| QQQ.US，30 分钟 | 819 | 2 | 2 |
| SH.600519，30 分钟 | 488 | 1 | 1 |
| SH.600519，5 分钟 | 12,072 | 40 | 8 |
| SZ.002299，1 分钟 | 21,954 | 157 | 8 |

SH.600519 的具体大范围案例发生在前缀由 5,639 根增加为 5,640 根时，时间为 **2025-11-21 11:30，UTC+8**：输出从 283 笔变为 276 笔，其中 8 条先前确认笔被撤换。其合并中心从 3893 开始的一串转折被替换为 3893→4011→4016。该事件的旧笔端点、确认时间、新输出和修订位置都保存在 [逐根撤换记录](D:/project/chanlun-pro/output/bi_code_review/v7_revision_probe.json)。它说明重选影响可以超过末笔，不能一概按“正常末笔延伸”向调用方解释。

异常二：同一已确认线段 ID 的确认时间被改写（高优先级）

使用实际 `CL` 入口、同一个对象，先输入 [QQQ.US 本地行情](D:/project/chanlun-pro/tests/fixtures/QQQ.US_30m.parquet) 的前 281 根，再增加到 282 根：

| 属性 | 前 281 根 | 前 282 根 |
| --- | --- | --- |
| 线段原始端点序号 | 14→85 | 14→85 |
| `is_done()` | `True` | `True` |
| `unit_id` | `9fcb4b75…5b62a269b` | 相同 |
| `locked_at` | 2026-03-20 21:30，UTC+8 | 2026-04-01 01:30，UTC+8 |
| `available_at` | 2026-03-20 21:30，UTC+8 | 2026-04-01 01:30，UTC+8 |

完整 ID 与两份快照在 [复现数据](D:/project/chanlun-pro/output/bi_code_review/v7_reproducible_cases.json) 的 `unit_identity`。后一次时间也与 282 根数据的冷启动计算一致，所以这不是单纯的旧缓存没有刷新。

上游笔重选改变了线段采用的确认依据；[CL 来源失效处理](D:/project/chanlun-pro/src/chanlun/core/cl.py:228)清理确认登记后，旧时间约束也被清除。与此同时，[单元身份构造](D:/project/chanlun-pro/src/chanlun/core/strict_structure/unit_adapter.py:166)只使用品种周期、价格口径、端点及区间等信息，没有区分这次重新选择的证据版本。结果是同一 ID 对应了两套确认时间。原有 [登记器约束](D:/project/chanlun-pro/src/chanlun/core/strict_structure/unit_adapter.py:26)本会拒绝同一 ID 的确认时间改变，但清理登记后这项检查不再能检测到跨修订变化。

影响：保存历史已确认单元、按 ID 去重或按首次可用时间回放的调用方，不能同时把这两份记录当成同一个不可变确认事实。当前计算能正常返回、与冷启动相同，仍不能说明这个身份与时间约定没有问题。

对 QQQ 全部 819 根、SH.600519 30 分钟全部 488 根、SH.600519 5 分钟前 5,000 根、SZ.002299 1 分钟前 5,000 根，共 11,307 个前缀进行 CL 下游扫描：出现 7 次更新事件涉及 8 条已确认线段的确认时间变化，未观察到已确认线段或正式中枢消失，也没有运行时异常。这个“未观察到”只限本次范围，不推广到所有输入。明细在 [下游核查](D:/project/chanlun-pro/output/bi_code_review/v7_downstream_probe.json)。

[第 69 课正文 151–184 行](<D:/缠论/chanlun_lesson_corpus/L069_月线分段与上海大走势分析、预判(2007-08-09.md:151>)明确区分暂时取舍与完成图形，第 160–166 行指出完成后不可再这样修改。当前代码把下一笔承接映射为 `confirmed/locked`，属于工程状态定义，不能直接声称等于该处原文的完成图形。即使保留“当前划分可重选”的接口语义，依赖证据的版本与首次确认时间也应一致处理；只清缓存并没有解决这一点。

为什么现有检查全部通过

1. [跨笔检查](D:/project/chanlun-pro/src/chanlun/core/stroke_audit.py:79)只寻找“从当前长边的同一起点到同一终点，可以完整连成至少三条合格边”的路径。异常一的 D13→E15 太近，这条同终点路径接不完，检查器便放行 B5→E15。它没有验证先前明确保护的 B5→C9→D13 是否被吞掉。
2. [逐根回放检查](D:/project/chanlun-pro/script/replay_old_strokes.py:41)遇到已确认笔被撤换时，只要求有 `revises_confirmed_selection` 记录。异常一有记录，所以通过；记录行为并不能证明取舍本身正确。
3. 批量和增量调用相同的选择规则，两者一致可以排除一部分缓存或增量实现错误，不能排除共同的规则错误。

本次重新执行既有核心与 Web 回归，结果 **1,052 项通过、0 项失败，63.65 秒**，见 [本次测试 XML](D:/project/chanlun-pro/output/bi_code_review/v7_case_audit_tests.xml)。没有删除既有断言或调整构笔结果来获得这个数字。

没有发现异常的检查范围

额外 150 组、每组 120 根带有宽 K 线、缺口、相等价的随机行情，共 18,000 个逐根前缀，进行 1,200 次冷启动比较；另有 150 组、每组 100 根行情，每根按高低范围逐步扩大的三个阶段更新，共 45,000 次实时更新及逐次冷启动比较。两组均未发现运行时异常、包含结果不一致、端点/资格/承接时间不一致，或现有区间与同终点跨笔检查报告的违规。结果分别在 [追加核查](D:/project/chanlun-pro/output/bi_code_review/v7_revision_probe.json)及 [实时更新核查](D:/project/chanlun-pro/output/bi_code_review/v7_intrabar_probe.json)。四份实际行情也核对了输入 OHLC 关系，无最高/最低价与开收盘价矛盾的记录。

这些阳性与阴性结果应同时保留：目前证据指向路径取舍和确认事实的约定问题，没有发现本次范围内的实时包含更新错误。仅以“零极值冲突”“逐根结果一致”或“测试全部通过”认定整体逻辑没有异常是不充分的。

复现方式

```powershell
python script/reproduce_old_stroke_anomalies.py --output output/bi_code_review/v7_reproducible_cases.json --figure output/bi_code_review/v7_protected_reversal_case.png
python script/probe_old_stroke_cases.py --seeds 150 --bars 100 --updates --output output/bi_code_review/v7_intrabar_probe.json
python script/probe_old_stroke_cases.py --seeds 150 --bars 120 --market tests/fixtures/QQQ.US_30m.parquet tests/fixtures/SH.600519_30m.parquet tests/fixtures/SH.600519_5m.parquet tests/fixtures/SZ.002299_1m.parquet --output output/bi_code_review/v7_revision_probe.json
python script/probe_old_stroke_cases.py --seeds 0 --downstream tests/fixtures/QQQ.US_30m.parquet tests/fixtures/SH.600519_30m.parquet tests/fixtures/SH.600519_5m.parquet tests/fixtures/SZ.002299_1m.parquet --limit 5000 --output output/bi_code_review/v7_downstream_probe.json
```

[定向复现脚本](D:/project/chanlun-pro/script/reproduce_old_stroke_anomalies.py)输出的 `protected_reversal_disappeared=True`、`static_audits_missed=True` 和 `identity_unchanged=True / confirmation_changed=True` 是观察到的问题，不是通过条件。脚本以成功退出表示完成诊断，不能将退出码 0 解读为被测算法没有异常。

修复顺序应先处理完整候选路径对合格反向结构的保护，再统一可重选状态与下游确认事实的版本约定，最后补充会拒绝上述反例的跨前缀断言。不能只禁止记录重选、清掉更多缓存，或退回“所有笔一直待定”来掩盖问题。
