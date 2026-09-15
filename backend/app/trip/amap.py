"""高德 Web 服务客户端（seed 补坐标 + 运行时搜真实餐饮，两处共用）。

坐标系：高德返回的 location 就是 GCJ-02，可以直接入库、直接画图，不需要转换。

- search_poi()     关键字文本搜索，用于「景点名 → 真实坐标」
- search_around()  周边搜索，用于「景点附近 → 真实餐饮店名」
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


# ---------------------------------------------------------------- 文本搜索
def search_poi(keyword: str, city: str, *, offset: int = 5) -> tuple[float, float, str] | None:
    """关键字搜索单个 POI，返回 (lat, lng, district)。找不到或名称对不上返回 None。"""
    if not settings.has_amap:
        return None

    hit = _query_text(keyword, city, offset)
    if hit:
        return hit

    # 首次没匹配上：剥掉「景区 / 博物馆 / 古镇」这类描述性后缀再试一次。
    # 模型喜欢写全称（「成都武侯祠博物馆」），高德里的官方 POI 名往往更短。
    short = _strip_suffix(keyword)
    if short and short != keyword:
        logger.info("「%s」未命中，改用「%s」重试", keyword, short)
        hit = _query_text(short, city, offset)
        if hit:
            return hit

    logger.warning("⚠️ 高德检索不到「%s」（city=%s）", keyword, city)
    return None


def _query_text(keyword: str, city: str, offset: int) -> tuple[float, float, str] | None:
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
        logger.warning("高德返回异常（%s）：%s", keyword, data.get("info"))
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
