# -*- coding: utf-8 -*-
"""地理归属解析模块。

数据源：ip2region 官方 xdb 数据文件（lionsoul2014/ip2region，纯静态文本库）
  * data/ip2region_v4.xdb  —— IPv4 城市级归属
  * data/ip2region_v6.xdb  —— IPv6 城市级归属
首次启动自动下载到本地缓存（~/.fastcf/），之后完全离线可用，
查询走本地前缀搜索（内存缓存全文件，10 微秒级）。

返回格式：国家|省份|城市|ISP|ISO-3166-1-alpha2
"""
import os
import threading
from pathlib import Path

from .ip2region import searcher, util

# ── 缓存路径 ──
DATA_DIR = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
XDB_V4_PATH = DATA_DIR / "ip2region_v4.xdb"
XDB_V6_PATH = DATA_DIR / "ip2region_v6.xdb"

# 下载源按优先级排列：jsDelivr CDN → GitHub raw。
# 国内网络可设置 FASTCF_PROXY_BASE（如自建 gh-proxy 前缀）插入到最前面。
_PROXY_BASE = os.environ.get("FASTCF_PROXY_BASE", "").rstrip("/")


def _urls(path: str) -> list:
    base = [f"{_PROXY_BASE}/https://raw.githubusercontent.com/lionsoul2014/ip2region/master/{path}"] if _PROXY_BASE else []
    return base + [
        f"https://cdn.jsdelivr.net/gh/lionsoul2014/ip2region@master/{path}",
        f"https://raw.githubusercontent.com/lionsoul2014/ip2region/master/{path}",
    ]


XDB_URLS = {
    "v4": _urls("data/ip2region_v4.xdb"),
    "v6": _urls("data/ip2region_v6.xdb"),
}

# ISO 代码 → 中文国家名（覆盖 ip2region 常见国家，未命中则返回代码）
COUNTRY_ZH = {
    "CN": "中国", "HK": "中国香港", "MO": "中国澳门", "TW": "中国台湾",
    "JP": "日本", "KR": "韩国", "SG": "新加坡", "MY": "马来西亚",
    "US": "美国", "GB": "英国", "DE": "德国", "FR": "法国", "NL": "荷兰",
    "AU": "澳大利亚", "NZ": "新西兰", "CA": "加拿大", "RU": "俄罗斯",
    "IN": "印度", "TH": "泰国", "VN": "越南", "ID": "印度尼西亚",
    "PH": "菲律宾", "BR": "巴西", "ZA": "南非", "AE": "阿联酋",
    "IL": "以色列", "TR": "土耳其", "EG": "埃及", "ES": "西班牙",
    "IT": "意大利", "CH": "瑞士", "SE": "瑞典", "NO": "挪威",
    "DK": "丹麦", "FI": "芬兰", "PL": "波兰", "CZ": "捷克",
    "AT": "奥地利", "BE": "比利时", "IE": "爱尔兰", "PT": "葡萄牙",
    "GR": "希腊", "RO": "罗马尼亚", "BG": "保加利亚", "HU": "匈牙利",
    "UA": "乌克兰", "BY": "白俄罗斯", "KZ": "哈萨克斯坦",
    "MN": "蒙古", "KH": "柬埔寨", "LA": "老挝", "MM": "缅甸",
    "NP": "尼泊尔", "BD": "孟加拉国", "LK": "斯里兰卡",
    "PK": "巴基斯坦", "IR": "伊朗", "SA": "沙特阿拉伯",
    "KW": "科威特", "QA": "卡塔尔", "BH": "巴林", "OM": "阿曼",
    "JO": "约旦", "LB": "黎巴嫩", "SY": "叙利亚", "IQ": "伊拉克",
    "AF": "阿富汗", "UZ": "乌兹别克斯坦", "TJ": "塔吉克斯坦",
    "KG": "吉尔吉斯斯坦", "GE": "格鲁吉亚", "AM": "亚美尼亚",
    "AZ": "阿塞拜疆", "AR": "阿根廷", "CL": "智利", "PE": "秘鲁",
    "CO": "哥伦比亚", "EC": "厄瓜多尔", "BO": "玻利维亚",
    "UY": "乌拉圭", "PY": "巴拉圭", "VE": "委内瑞拉",
    "MX": "墨西哥", "GT": "危地马拉", "CR": "哥斯达黎加",
    "PA": "巴拿马", "DO": "多米尼加", "CU": "古巴",
    "IS": "冰岛", "EE": "爱沙尼亚", "LV": "拉脱维亚", "LT": "立陶宛",
    "HR": "克罗地亚", "BA": "波黑", "RS": "塞尔维亚", "SI": "斯洛文尼亚",
    "SK": "斯洛伐克", "MD": "摩尔多瓦", "LU": "卢森堡", "MT": "马耳他",
    "CY": "塞浦路斯", "ME": "黑山", "MK": "北马其顿",
    "AL": "阿尔巴尼亚", "RS": "塞尔维亚", "MZ": "莫桑比克",
    "KE": "肯尼亚", "NG": "尼日利亚", "GH": "加纳", "MA": "摩洛哥",
    "DZ": "阿尔及利亚", "TN": "突尼斯", "LY": "利比亚",
    "SD": "苏丹", "ET": "埃塞俄比亚", "UG": "乌干达", "TZ": "坦桑尼亚",
    "FJ": "斐济", "PG": "巴布亚新几内亚",
    "TW": "中国台湾", "HK": "中国香港", "MO": "中国澳门",
}

