"""
自选（自选组与股票）相关接口蓝图。

  - `/get_zixuan_groups/<market>`
  - `/get_zixuan_stocks/<market>/<group_name>`
  - `/get_stock_zixuan/<market>/<code>`
  - `/zixuan_group/<market>`
  - `/opt_zixuan_group/<market>`
  - `/zixuan_opt_export`
  - `/zixuan_opt_import`
  - `/set_stock_zixuan`
"""

import re

from flask import Blueprint, Response, current_app, render_template, request
from flask_login import login_required
from werkzeug.utils import secure_filename

from chanlun.market import Market
from chanlun.exchange import get_exchange, resolve_bounded_stock_info
from chanlun.tools.log_util import LogUtil
from chanlun.zixuan import ZiXuan
from ..services.trading_screening_scope import admit_explicit_validation_codes


zixuan_bp = Blueprint("zixuan", __name__)

# 自选导入文件大小上限 1 MiB；自选条目通常 < 1 万行（每行约 30 字节），1 MiB 已远超合理上限。
_MAX_IMPORT_FILE_BYTES = 1 * 1024 * 1024
_MAX_IMPORT_REQUEST_BYTES = _MAX_IMPORT_FILE_BYTES + 64 * 1024
_MAX_GROUP_NAME_LENGTH = 64
_MAX_BOUNDED_IMPORT_SYMBOLS = 20
_VALID_MARKETS = frozenset(market.value for market in Market)
_NORMALIZED_A_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")


def _notify_instrument_scope_changed(market: str) -> None:
    """Best-effort wake-up after a successful watchlist membership edit."""

    try:
        if market == "a":
            screening = current_app.extensions.get(
                "decision_support_trading_screening"
            )
            notify = getattr(screening, "notify_instrument_scope_changed", None)
            if callable(notify):
                notify()
        monitor = current_app.extensions.get("holding_group_monitor")
        refresh = getattr(monitor, "request_refresh", None)
        if callable(refresh):
            refresh()
    except Exception:
        # 持久化已经成功；轮询仍是保证正确性的回退机制，因此调度器瞬时失败
        # 不能把用户已成功的编辑变成 HTTP 500 响应。
        current_app.logger.warning(
            "live monitor wake-up failed after watchlist edit",
            exc_info=True,
        )


def _normalize_group_name(value):
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _MAX_GROUP_NAME_LENGTH:
        return None
    if any(ord(char) < 32 or char in "/\\" for char in name):
        return None
    return name


def _normalize_explicit_import_code(raw_code: str, market: str) -> str | None:
    """Normalize one user-provided code without expanding a market catalog."""

    code = str(raw_code or "").strip()
    if not code:
        return None
    if market != "a":
        return code
    code = code.upper()
    for source, target in (
        ("SHSE.", "SH."),
        ("SZSE.", "SZ."),
        ("BJSE.", "BJ."),
    ):
        if code.startswith(source):
            code = target + code[len(source) :]
            break
    if code.isdigit() and len(code) == 6:
        if code[0] in {"5", "6", "9"}:
            code = f"SH.{code}"
        elif code[0] in {"0", "1", "2", "3"}:
            code = f"SZ.{code}"
        elif code[0] in {"4", "8"}:
            code = f"BJ.{code}"
    return code if _NORMALIZED_A_CODE.fullmatch(code) else None


def _parse_bounded_import_rows(content: str, market: str):
    """Parse and admit explicit rows before any exchange/provider is opened."""

    parsed = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        import_infos = stripped.split(",", 1)
        code = _normalize_explicit_import_code(import_infos[0], market)
        if code is None:
            LogUtil.warning(f"zixuan import skip invalid code: {stripped!r}")
            continue
        name = import_infos[1].strip() if len(import_infos) == 2 else ""
        if code in parsed:
            del parsed[code]
        parsed[code] = name
    if not parsed:
        return ()
    admitted = admit_explicit_validation_codes(
        tuple(parsed),
        max_symbols=_MAX_BOUNDED_IMPORT_SYMBOLS,
    )
    return tuple((code, parsed[code]) for code in admitted)


@zixuan_bp.before_request
def _reject_invalid_market():
    if (
        request.endpoint == "zixuan.opt_zixuan_import"
        and request.content_length is not None
        and request.content_length > _MAX_IMPORT_REQUEST_BYTES
    ):
        return {"ok": False, "msg": "文件过大（>1MB）"}, 413
    market = (request.view_args or {}).get("market")
    if market is None:
        market = request.values.get("market")
    if market not in _VALID_MARKETS:
        return {"ok": False, "msg": "无效的市场"}, 400
    return None


@zixuan_bp.route("/get_zixuan_groups/<market>")
@login_required
def get_zixuan_groups(market):
    zx = ZiXuan(market)
    groups = zx.get_zx_groups()
    return groups


@zixuan_bp.route("/get_zixuan_stocks/<market>/<group_name>")
@login_required
def get_zixuan_stocks(market, group_name):
    group_name = _normalize_group_name(group_name)
    if group_name is None:
        return {"ok": False, "msg": "无效的自选组名"}, 400
    zx = ZiXuan(market)
    stock_list = zx.zx_stocks(group_name)
    return {"code": 0, "msg": "", "count": len(stock_list), "data": stock_list}


@zixuan_bp.route("/get_stock_zixuan/<market>/<code>")
@login_required
def get_stock_zixuan(market, code: str):
    code = code.replace("__", "/")  # 数字货币特殊处理
    zx = ZiXuan(market)
    zx_groups = zx.query_code_zx_names(code)
    return zx_groups


