# -*- coding: utf-8 -*-
"""节点（colo）参考数据。

数据源：内置静态快照（data_colos.py，Netrvin/cloudflare-colo-list 快照），
启动时可在线刷新（3 天 TTL，失败沿用快照）。
位置信息一律以 CF 响应头 cf-meta-* 为准，本模块只做 code → 国家/中文名 映射。
"""
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("FASTCF_HOME", str(Path.home() / ".fastcf")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 国内网络可设置 FASTCF_PROXY_BASE（自建 gh-proxy 前缀）插入到下载源最前面
_PROXY_BASE = os.environ.get("FASTCF_PROXY_BASE", "").rstrip("/")

# ISO 代码 → 中文国家名（覆盖 colo 参考库全部 cca2，未命中返回代码）
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
    "AL": "阿尔巴尼亚",
    "MZ": "莫桑比克", "KE": "肯尼亚", "NG": "尼日利亚", "GH": "加纳",
    "MA": "摩洛哥", "DZ": "阿尔及利亚", "TN": "突尼斯", "LY": "利比亚",
    "SD": "苏丹", "ET": "埃塞俄比亚", "UG": "乌干达", "TZ": "坦桑尼亚",
    "FJ": "斐济", "PG": "巴布亚新几内亚",
    "AO": "安哥拉", "BB": "巴巴多斯", "BF": "布基纳法索", "BN": "文莱",
    "BT": "不丹", "BW": "博茨瓦纳", "CD": "刚果(金)", "CI": "科特迪瓦",
    "CM": "喀麦隆", "DJ": "吉布提", "GD": "格林纳达", "GU": "关岛",
    "GY": "圭亚那", "HN": "洪都拉斯", "JM": "牙买加", "MG": "马达加斯加",
    "MU": "毛里求斯", "MV": "马尔代夫", "MW": "马拉维", "NA": "纳米比亚",
    "NC": "新喀里多尼亚", "PF": "法属波利尼西亚", "PR": "波多黎各",
    "PS": "巴勒斯坦", "RE": "留尼汪", "RW": "卢旺达", "SN": "塞内加尔",
    "SR": "苏里南", "TT": "特立尼达和多巴哥", "ZM": "赞比亚", "ZW": "津巴布韦",
}

def country_zh(code: str) -> str:
    """ISO 代码 → 中文国家名。"""
    if not code:
        return "未知"
    return COUNTRY_ZH.get(code.strip().upper(), code)


# ── CF 三字码头 → 中文数据中心名 ──
# 数据来自 data_colos.py（Netrvin/cloudflare-colo-list 快照，341 个 colo），
# 启动时后台可在线刷新（ensure_colo_data，3 天 TTL）。

COLO_DATA_PATH = DATA_DIR / "colo_data.json"
COLO_URLS = (
    ([f"{_PROXY_BASE}/https://raw.githubusercontent.com/Netrvin/cloudflare-colo-list/master/DC-Colos.json"]
     if _PROXY_BASE else []) + [
        "https://cdn.jsdelivr.net/gh/Netrvin/cloudflare-colo-list@master/DC-Colos.json",
        "https://raw.githubusercontent.com/Netrvin/cloudflare-colo-list/master/DC-Colos.json",
    ]
)

# 静态快照（离线兜底）：{code: (cca2, 中文名)}
from .data_colos import COLO_ZH as _STATIC_COLO_ZH

_colo: dict = {c: tuple(v) for c, v in _STATIC_COLO_ZH.items()}
_colo_lock = threading.Lock()

