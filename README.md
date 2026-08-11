# 缠论 Pro（chanlun-pro）

> 多市场「缠论」自动化分析平台 —— 覆盖 A股 / 港股 / 美股 / 期货 / 外汇 / 数字货币，内置 TradingView 实时图表、统一结构计算、严格选股、研究回放与盘中信号监控。

![Python](https://img.shields.io/badge/Python-3.10--3.13-blue)
![License](https://img.shields.io/badge/License-Apache%202.0-green)
![Web](https://img.shields.io/badge/Web-Flask%20%2B%20Tornado-orange)
![Charts](https://img.shields.io/badge/Charts-TradingView-informational)

---

## 简介

chanlun-pro 是一套以**缠论**为核心的行情分析与量化交易系统。它把分型、笔、线段、中枢、买卖点、背驰、走势类型等缠论要素实现为**可递归的多级别算法核心**，并围绕它构建了完整工具链：

- 基于 **TradingView Charting Library** 的 Web 实时图表（自定义 datafeed + SSE 服务端推送，缠论形态随行情实时刷新）；
- 覆盖 **8 大市场、十余种数据源**的统一行情适配层，一套配置切换数据源；
- **严格选股、候选分钟级复核、持仓买卖点监控与研究回放**共用同一套结构判定。

> ⚠️ 本项目仅供学习与研究使用，不构成任何投资建议。实盘交易涉及资金安全，请务必在充分测试与风控前提下谨慎使用（见文末[注意事项](#注意事项)）。

---

## 核心特性

- **递归多级别缠论核心**：分型 → 笔 → 线段 → 中枢 → 买卖点 → 背驰 → 走势类型，支持中枢升级、区间套、同级别分解、走势多样性等进阶结构，级别可递归向上封顶。
- **多市场统一适配**：A股 / 港股 / 美股 / 国内期货 / 外盘期货 / 外汇 / 数字货币（合约+现货），同一套接口切换券商与数据源。
- **Web 实时图表**：TradingView 专业图表 + 自定义 datafeed；SSE 服务端定时重算并推送，缠论笔/线段/中枢/买卖点随 K 线实时更新；各级别递归着色、可独立开关。
- **研究回放与严格选股**：冻结事实回放、固定区间评估、走势/买卖点选股与全量重建工具。
- **盘中实时监听**：候选池分钟级复核、持仓结构事件监控、定时调度与运行就绪度检查。
- **消息推送**：严格结构事件使用钉钉 Webhook，通用选股任务可选飞书（Lark）。

---

## 支持的市场与数据源

配置文件 `src/chanlun/config.py` 中按市场分别设置数据源适配器（`EXCHANGE_*`）：

| 市场 | 配置项 | 可选数据源 |
| --- | --- | --- |
| 沪深 A 股 | `EXCHANGE_A` | `tdx`（通达信）/ `qmt`（迅投）/ `baostock` / `futu`（富途）/ `cq`（长桥）/ `usmart`（盈立）/ `db` |
| 港股 | `EXCHANGE_HK` | `tdx_hk` / `futu` / `cq` / `usmart` / `db` |
| 美股 | `EXCHANGE_US` | `tdx_us` / `alpaca` / `polygon` / `ib`（盈透）/ `cq`（长桥）/ `usmart` / `db` |
| 国内期货 | `EXCHANGE_FUTURES` | `tq`（天勤）/ `tdx_futures` / `db` |
| 外盘期货 | `EXCHANGE_NY_FUTURES` | `tdx_ny_futures` / `db` |
| 外汇 | `EXCHANGE_FX` | `tdx_fx` / `cq` / `db` |
| 数字货币（合约） | `EXCHANGE_CURRENCY` | `binance` / `db` |
| 数字货币（现货） | `EXCHANGE_CURRENCY_SPOT` | `binance_spot` / `db` |

> `db` 表示读取本地数据库中的行情，适用于境外/网络受限、原数据源不可用的场景。

---

## 技术栈

| 分类 | 技术 |
| --- | --- |
| 语言 / 依赖管理 | Python 3.10–3.13、Poetry |
| 数据计算 | pandas、numpy、pyarrow、scipy、TA-Lib、MyTT |
| Web / 图表 | Flask + Tornado（单进程 WSGI）、TradingView Charting Library、SSE |
| 存储 | file_db（Parquet 本地列存）、SQLAlchemy + SQLite/MySQL、Redis（可选） |
| 行情 SDK | akshare、longbridge（长桥）、uSMART Open API、pytdx、ccxt、alpaca-py、polygon、ib-insync、futu-api、baostock、tqsdk（天勤） |
| 通知 | 钉钉 Webhook、lark-oapi（飞书，可选） |
| 券商行情适配 | 迅投 QMT（vendored `xtquant`） |

---

## 快速开始

### 环境要求

- Python **3.10 – 3.13**（当前环境为 3.10）
- Windows（`package/` 下提供 TA-Lib / pytdx 的本地 wheel；其它平台需自行安装 TA-Lib）

### 安装

**Windows 一键安装**：

```bat
windows_install.bat
```

它会依次执行：安装 Poetry → `poetry install` → 从模板生成配置 → 环境自检。

**手动安装（跨平台）**：

```bash
pip install poetry
poetry install                       # 仅核心依赖
# 按需安装可选市场/功能（extras）：
#   us / hk / usmart / cn-extra / futures / notify / charts / monitor / corpus
poetry install --extras us --extras hk
poetry install --extras usmart          # 盈立 Open API 的 RSA 签名依赖
poetry install --all-extras          # 一次装齐

# 生成配置文件
cp src/chanlun/config.py.demo src/chanlun/config.py   # Windows: copy
```

### 配置

编辑 `src/chanlun/config.py`（或在项目根 `.env` 中以 `KEY=VALUE` 覆盖敏感项）：

- **数据源**：按市场设置 `EXCHANGE_A` / `EXCHANGE_US` / … 及对应 API（通达信目录、长桥、富途、天勤、Alpaca、Polygon、盈透、币安等）。
- **Web**：`WEB_HOST`（示例配置默认 `127.0.0.1`）、`LOGIN_PWD`、`PRELOAD_MARKETS`（启动预加载的市场，默认 `a/hk/us`）。非回环监听另见下方安全部署要求。
- **存储**：`DB_TYPE`（`sqlite`/`mysql`）、`DATA_PATH`（默认 `~/.chanlun_pro`）、`REDIS_HOST`（可选）。
- **实时推送**：`ENABLE_SSE_PUSH`、`SSE_REFRESH_MS`（服务端重算+推送间隔，默认 8000ms）。
- **通知**：`CHANLUN_DINGTALK_*` 用于严格结构事件，`FEISHU_KEYS` 用于通用选股任务。

#### uSMART（盈立）行情源

先安装 `usmart` extra，再把需要使用盈立的市场配置为 `usmart`，例如
`EXCHANGE_HK = "usmart"`、`EXCHANGE_US = "usmart"`；A 股同理使用
`EXCHANGE_A = "usmart"`。凭证放在项目根 `.env`，不要写入源码：

```dotenv
USMART_CHANNEL=你的对接编号
USMART_PUBLIC_KEY=官网公钥（单行 base64 DER）
USMART_PRIVATE_KEY=官网私钥（单行 base64 DER）

# 二选一：直接提供仍有效的登录 token
USMART_TOKEN=

# 或让适配器自动登录获取 token
USMART_AREA_CODE=86
USMART_PHONE=你的手机号
USMART_LOGIN_PASSWORD=你的盈立登录密码
```

适配器提供证券列表、K 线、实时快照和市场状态，不开放交易下单。官方基础行情接口
对实时行情/K 线限制为每分钟 120 次、基础信息每分钟 20 次；历史 K 线还受账户近
30 天标的额度约束，详见 [uSMART Open API 文档](https://api-doc.usmart.sg/zh-cn/quote-base.html)。

### 运行

**Windows**：

```bat
windows_run.bat
```

**手动启动**：

```bash
# app.py 会在导入项目模块前初始化本地源码路径
poetry run python web/chanlun_chart/app.py
```

启动后默认访问 **http://127.0.0.1:9900**；可用 `CHANLUN_WEB_PORT` 改端口，`nobrowser` 参数或环境变量 `CHANLUN_NO_AUTO_OPEN=1` 可关闭自动打开浏览器。

#### Web 安全部署模式

- 本机使用：保持 `WEB_HOST=127.0.0.1`。此模式允许 HTTP，也允许不设置登录密码；`windows_run.bat` 和每日重启脚本在未显式设置环境变量时会采用该安全默认值。
- 非回环监听：应用仅在同时满足以下条件时启动：`CHANLUN_HTTPS=1`、`LOGIN_PWD`/`CHANLUN_LOGIN_PWD` 使用 Werkzeug 的 `scrypt:` 或 `pbkdf2:` 哈希、会话 Cookie 启用 `Secure`。
- `CHANLUN_HTTPS=1` 表示 TLS 已由可信反向代理终止；代理必须覆盖 `X-Forwarded-For`/`X-Real-IP`，并通过防火墙禁止客户端直连后端 9900 端口。

生成密码哈希：

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('替换为强密码'))"
```

PowerShell 启动示例（将哈希替换为上一步完整输出）：

```powershell
$env:CHANLUN_WEB_HOST = '0.0.0.0'
$env:CHANLUN_LOGIN_PWD = 'scrypt:...'
$env:CHANLUN_HTTPS = '1'
poetry run python web/chanlun_chart/app.py nobrowser
```

---

## 目录结构

```
chanlun-pro/
├─ src/
│  ├─ chanlun/                  # 主包
│  │  ├─ core/                  # 缠论核心算法（笔/线段/中枢/买卖点/背驰/走势类型/递归/区间套）
│  │  ├─ exchange/              # 行情数据源适配层（tdx/qmt/长桥/富途/binance/alpaca/…）
│  │  ├─ decision_support/      # 唯一严格策略、研究回放与证据契约
│  │  ├─ trading/、trader/      # 实时筛选所需的行情数据契约与在线适配器
│  │  ├─ xuangu/                # 共用严格结构逻辑的选股入口
│  │  ├─ persistence/           # 持久化（file_db Parquet / DB）
│  │  ├─ cl_utils/、tools/      # 缠论工具、日志与缓存工具
│  │  ├─ config.py.demo         # 配置模板（复制为 config.py）
│  │  └─ market.py、fun.py …    # 市场枚举、通用工具
│  ├─ xtquant/                  # 迅投 QMT SDK（vendored）
├─ web/chanlun_chart/
│  ├─ app.py                    # Web 入口（Flask + Tornado，默认端口 9900）
│  └─ cl_app/
│     ├─ blueprints/            # 路由：tv(图表)/zixuan(自选)/xuangu(选股)/bkgn(板块)/setting/…
│     ├─ services/、handlers/   # 图表缓存、SSE 推送、静态资源等
│     ├─ static/、templates/    # TradingView 图表库 + 自定义 datafeed + 页面
│     └─ *_tasks.py             # 预警/选股/其它定时任务
├─ tests/                       # pytest：core / exchange / trading_system / trader / web / xuangu / fixtures
├─ package/                     # TA-Lib / pytdx 本地 wheel
├─ pyproject.toml、poetry.lock  # Poetry 依赖
└─ windows_install.bat / windows_run.bat
```

---

## 缠论核心算法（`src/chanlun/core/`）

核心按缠论原文实现，处理链自底向上、可递归多级别：

1. **K 线包含处理** `kline_data_processor` / `cl_kline_process`
2. **分型 → 笔** `bi_calculator`
3. **线段** `xd_calculator`（特征序列 / 缺口 / 两种情况 / 终点回溯）
4. **中枢** `zs_calculator` / `zs_branch`（+ `zs_upgrade` 升级、`zs_diversity` 走势多样性）
5. **买卖点** `bs_point_calculator` + `bs1_branch` / `bs2_branch` / `bs3_branch`（一/二/三类买卖点）
6. **背驰** `beichi_calculator` / `beichi_nest`（面积 / 柱高 / 黄白线，MACD 见 `macd`、`macd_htf`）
7. **走势类型** `zslx_calculator` / `zslx_branch`
8. **递归多级别 / 区间套** `recursive_branch` / `recursive_calculator` / `interval_nest` / `xiaozhuanda_branch`（小转大）

入口 `cl.py`（`CL` 类），类型定义在 `cl_interface.py` 与 `core/types/`。算法附有大量单测与「全量 == 增量」对拍守护网（见 `tests/core`）。

---

## 主要功能模块

- **Web 图表**（`web/chanlun_chart`）：TradingView 实时看盘，缠论多级别叠加渲染、SSE 实时推送、自选、选股、板块与运行状态页面。
- **研究回放**（`decision_support/trading_system/backtest`、`tools`）：冻结事实回放、固定年度评估、walk-forward 验证与全量重建。
- **选股**（`xuangu`、`cl_app/xuangu_tasks.py`）：所有三类买卖点与背驰判断统一进入严格结构实现。
- **盘中监控**（`cl_app/services/trading_screening.py`、`holding_group_monitor.py`）：候选池分钟级复核、持仓结构事件监听与通知。

---

## 测试

```bash
poetry run pytest                     # 全量
poetry run pytest tests/core -q       # 缠论核心
poetry run pytest tests/web -q        # Web 服务
```

前端 datafeed / 图表逻辑另有 Node 单测（`web/chanlun_chart/cl_app/static/js/__tests__/`）：

```bash
node --test web/chanlun_chart/cl_app/static/js/__tests__/<file>.test.js
```

CI 见 `.github/workflows/`（`ci.yml` 跑 Poetry 安装 + pytest，`codeql.yml` 做代码扫描）。

---

## 注意事项

- **单进程架构**：Web 服务以 `s.start(1)` 单进程运行是**刻意设计**——所有图表缓存、per-key 锁、数据源单例都是进程内内存，多进程会让缓存与锁全部失效。扩容请用反向代理 + 多端口，或先把缓存迁到 Redis。
- **访问鉴权**：本机模式使用回环监听；非回环模式必须通过 HTTPS 反向代理访问，并配置 Werkzeug 密码哈希。任一条件缺失时应用会拒绝启动。
- **数据源配额**：长桥（cq）按订阅级别限制每月可查询的历史 K 线 symbol 数量，相关防御见 `LB_QUOTA_MONTHLY_LIMIT` / `US_HISTORY_KLINE_SOURCE` / `US_PREWARM_ZIXUAN_ONLY`。
- **信号边界**：当前仓库不包含自动下单执行器；选股、回放与盘中提示均只输出研究信号。**本项目不对任何交易结果负责。**

---

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源。