# 参考库中 cca2 未收录在 COUNTRY_ZH 的补充
_COUNTRY_ZH_EXTRA = {
    "AO": "安哥拉", "BB": "巴巴多斯", "BF": "布基纳法索", "BN": "文莱",
    "BT": "不丹", "BW": "博茨瓦纳", "CD": "刚果(金)", "CI": "科特迪瓦",
    "CM": "喀麦隆", "DJ": "吉布提", "GD": "格林纳达", "GU": "关岛",
    "GY": "圭亚那", "HN": "洪都拉斯", "JM": "牙买加", "MG": "马达加斯加",
    "MU": "毛里求斯", "MV": "马尔代夫", "MW": "马拉维", "NA": "纳米比亚",
    "NC": "新喀里多尼亚", "PF": "法属波利尼西亚", "PR": "波多黎各",
    "PS": "巴勒斯坦", "RE": "留尼汪", "RW": "卢旺达", "SN": "塞内加尔",
    "SR": "苏里南", "TT": "特立尼达和多巴哥", "ZM": "赞比亚", "ZW": "津巴布韦",
}
COUNTRY_ZH.update(_COUNTRY_ZH_EXTRA)


# 省份/地区常见中文名（ip2region 国外用英文，这里做一层常用映射）
PROVINCE_ZH = {
    "California": "加利福尼亚", "New York": "纽约", "Florida": "佛罗里达",
    "Texas": "得克萨斯", "Washington": "华盛顿", "Oregon": "俄勒冈",
    "Virginia": "弗吉尼亚", "Illinois": "伊利诺伊", "Massachusetts": "马萨诸塞",
    "Georgia": "佐治亚", "North Carolina": "北卡罗来纳", "Pennsylvania": "宾夕法尼亚",
    "Ohio": "俄亥俄", "Michigan": "密歇根", "Arizona": "亚利桑那",
    "Nevada": "内华达", "Colorado": "科罗拉多", "Minnesota": "明尼苏达",
    "Maryland": "马里兰", "Tennessee": "田纳西", "Missouri": "密苏里",
    "New Jersey": "新泽西", "Alabama": "阿拉巴马", "Louisiana": "路易斯安那",
    "Kentucky": "肯塔基", "Wisconsin": "威斯康星", "Utah": "犹他",
    "Iowa": "艾奥瓦", "Nebraska": "内布拉斯加", "Oklahoma": "俄克拉何马",
    "Connecticut": "康涅狄格", "Indiana": "印第安纳", "Mississippi": "密西西比",
    "Kansas": "堪萨斯", "Arkansas": "阿肯色", "New Mexico": "新墨西哥",
    "Montana": "蒙大拿", "Idaho": "爱达荷", "Maine": "缅因",
    "Rhode Island": "罗德岛", "Delaware": "特拉华", "South Carolina": "南卡罗来纳",
    "Hawaii": "夏威夷", "Alaska": "阿拉斯加", "North Dakota": "北达科他",
    "South Dakota": "南达科他", "Wyoming": "怀俄明", "Vermont": "佛蒙特",
    "West Virginia": "西弗吉尼亚",
    # 加拿大
    "Ontario": "安大略", "British Columbia": "不列颠哥伦比亚",
    "Quebec": "魁北克", "Alberta": "艾伯塔", "Manitoba": "曼尼托巴",
    "Nova Scotia": "新斯科舍", "Saskatchewan": "萨斯喀彻温",
    "New Brunswick": "新不伦瑞克",
    # 欧洲
    "England": "英格兰", "Scotland": "苏格兰", "Wales": "威尔士",
    "Bavaria": "巴伐利亚", "Hesse": "黑森", "Berlin": "柏林",
    "Hamburg": "汉堡", "Saxony": "萨克森", "Baden-Wurttemberg": "巴登-符腾堡",
    "North Rhine-Westphalia": "北莱茵-威斯特法伦",
    "Paredes": "",
    # 其他
    "Queensland": "昆士兰", "New South Wales": "新南威尔士",
    "Victoria": "维多利亚", "Western Australia": "西澳大利亚",
    "Tasmania": "塔斯马尼亚", "South Australia": "南澳大利亚",
    "Auckland": "奥克兰", "Canterbury": "坎特伯雷",
    "Fukuoka": "福冈", "Osaka": "大阪", "Tokyo": "东京",
    "Hyogo": "兵库", "Saitama": "埼玉", "Kanagawa": "神奈川",
    "Seoul": "首尔", "Busan": "釜山", "Incheon": "仁川",
    "Delhi": "德里", "Maharashtra": "马哈拉施特拉", "Karnataka": "卡纳塔克",
    "Tamil Nadu": "泰米尔纳德", "Mumbai": "孟买",
    "Singapore": "新加坡",
}