# 在线刷新时的城市中文映射
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
    "San Diego": "圣迭戈", "Salt Lake City": "盐湖城", "Anchorage": "安克雷奇",
    "Honolulu": "火奴鲁鲁", "Miami": "迈阿密", "Atlanta": "亚特兰大",
    "Charlotte": "夏洛特", "New York": "纽约", "Boston": "波士顿",
    "Philadelphia": "费城", "Washington": "华盛顿", "Chicago": "芝加哥",
    "Detroit": "底特律", "Tampa": "坦帕", "Nashville": "纳什维尔",
    "Memphis": "孟菲斯", "Kansas City": "堪萨斯城", "Columbus": "哥伦布",
    "Minneapolis": "明尼阿波利斯", "St. Louis": "圣路易斯",
    "Indianapolis": "印第安纳波利斯", "Cleveland": "克利夫兰", "Austin": "奥斯汀",
    "San Antonio": "圣安东尼奥", "Cape Town": "开普敦",
    "Johannesburg": "约翰内斯堡", "Durban": "德班",
    "London": "伦敦", "Paris": "巴黎", "Frankfurt": "法兰克福",
    "Amsterdam": "阿姆斯特丹", "Berlin": "柏林", "Hamburg": "汉堡",
    "Madrid": "马德里", "Barcelona": "巴塞罗那", "Rome": "罗马",
    "Milan": "米兰", "Athens": "雅典", "Thessaloniki": "塞萨洛尼基",
    "Istanbul": "伊斯坦布尔", "Brussels": "布鲁塞尔", "Dublin": "都柏林",
    "Copenhagen": "哥本哈根", "Stockholm": "斯德哥尔摩", "Oslo": "奥斯陆",
    "Helsinki": "赫尔辛基", "Reykjavik": "雷克雅未克", "Warsaw": "华沙",
    "Prague": "布拉格", "Vienna": "维也纳", "Zurich": "苏黎世",
    "Geneva": "日内瓦", "Lisbon": "里斯本", "Lyon": "里昂", "Marseille": "马赛",
    "Bordeaux": "波尔多", "Tbilisi": "第比利斯", "Yerevan": "埃里温",
    "Baku": "巴库", "Doha": "多哈", "Dubai": "迪拜", "Riyadh": "利雅得",
    "Jeddah": "吉达", "Tel Aviv": "特拉维夫", "Haifa": "海法", "Amman": "安曼",
    "Beirut": "贝鲁特", "Baghdad": "巴格达", "Muscat": "马斯喀特",
    "Manama": "麦纳麦", "Cairo": "开罗", "Lagos": "拉各斯", "Accra": "阿克拉",
    "Dakar": "达喀尔", "Nairobi": "内罗毕", "Kampala": "坎帕拉",
    "Addis Ababa": "亚的斯亚贝巴", "Tunis": "突尼斯", "Djibouti City": "吉布提市",
    "Port Louis": "路易港", "Kathmandu": "加德满都", "Dhaka": "达卡",
    "Colombo": "科伦坡", "Male": "马累", "Yangon": "仰光",
    "Phnom Penh": "金边", "Vientiane": "万象", "Auckland": "奥克兰",
    "Wellington": "惠灵顿", "Sydney": "悉尼", "Melbourne": "墨尔本",
    "Brisbane": "布里斯班", "Perth": "珀斯", "Adelaide": "阿德莱德",
    "Canberra": "堪培拉", "Hobart": "霍巴特",
    "Buenos Aires": "布宜诺斯艾利斯", "Santiago": "圣地亚哥", "Lima": "利马",
    "Bogota": "波哥大", "Quito": "基多", "Guayaquil": "瓜亚基尔",
    "Asuncion": "亚松森", "La Paz": "拉巴斯", "Kingston": "金斯敦",
    "San Juan": "圣胡安", "Guadalajara": "瓜达拉哈拉",
    "Vancouver": "温哥华", "Toronto": "多伦多", "Montreal": "蒙特利尔",
    "Calgary": "卡尔加里", "Winnipeg": "温尼伯", "Halifax": "哈利法克斯",
    "Moscow": "莫斯科", "Saint Petersburg": "圣彼得堡", "Minsk": "明斯克",
    "Pune": "浦那", "Ahmedabad": "艾哈迈达巴德", "Belgrad": "贝尔格莱德",
    "Kolkata": "加尔各答", "Delhi": "德里", "New Delhi": "新德里",
    "Luoyang": "洛阳", "Chengdu": "成都", "Chongqing": "重庆", "Harbin": "哈尔滨",
    "Dalian": "大连", "Nanjing": "南京", "Hangzhou": "杭州", "Tianjin": "天津",
    "Changsha": "长沙", "Kunming": "昆明", "Guiyang": "贵阳",
    "Fuzhou": "福州", "Zhengzhou": "郑州", "Jinan": "济南", "Lanzhou": "兰州",
    "Taiyuan": "太原", "Shijiazhuang": "石家庄", "Taipa": "氹仔", "Xingyi": "兴义",
}


def _colo_name_zh(v: dict) -> str:
    """参考库条目 → 中文名 '国·城市'。"""
    cc = (v.get("cca2") or "").upper()
    country = COUNTRY_ZH.get(cc, cc)
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


def colo_zh(code: str) -> str:
    """CF 三字码头 → 中文节点名（如 '中国·香港'、'美国·洛杉矶'）。未收录返回原码。"""
    if not code:
        return ""
    c = code.strip().upper()
    with _colo_lock:
        v = _colo.get(c)
    return v[1] if v else c


def colo_country(code: str):
    """CF 三字码头 → ISO alpha2 国家码。未收录返回 None。
    港澳台的 colo cca2 分别为 HK/MO/TW，可直接用于就近过滤。"""
    if not code:
        return None
    c = code.strip().upper()
    with _colo_lock:
        v = _colo.get(c)
    return v[0].upper() if v and v[0] else None


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


def colo_count() -> int:
    with _colo_lock:
        return len(_colo)


def ensure_colo_data(force=False) -> bool:
    """确保 colo 参考数据可用；可在线刷新（3 天 TTL）。
    在线失败时沿用静态快照，不视为错误。"""
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
            _download(url, COLO_DATA_PATH, min_size=1000)
            _load_colo_file(COLO_DATA_PATH)
            return True
        except Exception as e:
            log_event(f"  colo 数据刷新失败({e})，尝试下一个源...")
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


def _download(url, dest: Path, timeout=60, min_size=1000):
    """绕过代理直连下载（socket 级超时，防止慢速连接挂死）。"""
    import socket
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "FastCF/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with opener.open(req, timeout=timeout) as r:
            r.fp.raw._sock.settimeout(timeout)  # socket 读超时
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    size = tmp.stat().st_size
    if size < min_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载文件过小({size}B)，视为失败")
    os.replace(tmp, dest)


def preload(on_done=None):
    """后台预加载 colo 参考数据。"""
    def _work():
        ensure_colo_data()
        if on_done:
            on_done()
    import threading
    threading.Thread(target=_work, daemon=True).start()