@zixuan_bp.route("/zixuan_group/<market>", methods=["GET"])
@login_required
def zixuan_group_view(market):
    zx = ZiXuan(market)
    zx_groups = zx.get_zx_groups()
    return render_template("zixuan.html", market=market, zx_groups=zx_groups)


@zixuan_bp.route("/opt_zixuan_group/<market>", methods=["POST"])
@login_required
def opt_zixuan_group(market):
    """
    操作自选组
    """
    opt = request.form.get("opt", "")
    zx_group = _normalize_group_name(request.form.get("zx_group"))
    if zx_group is None:
        return {"ok": False, "msg": "无效的自选组名"}, 400
    if opt not in {"ADD", "DEL"}:
        return {"ok": False, "msg": "无效的操作"}, 400
    zx = ZiXuan(market)
    if opt == "DEL":
        deleted = zx.del_zx_group(zx_group)
        if deleted:
            _notify_instrument_scope_changed(market)
        return {
            "ok": deleted,
            "group": zx_group,
            "msg": "自选分组已删除" if deleted else "默认分组不可删除或分组不存在",
        }
    created = zx.add_zx_group(zx_group)
    return {
        "ok": created,
        "group": zx_group,
        "msg": "自选分组已创建" if created else "分组已存在或名称不可用",
    }


@zixuan_bp.route("/zixuan_opt_export", methods=["GET"])
@login_required
def opt_zixuan_export():
    """
    导出自选组
    """
    market = request.args.get("market")
    zx_group = _normalize_group_name(request.args.get("zx_group"))
    if zx_group is None:
        return {"ok": False, "msg": "无效的自选组名"}, 400
    zx = ZiXuan(market)
    stock_list = zx.zx_stocks(zx_group)
    output = "".join(f"{s['code']},{s['name']}\n" for s in stock_list)
    # 导出内容只有几 KB，直接以内存响应发送；下载名经 secure_filename 处理。
    safe_name = secure_filename(f"zixuan_{zx_group}.txt") or "zixuan.txt"
    return Response(
        output,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@zixuan_bp.route("/zixuan_opt_import", methods=["POST"])
@login_required
def opt_zixuan_import():
    """
    导入自选
    """
    market = request.form.get("market", "")
    zx_group = _normalize_group_name(request.form.get("zx_group"))
    if market not in _VALID_MARKETS:
        return {"ok": False, "msg": "无效的市场"}
    if zx_group is None:
        return {"ok": False, "msg": "无效的自选组名"}, 400

    file = request.files.get("file")
    if file is None or not file.filename:
        return {"ok": False, "msg": "未上传文件"}

    payload = file.stream.read(_MAX_IMPORT_FILE_BYTES + 1)
    if len(payload) > _MAX_IMPORT_FILE_BYTES:
        return {"ok": False, "msg": "文件过大（>1MB）"}, 413
    try:
        content = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"ok": False, "msg": "文件必须使用 UTF-8 编码"}, 400

    try:
        import_rows = _parse_bounded_import_rows(content, market)
    except ValueError:
        return {
            "ok": False,
            "msg": f"单次导入最多允许 {_MAX_BOUNDED_IMPORT_SYMBOLS} 个显式代码",
        }, 400

    zx = ZiXuan(market)
    if zx_group not in {group["name"] for group in zx.zixuan_list}:
        return {"ok": False, "msg": "自选组不存在"}, 400
    imported = {}
    ex = get_exchange(Market(market)) if import_rows else None
    for code, imported_name in import_rows:
        try:
            resolved = resolve_bounded_stock_info(
                ex,
                code,
                fallback_name=imported_name,
                allow_code_fallback=True,
                fallback_when_missing=True,
            )
            if resolved is None:
                continue
            resolved_name = str(resolved.get("name") or "").strip()
            if not resolved_name:
                continue
            imported[code] = {
                "code": code,
                "name": imported_name or resolved_name,
            }
        except (KeyError, TypeError, ValueError):
            LogUtil.warning(f"zixuan import skip code: {code!r}")
        except Exception as exc:
            LogUtil.warning(
                f"zixuan import identity lookup failed code={code!r}: {exc}"
            )

    if imported:
        existing = [
            stock
            for stock in zx.zx_stocks(zx_group)
            if stock.get("market", market) == market
            and stock["code"] not in imported
        ]
        if not zx.replace_zx_stocks(zx_group, existing + list(imported.values())):
            return {"ok": False, "msg": "导入失败"}, 409
        _notify_instrument_scope_changed(market)

    return {"ok": True, "msg": f"成功导入 {len(imported)} 条记录"}


@zixuan_bp.route("/set_stock_zixuan", methods=["POST"])
@login_required
def set_stock_zixuan():
    market = request.form["market"]
    opt = request.form["opt"]
    group_name = _normalize_group_name(request.form.get("group_name"))
    if group_name is None:
        return {"ok": False, "msg": "无效的自选组名"}, 400
    code = request.form["code"]
    zx = ZiXuan(market)
    if opt == "DEL":
        res = zx.del_stock(group_name, code)
    elif opt == "ADD":
        res = zx.add_stock(group_name, code, None)
    elif opt == "COLOR":
        color = request.form["color"]
        res = zx.color_stock(group_name, code, color)
    elif opt == "SORT":
        direction = request.form["direction"]
        if direction == "top":
            res = zx.sort_top_stock(group_name, code)
        else:
            res = zx.sort_bottom_stock(group_name, code)
    else:
        res = False

    if res and opt in {"ADD", "DEL"}:
        _notify_instrument_scope_changed(market)

    return {"ok": res}