def country_zh(code: str) -> str:
    """ISO 代码 → 中文国家名。"""
    if not code:
        return "未知"
    return COUNTRY_ZH.get(code.strip().upper(), code)


def province_zh(name: str) -> str:
    if not name:
        return ""
    return PROVINCE_ZH.get(name, name)


# 默认就近国家（用户默认在中国大陆）
DEFAULT_NEARBY = ["CN", "HK", "MO", "TW", "JP", "KR", "SG", "MY"]


def countries_zh(codes: list) -> str:
    return "、".join(country_zh(c) for c in codes)


# ── CF 三字码头 → 中文数据中心名 ──
# 数据来自 data_colos.py（Netrvin/cloudflare-colo-list 快照，341 个 colo），
# 可在启动时后台刷新（ensure_colo_data）。

COLO_DATA_PATH = DATA_DIR / "colo_data.json"
COLO_URLS = (
    ([f"{_PROXY_BASE}/https://raw.githubusercontent.com/Netrvin/cloudflare-colo-list/master/DC-Colos.json"]
     if _PROXY_BASE else []) + [
        "https://cdn.jsdelivr.net/gh/Netrvin/cloudflare-colo-list@master/DC-Colos.json",
        "https://raw.githubusercontent.com/Netrvin/cloudflare-colo-list/master/DC-Colos.json",
    ]
)

# 静态快照（离线兜底）：{code: [cca2, 中文名]}
from .data_colos import COLO_ZH as _STATIC_COLO_ZH

# 运行时 colo 表：{code: (cca2, 中文名)}，初始用静态快照，ensure_colo_data 可刷新
_colo: dict = {c: tuple(v) for c, v in _STATIC_COLO_ZH.items()}
_colo_lock = threading.Lock()


def colo_zh(code: str) -> str:
    """CF 三字码头 → 中文节点名（如 '中国·香港'、'美国·洛杉矶'）。未收录返回原码。"""
    if not code:
        return ""
    c = code.strip().upper()
    with _colo_lock:
        v = _colo.get(c)
    return v[1] if v else c


def colo_country(code: str):
    """CF 三字码头 → ISO alpha2 国家码（参考库 cca2）。未收录返回 None。
    港澳台的 colo 在参考库 cca2 分别为 HK/MO/TW，故直接可用于就近过滤。"""
    if not code:
        return None
    c = code.strip().upper()
    with _colo_lock:
        v = _colo.get(c)
    return v[0].upper() if v and v[0] else None


def colo_list() -> list:
    """当前可用 colo 列表：[{code, name, cca2, region}...]，按中文名排序。"""
    import json as _json
    region = {}
    try:
        raw = _json.loads(COLO_DATA_PATH.read_text(encoding="utf-8"))
        for c, v in raw.items():
            region[c.upper()] = v.get("region", "")
    except Exception:
        pass
    out = []
    with _colo_lock:
        for code, (cca2, name) in _colo.items():
            out.append({"code": code, "name": name, "cca2": cca2,
                        "region": region.get(code, "")})
    out.sort(key=lambda x: x["name"])
    return out


