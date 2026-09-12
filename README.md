# 缠论 Pro

多市场行情与缠论图表分析系统。当前版本保留行情、自选、分型、笔、线段、本周期中枢、MACD 与图表画线。

按本次精简范围，研究审计、模拟与回测、走势类型、混合级别分解、主划分择优、未完成走势多方案、原文加工与检索、电子书转换和清理工具已经移除。依赖这些算法的选股、买卖点、背驰及信号通知不在当前交付范围内。

## 使用

Windows 已配置环境可直接运行：

```powershell
.\windows_run.bat
```

也可在项目根目录启动：

```powershell
poetry run python web/chanlun_chart/app.py
```

默认地址：<http://127.0.0.1:9900>。登录后可切换标的和周期，管理跨市场自选，查看或隐藏笔、线段和中枢，并保存个人布局与手动画线。中枢区间表按需展开。

默认使用单周期图表。1m、5m、30m 分别使用自己的 K 线和线段计算本周期中枢，不再提供跨周期递归叠加。首次打开标的先显示 K 线，再补齐结构；后续请求复用缓存。

## 安装和配置

支持 Python 3.10–3.13。Windows 可运行 `windows_install.bat`，或手动安装：

```powershell
pip install poetry
poetry install
# 按数据源需要选择额外 SDK
poetry install --extras hk --extras usmart
```

可选依赖仅保留 `hk`、`usmart`、`cn-extra`、`futures`。开发验证工具位于 Poetry 的 `dev` 依赖组。`package/` 中的本地 wheel 用于 Windows 环境。

首次配置时，从 `src/chanlun/config.py.demo` 复制生成 `src/chanlun/config.py`；已有配置无需覆盖。数据源与存储位置在配置文件中设置，账号、API 凭证等私密值使用项目根目录 `.env`。

- `CHANLUN_LOGIN_USERS`：用户名到密码哈希的 JSON 对象，可用 `script/generate_web_password_hash.py` 生成哈希。
- `CHANLUN_WEB_HOST`、`CHANLUN_WEB_PORT`：监听地址和端口，默认回环地址、9900。
- `EXCHANGE_A`、`EXCHANGE_US` 等：各市场行情适配器。
- `DATA_PATH`、`DB_TYPE`：行情缓存和持久化数据位置。
- QMT、长桥、盈立、富途等行情源需要相应客户端或有效凭证。QMT 行情客户端由 `ops/manage_qmt_runtime.ps1` 独立管理。

使用 QMT 运行维护脚本前，将 `ops/qmt_exe_path.example.txt` 复制为 `ops/qmt_exe_path.txt`，填写本机快捷方式所指向的 `XtItClient.exe` 完整路径。本机配置、开发通知配置和运行数据不纳入代码提交。

## 保留的功能

| 功能 | 说明 |
| --- | --- |
| 行情图表 | K 线、成交量、MACD、周期切换与历史加载 |
| 缠论结构 | 分型、笔、线段、本周期线段中枢 |
| 自选 | 跨市场分组、增删、排序、标色、行情与涨跌幅 |
| 标的与板块 | 缓存目录搜索、完整代码查询、板块成分查看 |
| 图表工作区 | 默认单图，可切布局；显示开关、手动画线、账号偏好存储 |
| 运行维护 | 登录、健康检查、缓存失效、受控重启与进程守护 |

行情适配层覆盖 A 股、港股、美股、国内外期货、外汇及数字货币，实际可用周期、历史长度和实时权限取决于选用的数据源。

当前系统按需计算已打开的图表，不启动全市场选股或预先构建选股。启动标的目录默认保持小范围；手动输入完整 A 股代码可以查询单只标的，不触发全市场枚举。

完整功能和代码说明见 [系统说明](docs/system_overview.md)。

## 验证

```powershell
poetry run pytest -q
$chartTests = Get-ChildItem web/chanlun_chart/cl_app/static/js/__tests__ -Filter *.test.js
node --test $chartTests.FullName
poetry run ruff check src/ web/ tests/
```

测试覆盖基础结构、中枢物理角色、真实历史样本、缓存及增量一致性、页面数据传输、自选、登录和启动维护。删除功能对应的用例、失效夹具及旧压缩资源已清理。保留的回归测试用于防止空白图表、错位中枢和缓存串标的。

前端数据传输源码位于 `web/chanlun_chart/cl_app/static/datafeeds/udf/src`。修改后在 `web/chanlun_chart/cl_app/static/datafeeds/udf` 中执行 `npm ci` 和 `npm run build`，提交同步生成的 `dist/bundle.js`。
