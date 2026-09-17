"""高德 Web 服务客户端（seed 补坐标；周边餐饮搜索为遗留能力，当前无调用方）。

坐标系：高德返回的 location 就是 GCJ-02，可以直接入库、直接画图，不需要转换。

配额（高德 2026 控制台口径）：
- **基础 LBS**：地理编码/逆地理编码/路径规划 —— **15 万/月**
- **POI 搜索**：place text / place around —— **5 千/月** ⚠️ 易触顶

对应到这个文件：
- search_address()  地理编码（geocode/geo），主路径 —— 用 15 万配额
- search_poi()      关键字搜索（place/text），仅作 search_address 失败的兜底
- search_around()   周边搜索（place/around），历史遗留，当前无调用方
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("triptide.amap")

# 高德 POI 分类码：050000 = 餐饮服务
TYPE_RESTAURANT = "050000"
TYPE_SCENIC = "110000"

_client: httpx.Client | None = None


class AmapQuotaExceeded(RuntimeError):
    """高德配额 / 限流用完 —— 必须**显式抛出并中止整轮**，不能静默降级。

    ⚠️ 以前配额用完和「真的搜不到」返回的是同一个 `None`，
    `search_poi` 一律打「检索不到「X」」，于是 seed 会把景点一个个跳过，
    最后只报一句「写入 N 个景点」—— 看上去像数据问题，实际是额度没了。
    现在改成抛这个异常，并带**熔断**：额度确认没了之后不再打网络，
    免得继续发几百个注定失败的请求。
    """


# 熔断开关 —— 两个接口独立计数
# ⚠️ 用户 2026-09-17 反馈：「基础搜索服务的关键字搜索」是 **5 千/月**的 POI 配额，
# 而**地理编码**走的是 **15 万/月**的基础 LBS —— 把主路径切到地理编码，
# POI 只作兜底，两个熔断独立，避免「POI 用完了」误把地理编码也停了。
_QUOTA_GONE_GEO = False   # geocode/geo（地理编码，15 万/月）
_QUOTA_GONE_POI = False    # place/text（关键字搜索，5 千/月）

# 「额度耗尽」类错误码 —— 日/月配额型，触发即熔断
# 10003 DAILY_QUERY_OVER_LIMIT  日配额（每天 0 点重置）
# 10044 USER_DAILY_QUERY_OVER_LIMIT  账号维度日配额
# 40000 QUOTA_PLAN_RUN_OUT  余额耗尽
QUOTA_INFOCODES = {"10003", "10044", "40000"}
# QPS 类错误（高频但不耗月配额）—— 触发**单次短退避**重试一次，不熔断
QPS_INFOCODES = {"10004", "10014", "10015", "10019", "10020", "10021"}
QUOTA_HINTS = ("CUQPS", "QUOTA", "LIMIT", "OVER_LIMIT", "超出", "超限", "已达上限", "上限", "耗尽")


def is_quota_error(status: str = "", infocode: str = "", info: str = "") -> bool:
    """「额度耗尽」类（月/日配额 / 余额耗尽）—— 纯函数，好测。"""
    if infocode.strip() in QUOTA_INFOCODES:
        return True
    blob = f"{info} {status}".upper()
    return any(h in blob for h in QUOTA_HINTS)


def is_qps_error(status: str = "", infocode: str = "", info: str = "") -> bool:
    """「高频瞬时限流」类 —— 短暂退避重试即可，不熔断。"""
    if infocode.strip() in QPS_INFOCODES:
        return True
    blob = f"{info} {status}".upper()
    return "QPS" in blob or "频繁" in blob


def quota_gone() -> bool:
    """任意一类用完都算 true —— 用于最外层快速 fail-fast。"""
    return _QUOTA_GONE_GEO or _QUOTA_GONE_POI


def quota_gone_geo() -> bool: return _QUOTA_GONE_GEO
def quota_gone_poi() -> bool: return _QUOTA_GONE_POI


def reset_quota_breakers() -> None:
    """测试用：手动复位两个熔断（开发联调时偶尔也会用到）。"""
    global _QUOTA_GONE_GEO, _QUOTA_GONE_POI
    _QUOTA_GONE_GEO = False
    _QUOTA_GONE_POI = False


def _set_quota_breaker(kind: str) -> None:
    """只置熔断、不抛 —— 测试要能在不抛的情况下断言熔断状态。"""
    global _QUOTA_GONE_GEO, _QUOTA_GONE_POI
    if kind == "geo":
        _QUOTA_GONE_GEO = True
    else:
        _QUOTA_GONE_POI = True


def _trip_quota(kind: str, info: str) -> None:
    """置上熔断并抛出（不再发对应接口的请求）。

    `kind` ∈ {"geo", "poi"} —— 只熔断对应那一路，另一路继续可用。
    """
    _set_quota_breaker(kind)
    raise AmapQuotaExceeded(
        "高德" + ("地理编码" if kind == "geo" else "POI 搜索") +
        "配额已用完（" + (info or "未知原因") + "）。\n"
        "   · 日配额是**每天 0 点重置**，月配额去高德控制台换 key 或提额。\n"
        "   · 已有的景点都带 coord_source=amap，会**直接信任、不再打接口**，\n"
        "     所以用 `--source offline`（或 `--no-amap`）重灌老目的地是**不受影响**的。\n"
        "   · 但**新目的地必须补坐标**，额度没恢复之前别灌。")


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=settings.amap_timeout)
    return _client


def _parse_location(location: str) -> tuple[float, float] | None:
    """"lng,lat" → (lat, lng)。"""
    if not location or "," not in location:
        return None
    lng_s, lat_s = location.split(",")[:2]
    try:
        return float(lat_s), float(lng_s)
    except ValueError:
        return None


# ---------------------------------------------------------------- 文本搜索（兜底，5 千/月）
def search_poi(keyword: str, city: str, *, offset: int = 5) -> tuple[float, float, str] | None:
    """「景点名 → 真实坐标」。**先走地理编码（15 万/月），失败才用 POI 搜索（5 千/月）**。

    高德配额（2026 控制台口径）：
        - 基础 LBS（地理编码）—— 15 万/月
        - POI 搜索（place/text）—— 5 千/月 ⚠️ 易触顶

    ⚠️ 用户 2026-09-17 反馈：原实现无脑走 POI 搜索，把月配额用到 4000+ 后只剩 1000，
    等额度耗尽后**静默把所有景点都标「检索不到」**，看上去像数据问题。
    现在改成「先 geo 后 poi」，90%+ 的请求只花地理编码配额，且 POI 熔断后地理编码仍可用。

    返回 (lat, lng, district)；找不到或名称对不上返回 None。
    """
    if not settings.has_amap:
        return None

    # 主路径：地理编码（15 万/月）
    if not _QUOTA_GONE_GEO:
        hit = search_address(keyword, city)
        if hit:
            return hit

    # 兜底：POI 关键字搜索（5 千/月）—— 地理编码失败或额度熔断时才走
    if not _QUOTA_GONE_POI:
        hit = _query_text(keyword, city, offset)
        if hit:
            return hit

        # 剥掉「景区 / 博物馆 / 古镇」这类描述性后缀再试一次。
        short = _strip_suffix(keyword)
        if short and short != keyword:
            logger.info("「%s」未命中，改用「%s」重试", keyword, short)
            hit = _query_text(short, city, offset)
            if hit:
                return hit

    logger.warning("⚠️ 高德检索不到「%s」（city=%s）", keyword, city)
    return None


def _query_text(keyword: str, city: str, offset: int) -> tuple[float, float, str] | None:
    if _QUOTA_GONE_POI:
        raise AmapQuotaExceeded("高德 POI 搜索额度已用完（熔断生效），已停止后续检索")
    try:
        resp = get_client().get(
            f"{settings.amap_base_url.rstrip('/')}/v3/place/text",
            params={
                "key": settings.amap_key,
                "keywords": keyword,
                "city": city,
                "citylimit": "true",
                "offset": offset,
                "page": 1,
                "extensions": "base",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("高德检索「%s」失败：%s", keyword, exc)
        return None

    if str(data.get("status")) != "1":
        info = str(data.get("info") or "")
        if is_quota_error(status=str(data.get("status") or ""),
                          infocode=str(data.get("infocode") or ""), info=info):
            _trip_quota("poi", info)
        logger.warning("高德返回异常（%s）：%s", keyword, info)
        return None

    pois = [p for p in (data.get("pois") or []) if p.get("location")]
    if not pois:
        return None

    def overlap(poi: dict) -> int:
        return len(set(str(poi.get("name") or "")) & set(keyword))

    best = max(pois, key=overlap)
    name = str(best.get("name") or "")
    if overlap(best) < 2 and keyword not in name and name not in keyword:
        logger.debug("「%s」命中「%s」但相似度不足", keyword, name)
        return None

    coords = _parse_location(str(best.get("location") or ""))
    if not coords:
        return None
    lat, lng = coords
    return lat, lng, str(best.get("adname") or "")


# 描述性后缀，按长度从长到短尝试剥离
_SUFFIXES = (
    "国家森林公园", "文化旅游区", "旅游度假区", "风景名胜区", "遗址公园",
    "步行街", "博物馆", "博物院", "纪念馆", "古镇", "老街", "景区", "乐园", "公园",
)


def _strip_suffix(name: str) -> str:
    for suf in _SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf) + 1:
            return name[: -len(suf)]
    return ""


# ---------------------------------------------------------------- 地理编码（主路径，15 万/月）
def search_address(address: str, city: str) -> tuple[float, float, str] | None:
    """地理编码：地址 → (lat, lng, district)。

    高德文档明确：「也支持对地标性名胜景区、建筑物名称解析为高德经纬度坐标」，
    所以直接把景点名当 address 传也能命中 POI 级（level=兴趣点）。

    与 `search_poi` 的区别：**走的是「基础 LBS」配额（15 万/月）**，
    而 POI 搜索（place/text）只有 **5 千/月**。同样的工作量，配额差 30 倍。
    """
    if not settings.has_amap:
        return None

    hit = _query_geo(address, city)
    if hit:
        return hit

    # 偶尔剥掉「博物馆/景区/古镇」后能命中更短的官方 POI 名 —— 复用同一份剥后缀
    short = _strip_suffix(address)
    if short and short != address:
        logger.info("「%s」地理编码未命中，改用「%s」重试", address, short)
        hit = _query_geo(short, city)
        if hit:
            return hit

    logger.warning("⚠️ 地理编码查不到「%s」（city=%s）", address, city)
    return None


def _query_geo(address: str, city: str) -> tuple[float, float, str] | None:
    if _QUOTA_GONE_GEO:
        raise AmapQuotaExceeded("高德地理编码额度已用完（熔断生效），已停止后续检索")
    try:
        resp = get_client().get(
            f"{settings.amap_base_url.rstrip('/')}/v3/geocode/geo",
            params={
                "key": settings.amap_key,
                "address": address,
                "city": city,
                "output": "json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("地理编码「%s」失败：%s", address, exc)
        return None

    if str(data.get("status")) != "1":
        info = str(data.get("info") or "")
        if is_quota_error(status=str(data.get("status") or ""),
                          infocode=str(data.get("infocode") or ""), info=info):
            _trip_quota("geo", info)
        logger.warning("地理编码返回异常（%s）：%s", address, info)
        return None

    geocodes = data.get("geocodes") or []
    # ⚠️ 高德文档原话：「当返回值存在时，将以字符串类型返回；当返回值不存在时，则以数组类型返回」
    # —— 也就是「count 为 0」时返回的不是 `[]`，是 `0`（数字）。统一处理：
    if not isinstance(geocodes, list) or not geocodes:
        return None

    # 兴趣点级（POI 级）最准 —— 高德其它级别（区县/道路）也会匹配，但景区/博物馆类会落到这里
    poi_hits = [g for g in geocodes if str(g.get("level") or "") == "兴趣点"]
    candidates = poi_hits or geocodes
    best = candidates[0]              # 地理编码的匹配级别表是按精确度排过序的，第一个即可

    coords = _parse_location(str(best.get("location") or ""))
    if not coords:
        return None
    lat, lng = coords
    return lat, lng, str(best.get("district") or "")


# ---------------------------------------------------------------- 周边搜索
def search_around(
    lat: float,
    lng: float,
    *,
    keyword: str = "",
    types: str = TYPE_RESTAURANT,
    radius: int = 1200,
    offset: int = 8,
) -> list[dict]:
    """周边搜索，返回 [{name, lat, lng, distance_m, address, type, district}]。

    用于给餐饮节点找**真实店名**——比让模型编一个店名可靠得多。
    """
    if not settings.has_amap:
        return []

    try:
        resp = get_client().get(
            f"{settings.amap_base_url.rstrip('/')}/v3/place/around",
            params={
                "key": settings.amap_key,
                "location": f"{lng},{lat}",
                "keywords": keyword,
                "types": types,
                "radius": radius,
                "offset": offset,
                "page": 1,
                "sortrule": "distance",
                "extensions": "base",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("高德周边搜索失败（%.4f,%.4f）：%s", lat, lng, exc)
        return []

    if str(data.get("status")) != "1":
        logger.warning("高德周边搜索异常：%s", data.get("info"))
        return []

    out: list[dict] = []
    for p in data.get("pois") or []:
        coords = _parse_location(str(p.get("location") or ""))
        if not coords:
            continue
        p_lat, p_lng = coords
        try:
            dist = int(float(p.get("distance") or 0))
        except (TypeError, ValueError):
            dist = 0
        out.append(
            {
                "name": str(p.get("name") or ""),
                "lat": p_lat,
                "lng": p_lng,
                "distance_m": dist,
                "address": str(p.get("address") or ""),
                "type": str(p.get("type") or ""),
                "district": str(p.get("adname") or ""),
            }
        )
    return out


def search_restaurants(lat: float, lng: float, meal: str = "lunch", limit: int = 5) -> list[dict]:
    """按餐别找附近餐厅。午餐偏快餐/本地小吃，晚餐偏正餐/火锅。"""
    keyword = "小吃|面馆|快餐" if meal == "lunch" else "火锅|川菜|中餐厅"
    radius = 1000 if meal == "lunch" else 1500
    hits = search_around(lat, lng, keyword=keyword, types=TYPE_RESTAURANT,
                         radius=radius, offset=max(6, limit * 2))
    # 过滤掉明显不是餐馆的（便利店、超市混进 050000 是常事）
    bad = ("便利店", "超市", "商店", "咖啡", "饮品", "甜品", "蛋糕", "面包")
    hits = [h for h in hits if h["name"] and not any(b in h["name"] for b in bad)]
    return hits[:limit]
