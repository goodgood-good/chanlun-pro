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

import csv
import io
import re

from flask import Blueprint, Response, render_template, request
from flask_login import login_required
from werkzeug.utils import secure_filename

from chanlun.market import Market
from chanlun.exchange import get_exchange, resolve_bounded_stock_info
from chanlun.zixuan import ZiXuan
from chanlun.screening.markets import validate_symbol
from ..services.symbol_scope import admit_explicit_validation_codes


zixuan_bp = Blueprint("zixuan", __name__)

# 自选导入文件大小上限 1 MiB；自选条目通常 < 1 万行（每行约 30 字节），1 MiB 已远超合理上限。
_MAX_IMPORT_FILE_BYTES = 1 * 1024 * 1024
_MAX_IMPORT_REQUEST_BYTES = _MAX_IMPORT_FILE_BYTES + 64 * 1024
_MAX_GROUP_NAME_LENGTH = 64
_MAX_BOUNDED_IMPORT_SYMBOLS = 20
_VALID_MARKETS = frozenset(market.value for market in Market)
_NORMALIZED_A_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")




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
        if code.startswith("920"):
            code = f"BJ.{code}"
        elif code[0] in {"5", "6", "9"}:
            code = f"SH.{code}"
        elif code[0] in {"0", "1", "2", "3"}:
            code = f"SZ.{code}"
        elif code[0] in {"4", "8"}:
            code = f"BJ.{code}"
    return code if _NORMALIZED_A_CODE.fullmatch(code) else None


def _parse_bounded_import_rows(content: str, market: str):
    """Read standard market/code/name CSV or legacy code/name, before I/O."""
    parsed, issues, skipped = {}, [], 0
    reader = csv.reader(io.StringIO(content, newline=""), strict=True)
    header, first = False, True
    for fields in reader:
        if not fields or all(not value.strip() for value in fields):
            continue
        if first and [value.strip().lower() for value in fields] == ["market", "code", "name"]:
            header, first = True, False
            continue
        first = False
        line = reader.line_num
        if header:
            row_market = fields[0].strip().lower() if fields else ""
            raw_code = fields[1] if len(fields) > 1 else ""
            name = fields[2].strip() if len(fields) > 2 else ""
            shape_ok = len(fields) == 3
        else:
            row_market, raw_code = market, fields[0]
            # Old exports did not quote commas inside names. Preserve their
            # first-comma split while accepting properly quoted legacy CSV.
            name = ",".join(fields[1:]).strip()
            shape_ok = True
        code = _normalize_explicit_import_code(raw_code, row_market)
        reason = ("列数不符合 market,code,name 或旧版 code,name" if not shape_ok else
                  "市场无效" if row_market not in _VALID_MARKETS else
                  "标的代码无效" if not code or not validate_symbol(row_market, code) else
                  "标的名称超过 100 个字符" if len(name) > 100 else None)
        identity = row_market, code
        if not reason and identity in parsed:
            reason = "重复标的，保留文件中最后一条"
        if reason:
            skipped += 1
            if len(issues) < 50:
                issues.append({"line": line, "market": row_market[:20], "code": str(raw_code)[:80], "reason": reason})
            if identity not in parsed or not reason.startswith("重复"):
                continue
        parsed[identity] = {"market": row_market, "code": code, "name": name, "line": line}
        admit_explicit_validation_codes(
            [f"{m}:{c}" for m, c in parsed], max_symbols=_MAX_BOUNDED_IMPORT_SYMBOLS)
    return list(parsed.values()), issues, skipped


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
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("market", "code", "name"))
    writer.writerows((s.get("market", market), s["code"], s["name"]) for s in stock_list)
    safe_name = secure_filename(f"zixuan_{zx_group}.csv") or "zixuan.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
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
        import_rows, issues, skipped = _parse_bounded_import_rows(content, market)
    except csv.Error:
        return {"ok": False, "msg": "CSV 格式错误，请检查引号与列分隔"}, 400
    except ValueError:
        return {
            "ok": False,
            "msg": f"单次导入最多允许 {_MAX_BOUNDED_IMPORT_SYMBOLS} 个显式代码",
        }, 400

    zx = ZiXuan(market)
    if zx_group not in {group["name"] for group in zx.zixuan_list}:
        return {"ok": False, "msg": "自选组不存在"}, 400
    imported, exchanges = [], {}
    for row in import_rows:
        row_market, code, imported_name = row["market"], row["code"], row["name"]
        try:
            if row_market not in exchanges:
                try:
                    exchanges[row_market] = get_exchange(Market(row_market))
                except Exception:
                    exchanges[row_market] = None
            if exchanges[row_market] is None:
                raise ValueError("identity source unavailable")
            resolved = resolve_bounded_stock_info(
                exchanges[row_market],
                code,
                fallback_name=imported_name,
                allow_code_fallback=True,
                fallback_when_missing=True,
            )
            resolved_name = str((resolved or {}).get("name") or "").strip()
            if not resolved_name:
                raise ValueError("identity unavailable")
            imported.append({"market": row_market, "code": code, "name": imported_name or resolved_name[:100]})
        except Exception:
            skipped += 1
            if len(issues) < 50:
                issues.append({"line": row["line"], "market": row_market, "code": code,
                               "reason": "标的身份暂不可核验，未导入；请稍后重试"})
    counts = {"added": 0, "updated": 0}
    if imported:
        try:
            counts = zx.merge_zx_stocks(zx_group, imported)
        except Exception:
            return {"ok": False, "msg": "导入保存失败，本次未写入任何标的，请重试"}, 409
    return {"ok": bool(imported), "msg": f"导入 {len(imported)} 条：新增 {counts['added']}、更新 {counts['updated']}；跳过 {skipped} 条",
            "imported": len(imported), **counts, "skipped": skipped, "issues": issues,
            "issues_truncated": skipped > len(issues)}


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