def colo_list_by_cc() -> dict:
    """按 ISO 国家码分组的 colo：{cc: [{code, name}, ...]}，组内按 code 排序。"""
    out: dict = {}
    with _colo_lock:
        for code, (cca2, name) in _colo.items():
            if cca2:
                out.setdefault(cca2, []).append({"code": code, "name": name})
    for cc in out:
        out[cc].sort(key=lambda x: x["code"])
    return dict(sorted(out.items()))


def ensure_colo_data(force=False) -> bool:
    """确保 colo 参考数据可用；可在线刷新（3 天 TTL）。

    成功（静态或在线）返回 True。在线刷新失败时沿用静态快照，不视为错误。
    """
    if not force:
        try:
            import time
            if COLO_DATA_PATH.exists() and time.time() - COLO_DATA_PATH.stat().st_mtime < 3 * 86400:
                return True
        except OSError:
            pass
    for url in COLO_URLS:
        try:
            log_event(f"刷新 colo 参考数据: {url}")
            _direct_download(url, COLO_DATA_PATH, min_size=1000)
            _load_colo_file(COLO_DATA_PATH)
            return True
        except Exception as e:
            log_event(f"  colo 数据刷新失败({e})，尝试下一个源...")
    # 在线失败：沿用静态快照
    if not force:
        return True
    return bool(_colo)


def _load_colo_file(path: Path):
    """从参考 JSON 加载 colo 表（cca2 + 中文名）。"""
    import json as _json
    raw = _json.loads(path.read_text(encoding="utf-8"))
    new = {}
    for code, v in raw.items():
        cca2 = (v.get("cca2") or "").upper()
        name = _colo_name_zh(v)
        if code and name:
            new[code.upper()] = (cca2, name)
    if new:
        with _colo_lock:
            _colo.update(new)
        log_event(f"  colo 数据已更新（{len(new)} 个节点）")


