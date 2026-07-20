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

from flask import Blueprint, Response, render_template, request
from flask_login import login_required
from werkzeug.utils import secure_filename

from chanlun.market import Market
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil
from chanlun.zixuan import ZiXuan


zixuan_bp = Blueprint("zixuan", __name__)

# 自选导入文件大小上限 1 MiB；自选条目通常 < 1 万行（每行约 30 字节），1 MiB 已远超合理上限。
_MAX_IMPORT_FILE_BYTES = 1 * 1024 * 1024
_MAX_IMPORT_REQUEST_BYTES = _MAX_IMPORT_FILE_BYTES + 64 * 1024
_MAX_GROUP_NAME_LENGTH = 64
_VALID_MARKETS = frozenset(market.value for market in Market)


def _normalize_group_name(value):
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _MAX_GROUP_NAME_LENGTH:
        return None
    if any(ord(char) < 32 or char in "/\\" for char in name):
        return None
    return name


def _build_stock_lookups(stocks):
    exact = {}
    aliases = {}
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        code = str(stock.get("code") or "").strip()
        if not code:
            continue
        item = {"code": code, "name": str(stock.get("name") or code)}
        exact[code] = item
        alias = code.rsplit(".", 1)[-1]
        previous = aliases.get(alias)
        if previous is None and alias not in aliases:
            aliases[alias] = item
        elif previous is not None and previous["code"] != code:
            aliases[alias] = None
    return exact, aliases


def _resolve_import_stock(raw_code, market, exact, aliases):
    code = raw_code.strip()
    if market == "a":
        code = code.replace("SHSE.", "SH.").replace("SZSE.", "SZ.")
    return exact.get(code) or aliases.get(code)


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
    # 直接走内存 Response，不落临时文件：导出内容仅几 KB；旧实现的
    # finally os.remove 会在 send_file 推流前执行，Windows 上文件被 send_file
    # 占用 → 删除失败 → zx_export_*.txt 永久泄漏。下载名经 secure_filename 防注入。
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

    zx = ZiXuan(market)
    if zx_group not in {group["name"] for group in zx.zixuan_list}:
        return {"ok": False, "msg": "自选组不存在"}, 400
    ex = get_exchange(Market(market))
    # cq singleton 的 default_market 会被后初始化市场覆盖, 必须显式传 market。
    from ..services.stock_list import _safe_all_stocks

    exact, aliases = _build_stock_lookups(_safe_all_stocks(ex, market))
    imported = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            import_infos = stripped.split(",", 1)
            resolved = _resolve_import_stock(import_infos[0], market, exact, aliases)
            if resolved is None:
                continue
            code = resolved["code"]
            name = import_infos[1].strip() if len(import_infos) == 2 else ""
            if code in imported:
                del imported[code]
            imported[code] = {"code": code, "name": name or resolved["name"]}
        except (KeyError, TypeError, ValueError):
            LogUtil.warning(f"zixuan import skip line: {stripped!r}")

    if imported:
        existing = [
            stock for stock in zx.zx_stocks(zx_group) if stock["code"] not in imported
        ]
        if not zx.replace_zx_stocks(zx_group, existing + list(imported.values())):
            return {"ok": False, "msg": "导入失败"}, 409

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

    return {"ok": res}
