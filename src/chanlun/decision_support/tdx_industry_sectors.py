"""Map the local industry membership catalog to real TDX 880 index bars.

The mapping is deliberately explicit.  It never creates OHLC values from
constituent stocks: every returned ``kline_code`` is a native TongdaXin
industry-board index consumed by ``get_index_bars``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


_TDX_INDEX_NAMES: dict[str, str] = {
    "SH.880302": "煤炭开采",
    "SH.880303": "焦炭加工",
    "SH.880305": "电力",
    "SH.880310": "石油",
    "SH.880311": "石油开采",
    "SH.880312": "石油加工",
    "SH.880318": "钢铁",
    "SH.880319": "普钢",
    "SH.880320": "特种钢",
    "SH.880324": "有色",
    "SH.880328": "黄金",
    "SH.880329": "小金属",
    "SH.880330": "化纤",
    "SH.880335": "化工",
    "SH.880336": "化工原料",
    "SH.880337": "农药化肥",
    "SH.880338": "塑料",
    "SH.880339": "橡胶",
    "SH.880344": "建材",
    "SH.880346": "水泥",
    "SH.880347": "玻璃",
    "SH.880348": "其他建材",
    "SH.880350": "造纸",
    "SH.880351": "矿物制品",
    "SH.880355": "日用化工",
    "SH.880360": "农林牧渔",
    "SH.880361": "种植业",
    "SH.880362": "渔业",
    "SH.880363": "林业",
    "SH.880364": "饲料",
    "SH.880366": "农业综合",
    "SH.880368": "纺织",
    "SH.880369": "服饰",
    "SH.880372": "食品饮料",
    "SH.880375": "食品",
    "SH.880380": "酿酒",
    "SH.880387": "家用电器",
    "SH.880391": "汽车整车",
    "SH.880392": "汽车配件",
    "SH.880393": "汽车服务",
    "SH.880394": "摩托车",
    "SH.880398": "医疗保健",
    "SH.880399": "家居用品",
    "SH.880401": "化学制药",
    "SH.880402": "生物制药",
    "SH.880403": "中成药",
    "SH.880406": "商业连锁",
    "SH.880407": "百货",
    "SH.880410": "医药商业",
    "SH.880412": "商品城",
    "SH.880414": "商贸代理",
    "SH.880418": "传媒娱乐",
    "SH.880419": "出版业",
    "SH.880420": "影视音像",
    "SH.880421": "广告包装",
    "SH.880422": "文教休闲",
    "SH.880423": "酒店餐饮",
    "SH.880424": "旅游",
    "SH.880430": "航空",
    "SH.880431": "船舶",
    "SH.880432": "运输设备",
    "SH.880437": "通用机械",
    "SH.880440": "工业机械",
    "SH.880445": "专用机械",
    "SH.880446": "电气设备",
    "SH.880447": "工程机械",
    "SH.880448": "电器仪表",
    "SH.880452": "电信运营",
    "SH.880454": "水务",
    "SH.880455": "供气供热",
    "SH.880456": "环境保护",
    "SH.880459": "运输服务",
    "SH.880464": "仓储物流",
    "SH.880471": "银行",
    "SH.880472": "证券",
    "SH.880473": "保险",
    "SH.880474": "多元金融",
    "SH.880476": "建筑",
    "SH.880477": "建筑工程",
    "SH.880478": "装修装饰",
    "SH.880482": "房地产",
    "SH.880486": "房产服务",
    "SH.880489": "IT设备",
    "SH.880490": "通信设备",
    "SH.880491": "半导体",
    "SH.880492": "元器件",
    "SH.880493": "软件服务",
    "SH.880494": "互联网",
    "SH.880497": "综合类",
}


_INDUSTRY_TO_TDX: dict[str, str] = {
    "煤炭开采": "SH.880302",
    "焦炭加工": "SH.880303",
    "油气开采": "SH.880311",
    "石油化工": "SH.880312",
    "油服工程": "SH.880310",
    "日用化工": "SH.880355",
    "化纤": "SH.880330",
    "化学原料": "SH.880336",
    "化学制品": "SH.880335",
    "塑料": "SH.880338",
    "橡胶": "SH.880339",
    "农用化工": "SH.880337",
    "非金属材料": "SH.880351",
    "冶钢原料": "SH.880318",
    "普钢": "SH.880319",
    "特钢": "SH.880320",
    "工业金属": "SH.880324",
    "贵金属": "SH.880328",
    "能源金属": "SH.880329",
    "稀有金属": "SH.880329",
    "金属新材料": "SH.880324",
    "水泥": "SH.880346",
    "玻璃玻纤": "SH.880347",
    "装饰建材": "SH.880348",
    "种植业": "SH.880361",
    "养殖业": "SH.880360",
    "林业": "SH.880363",
    "渔业": "SH.880362",
    "饲料": "SH.880364",
    "农产品加工": "SH.880366",
    "动物保健": "SH.880398",
    "酿酒": "SH.880380",
    "饮料乳品": "SH.880372",
    "调味品": "SH.880375",
    "休闲食品": "SH.880375",
    "食品加工": "SH.880375",
    "纺织制造": "SH.880368",
    "服装家纺": "SH.880369",
    "饰品": "SH.880399",
    "造纸": "SH.880350",
    "包装印刷": "SH.880421",
    "家居用品": "SH.880399",
    "文娱用品": "SH.880422",
    "白色家电": "SH.880387",
    "黑色家电": "SH.880387",
    "小家电": "SH.880387",
    "厨卫电器": "SH.880387",
    "家电零部件": "SH.880387",
    "一般零售": "SH.880407",
    "商业物业经营": "SH.880412",
    "专业连锁": "SH.880406",
    "贸易": "SH.880414",
    "电子商务": "SH.880494",
    "乘用车": "SH.880391",
    "商用车": "SH.880391",
    "汽车零部件": "SH.880392",
    "汽车服务": "SH.880393",
    "摩托车及其他": "SH.880394",
    "化学制药": "SH.880401",
    "生物制品": "SH.880402",
    "中药": "SH.880403",
    "医药商业": "SH.880410",
    "医疗器械": "SH.880398",
    "医疗服务": "SH.880398",
    "医疗美容": "SH.880398",
    "电机制造": "SH.880446",
    "电池": "SH.880446",
    "电网设备": "SH.880446",
    "光伏设备": "SH.880446",
    "风电设备": "SH.880446",
    "其他发电设备": "SH.880446",
    "地面兵装": "SH.880432",
    "航空装备": "SH.880430",
    "航天装备": "SH.880430",
    "航海装备": "SH.880431",
    "军工电子": "SH.880489",
    "轨交设备": "SH.880432",
    "通用设备": "SH.880437",
    "专用设备": "SH.880445",
    "工程机械": "SH.880447",
    "自动化设备": "SH.880448",
    "半导体": "SH.880491",
    "消费电子": "SH.880492",
    "光学光电": "SH.880492",
    "元器件": "SH.880492",
    "其他电子": "SH.880492",
    "通信设备": "SH.880490",
    "通信工程": "SH.880490",
    "电信服务": "SH.880452",
    "IT设备": "SH.880489",
    "软件服务": "SH.880493",
    "云服务": "SH.880494",
    "产业互联网": "SH.880494",
    "游戏": "SH.880420",
    "广告营销": "SH.880421",
    "影视院线": "SH.880420",
    "数字媒体": "SH.880418",
    "出版业": "SH.880419",
    "广播电视": "SH.880420",
    "全国性银行": "SH.880471",
    "地方性银行": "SH.880471",
    "证券": "SH.880472",
    "保险": "SH.880473",
    "多元金融": "SH.880474",
    "房屋建设": "SH.880476",
    "基础建设": "SH.880477",
    "专业工程": "SH.880477",
    "工程咨询服务": "SH.880477",
    "装修装饰": "SH.880478",
    "房地产开发": "SH.880482",
    "房产服务": "SH.880486",
    "体育": "SH.880422",
    "教育培训": "SH.880422",
    "酒店餐饮": "SH.880423",
    "旅游": "SH.880424",
    "专业服务": "SH.880497",
    "公路铁路": "SH.880459",
    "航空机场": "SH.880459",
    "航运港口": "SH.880459",
    "物流": "SH.880464",
    "电力": "SH.880305",
    "燃气": "SH.880455",
    "水务": "SH.880454",
    "环保设备": "SH.880456",
    "环境治理": "SH.880456",
    "环境监测": "SH.880456",
    "综合类": "SH.880497",
}


def _valid_member_codes(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(
        sorted(
            {
                item
                for item in value
                if type(item) is str and len(item) == 6 and item.isdigit()
            }
        )
    )


def build_tdx_industry_sector_catalog(raw: object) -> dict[str, object]:
    """Return native TDX index codes plus the local stock memberships."""

    if not isinstance(raw, Mapping):
        raise TypeError("industry catalog must be a mapping")
    industries = raw.get("hy_codes")
    if not isinstance(industries, Mapping):
        raise TypeError("industry catalog must expose a hy_codes mapping")

    grouped: dict[str, dict[str, set[str]]] = {}
    unmapped: list[str] = []
    mapped_count = 0
    for raw_name, raw_members in industries.items():
        if type(raw_name) is not str or not raw_name.strip():
            continue
        name = raw_name.strip()
        members = _valid_member_codes(raw_members)
        if not members:
            continue
        kline_code = _INDUSTRY_TO_TDX.get(name)
        if kline_code is None:
            unmapped.append(name)
            continue
        mapped_count += 1
        group = grouped.setdefault(
            kline_code,
            {"members": set(), "industries": set()},
        )
        group["members"].update(members)
        group["industries"].add(name)

    sectors = [
        {
            "sector_id": f"tdx-industry:{kline_code}",
            "name": _TDX_INDEX_NAMES[kline_code],
            "kline_code": kline_code,
            "member_codes": sorted(values["members"]),
            "source_industries": sorted(values["industries"]),
        }
        for kline_code, values in sorted(grouped.items())
        if values["members"]
    ]
    return {
        "source": "tdx_880_industry_index",
        "mapped_industry_count": mapped_count,
        "unmapped_industries": sorted(unmapped),
        "sectors": sectors,
    }


__all__ = ("build_tdx_industry_sector_catalog",)