# 在线刷新时的城市中文映射（与 data_colos.py 静态快照一致）
_CITY_ZH = {
    "Hong Kong": "香港", "Macau": "澳门", "Taipei": "台北", "Tokyo": "东京",
    "Singapore": "新加坡", "Seoul": "首尔", "Shanghai": "上海", "Beijing": "北京",
    "Shenzhen": "深圳", "Guangzhou": "广州", "Osaka": "大阪", "Kyoto": "京都",
    "Sapporo": "札幌", "Fukuoka": "福冈", "Nagoya": "名古屋", "Daegu": "大邱",
    "Busan": "釜山", "Incheon": "仁川", "Kuala Lumpur": "吉隆坡", "Bangkok": "曼谷",
    "Jakarta": "雅加达", "Manila": "马尼拉", "Hanoi": "河内", "Mumbai": "孟买",
    "Bangalore": "班加罗尔", "Chennai": "金奈", "Hyderabad": "海得拉巴",
    "Los Angeles": "洛杉矶", "San Jose": "圣何塞", "San Francisco": "旧金山",
    "Seattle": "西雅图", "Portland": "波特兰", "Denver": "丹佛", "Dallas": "达拉斯",
    "Houston": "休斯敦", "Phoenix": "凤凰城", "Las Vegas": "拉斯维加斯",
    "San Diego": "圣迭戈", "Salt Lake City": "盐湖城", "Boise": "博伊西",
    "Anchorage": "安克雷奇", "Honolulu": "火奴鲁鲁", "Miami": "迈阿密",
    "Atlanta": "亚特兰大", "Charlotte": "夏洛特", "Orlando": "奥兰多",
    "New York": "纽约", "Jersey City": "泽西市", "Boston": "波士顿",
    "Philadelphia": "费城", "Washington": "华盛顿", "Arlington": "阿灵顿",
    "Chicago": "芝加哥", "Detroit": "底特律", "Tampa": "坦帕", "Nashville": "纳什维尔",
    "Memphis": "孟菲斯", "Kansas City": "堪萨斯城", "Louisville": "路易斯维尔",
    "Columbus": "哥伦布", "Cincinnati": "辛辛那提", "Minneapolis": "明尼阿波利斯",
    "St. Louis": "圣路易斯", "Indianapolis": "印第安纳波利斯", "Baltimore": "巴尔的摩",
    "Richmond": "里士满", "Cleveland": "克利夫兰", "St. Paul": "圣保罗",
    "Raleigh": "罗利", "Austin": "奥斯汀", "San Antonio": "圣安东尼奥",
    "Cape Town": "开普敦", "Johannesburg": "约翰内斯堡", "Durban": "德班",
    "London": "伦敦", "Paris": "巴黎", "Frankfurt": "法兰克福",
    "Frankfurt-am-Main": "法兰克福", "Amsterdam": "阿姆斯特丹", "Berlin": "柏林",
    "Hamburg": "汉堡", "Madrid": "马德里", "Barcelona": "巴塞罗那", "Rome": "罗马",
    "Milan": "米兰", "Turin": "都灵", "Naples": "那不勒斯", "Verona": "维罗纳",
    "Bologna": "博洛尼亚", "Florence": "佛罗伦萨", "Athens": "雅典",
    "Thessaloniki": "塞萨洛尼基", "Istanbul": "伊斯坦布尔", "Brussels": "布鲁塞尔",
    "Antwerp": "安特卫普", "Dublin": "都柏林", "Copenhagen": "哥本哈根",
    "Stockholm": "斯德哥尔摩", "Oslo": "奥斯陆", "Helsinki": "赫尔辛基",
    "Reykjavik": "雷克雅未克", "Warsaw": "华沙", "Krakow": "克拉科夫", "Prague": "布拉格",
    "Vienna": "维也纳", "Zurich": "苏黎世", "Geneva": "日内瓦", "Basel": "巴塞尔",
    "Lisbon": "里斯本", "Porto": "波尔图", "Lyon": "里昂", "Marseille": "马赛",
    "Nice": "尼斯", "Toulouse": "图卢兹", "Bordeaux": "波尔多",
    "Tbilisi": "第比利斯", "Yerevan": "埃里温", "Baku": "巴库",
    "Doha": "多哈", "Dubai": "迪拜", "Riyadh": "利雅得", "Jeddah": "吉达",
    "Tel Aviv": "特拉维夫", "Haifa": "海法", "Amman": "安曼", "Beirut": "贝鲁特",
    "Baghdad": "巴格达", "Kuwait City": "科威特城", "Muscat": "马斯喀特",
    "Manama": "麦纳麦", "Abu Dhabi": "阿布扎比", "Tehran": "德黑兰",
    "Cairo": "开罗", "Alexandria": "亚历山大", "Pretoria": "比勒陀利亚",
    "Lagos": "拉各斯", "Abidjan": "阿比让", "Accra": "阿克拉", "Dakar": "达喀尔",
    "Nairobi": "内罗毕", "Kampala": "坎帕拉", "Addis Ababa": "亚的斯亚贝巴",
    "Alger": "阿尔及尔", "Casablanca": "卡萨布兰卡", "Tunis": "突尼斯",
    "Tangier": "丹吉尔", "Tripoli": "的黎波里", "Sanaa": "萨那",
    "Aden": "亚丁", "Mogadishu": "摩加迪沙", "Djibouti City": "吉布提市",
    "Port Louis": "路易港", "Kathmandu": "加德满都", "Dhaka": "达卡",
    "Colombo": "科伦坡", "Male": "马累", "Yangon": "仰光",
    "Phnom Penh": "金边", "Vientiane": "万象", "Auckland": "奥克兰",
    "Wellington": "惠灵顿", "Sydney": "悉尼", "Melbourne": "墨尔本",
    "Brisbane": "布里斯班", "Perth": "珀斯", "Adelaide": "阿德莱德",
    "Canberra": "堪培拉", "Hobart": "霍巴特", "Suva": "苏瓦",
    "Port Moresby": "莫尔兹比港", "Noumea": "诺梅亚", "Papeete": "帕皮提",
    "Buenos Aires": "布宜诺斯艾利斯", "Santiago": "圣地亚哥", "Lima": "利马",
    "Bogota": "波哥大", "Caracas": "加拉加斯", "Guayaquil": "瓜亚基尔",
    "Quito": "基多", "Montevideo": "蒙得维的亚", "Asuncion": "亚松森",
    "La Paz": "拉巴斯", "Bridgetown": "布里奇敦", "Kingston": "金斯敦",
    "Port of Spain": "西班牙港", "San Juan": "圣胡安", "Panama City": "巴拿马城",
    "Tegucigalpa": "特古西加尔巴", "Guatemala City": "危地马拉城",
    "San Salvador": "圣萨尔瓦多", "Managua": "马那瓜",
    "Mexico City": "墨西哥城", "Guadalajara": "瓜达拉哈拉", "Monterrey": "蒙特雷",
    "Vancouver": "温哥华", "Toronto": "多伦多", "Montreal": "蒙特利尔",
    "Calgary": "卡尔加里", "Edmonton": "埃德蒙顿", "Winnipeg": "温尼伯",
    "Halifax": "哈利法克斯", "Victoria": "维多利亚", "Ottawa": "渥太华",
    "Quebec City": "魁北克城", "St. John's": "圣约翰斯",
    "Moscow": "莫斯科", "Saint Petersburg": "圣彼得堡", "Kazan": "喀山",
    "Minsk": "明斯克", "Kyiv": "基辅", "Odesa": "敖德萨",
    "Pune": "浦那", "Jaipur": "斋浦尔", "Lucknow": "勒克瑙",
    "Ahmedabad": "艾哈迈达巴德", "Belgrad": "贝尔格莱德",
    "Kolkata": "加尔各答", "Delhi": "德里", "New Delhi": "新德里",
    "Luoyang": "洛阳", "Chengdu": "成都", "Chongqing": "重庆", "Wuhan": "武汉",
    "Xian": "西安", "Xi'an": "西安", "Harbin": "哈尔滨", "Dalian": "大连",
    "Nanjing": "南京", "Hangzhou": "杭州", "Suzhou": "苏州", "Tianjin": "天津",
    "Changsha": "长沙", "Nanchang": "南昌", "Kunming": "昆明", "Guiyang": "贵阳",
    "Changchun": "长春", "Shenyang": "沈阳", "Hefei": "合肥", "Fuzhou": "福州",
    "Ningbo": "宁波", "Wenzhou": "温州", "Xiamen": "厦门", "Quanzhou": "泉州",
    "Zhengzhou": "郑州", "Jinan": "济南", "Xining": "西宁", "Yinchuan": "银川",
    "Lanzhou": "兰州", "Urumqi": "乌鲁木齐", "Lhasa": "拉萨",
    "Shijiazhuang": "石家庄", "Taiyuan": "太原", "Xuzhou": "徐州",
    "Taipa": "氹仔", "Xingyi": "兴义",
}

_EXTRA_COUNTRIES = {
    "AO": "安哥拉", "BB": "巴巴多斯", "BF": "布基纳法索", "BN": "文莱",
    "BT": "不丹", "BW": "博茨瓦纳", "CD": "刚果(金)", "CI": "科特迪瓦",
    "CM": "喀麦隆", "DJ": "吉布提", "GD": "格林纳达", "GU": "关岛",
    "GY": "圭亚那", "HN": "洪都拉斯", "JM": "牙买加", "MG": "马达加斯加",
    "MU": "毛里求斯", "MV": "马尔代夫", "MW": "马拉维", "NA": "纳米比亚",
    "NC": "新喀里多尼亚", "PF": "法属波利尼西亚", "PR": "波多黎各",
    "PS": "巴勒斯坦", "RE": "留尼汪", "RW": "卢旺达", "SN": "塞内加尔",
    "SR": "苏里南", "TT": "特立尼达和多巴哥", "ZM": "赞比亚", "ZW": "津巴布韦",
}


def _colo_name_zh(v: dict) -> str:
    """参考库条目 → 中文名 '国·城市'。"""
    cc = (v.get("cca2") or "").upper()
    country = COUNTRY_ZH.get(cc, _EXTRA_COUNTRIES.get(cc, cc))
    city = (v.get("city") or "").strip()
    city_zh = _CITY_ZH.get(city)
    if not city_zh:
        return f"{country}·{city}" if city else country
    # 去重：城市中文 == 国家中文/核心词，或互相包含
    core = country.rstrip("国")
    if city_zh == country or city_zh == core or city_zh in country or country in city_zh:
        return country
    return f"{country}·{city_zh}"


def log_event(msg):
    print(f"[{__import__('datetime').datetime.now().strftime('%H:%M:%S')}][geoip] {msg}", flush=True)


# ── 下载 ──

def _direct_download(url, dest: Path, timeout=120, min_size=100000):
    """绕过代理直连下载。min_size: 最小文件大小（字节），低于则视为失败。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "FastCF/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    tmp = dest.with_suffix(dest.suffix + ".part")
    with opener.open(req, timeout=timeout) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total > 1048576 and got % (4 << 20) < (1 << 16):
                log_event(f"  下载 {dest.name}: {got/1048576:.0f}/{total/1048576:.0f} MB")
    size = tmp.stat().st_size
    if size < min_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载文件过小({size}B)，视为失败")
    os.replace(tmp, dest)
    log_event(f"  {dest.name} 下载完成 ({size/1048576:.1f} MB)")


def ensure_xdb(version: str, force=False) -> bool:
    """确保指定版本(v4/v6)的 xdb 文件存在。返回是否可用。"""
    dest = XDB_V4_PATH if version == "v4" else XDB_V6_PATH
    if not force and dest.exists() and dest.stat().st_size > 100000:
        return True
    for url in XDB_URLS[version]:
        try:
            log_event(f"正在下载 ip2region {version} 数据: {url}")
            _direct_download(url, dest)
            return True
        except Exception as e:
            log_event(f"  失败({e})，尝试下一个源...")
    if dest.exists():
        log_event("下载失败，沿用旧版 xdb 数据")
        return True
    log_event(f"ip2region {version} 数据不可用（将跳过该版本的地理过滤）")
    return False


# ── 查询引擎 ──

class Ip2RegionDB:
    """线程安全的 ip2region xdb 查询封装（整文件内存缓存）。"""

    def __init__(self, version: str):
        self.version = util.IPv4 if version == "v4" else util.IPv6
        path = XDB_V4_PATH if version == "v4" else XDB_V6_PATH
        self._searcher = searcher.new_with_buffer(self.version, path.read_bytes())
        self._cache = {}
        self._lock = threading.Lock()

    def search(self, ip: str) -> str:
        """返回原始 region 字符串（国家|省份|城市|ISP|代码），未命中返回空串。"""
        with self._lock:
            if ip in self._cache:
                return self._cache[ip]
        try:
            region = self._searcher.search(ip) or ""
        except Exception:
            region = ""
        with self._lock:
            if len(self._cache) < 200000:
                self._cache[ip] = region
        return region

    def country_code(self, ip: str):
        """返回 ISO alpha2 国家代码，未命中返回 None。"""
        r = self.search(ip)
        if not r:
            return None
        parts = r.split("|")
        return parts[-1].strip().upper() if len(parts) >= 5 else None

    def location_zh(self, ip: str):
        """返回中文位置描述，如 '中国·广东省·深圳市' / 'United States·California·San Francisco'。"""
        r = self.search(ip)
        if not r:
            return ""
        parts = r.split("|")
        if len(parts) < 5:
            return r
        country, province, city, _isp, code = parts[0], parts[1], parts[2], parts[3], parts[4]
        cn = country_zh(code)
        prov = province_zh(province)
        seg = [cn]
        if prov:
            seg.append(prov)
        if city and city not in ("0", cn):
            seg.append(city)
        return "·".join(seg)


# ── 全局单例 ──

_dbs = {}
_dbs_lock = threading.Lock()


def get_db(version: str) -> Ip2RegionDB | None:
    """获取（或惰性创建）指定版本的查询库。文件不存在时返回 None。"""
    with _dbs_lock:
        if version in _dbs:
            return _dbs[version]
    if version not in ("v4", "v6"):
        return None
    path = XDB_V4_PATH if version == "v4" else XDB_V6_PATH
    if not path.exists() or path.stat().st_size < 100000:
        return None
    with _dbs_lock:
        if version not in _dbs:
            try:
                _dbs[version] = Ip2RegionDB(version)
            except Exception as e:
                log_event(f"xdb 加载失败({version}): {e}")
                return None
    return _dbs[version]


def preload(on_done=None):
    """后台预加载：下载（如缺）+ 建立内存索引。"""
    def _work():
        ok4 = ensure_xdb("v4")
        ok6 = ensure_xdb("v6")
        if ok4:
            get_db("v4")
        if ok6:
            get_db("v6")
        # 刷新 CF colo 参考数据（3 天 TTL；在线失败则沿用内置快照）
        ensure_colo_data()
        if on_done:
            on_done()
    threading.Thread(target=_work, daemon=True).start()
