"""一次性数据初始化脚本（管理员用）。

流程：DeepSeek 出景点名单 → 高德 Web 服务 POI 搜索补真实 GCJ-02 坐标 → 入库。
没有 Key 时自动降级：名单用 data/seed_attractions.json，坐标用种子里的手校值。

用法：
    cd backend
    python -m app.trip.seed                      # 全部预置城市，自动选源
    python -m app.trip.seed --city 成都           # 只初始化一个城市
    python -m app.trip.seed --source offline      # 强制用离线种子
    python -m app.trip.seed --no-amap             # 跳过坐标校准（省时）
    python -m app.trip.seed --reset --city 成都    # 先清空该城景点再重建
    python -m app.trip.seed --list                # 只看当前库里的情况
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.trip import llm
from app.trip.models import Attraction, City, Spot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger("triptide.seed")

SEED_FILE = Path(__file__).resolve().parent / "data" / "seed_attractions.json"
TARGET_PER_CITY = 22   # 一个城市的景点池参考规模（含远郊，约 3~4 天量）
AMAP_SLEEP = 0.8       # 控制 QPS。0.25 实测会撞 CUQPS_HAS_EXCEEDED_THE_LIMIT
SPOT_MAX_KM = 3.0      # 子景点离母景点超过这个距离 → 判定为命中同名地点，丢弃
EARTH_R_M = 6_371_000.0


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点球面距离（km）。seed 内部用，不引 planner 避免循环依赖。"""
    from math import asin, cos, radians, sin, sqrt

    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * EARTH_R_M * asin(sqrt(a)) / 1000.0

LIST_SYSTEM = """你是旅游数据编辑。只输出 JSON，不要任何解释和 Markdown 围栏。"""

LIST_USER = """请为「{city}」列出 {n} 个最值得去的景点，按外地游客视角排序。

输出 JSON（字段名必须完全一致）：
{{"attractions":[{{"name":"景点官方常用名","district":"所在区，如 青羊区","intro":"一句话简介","visit_minutes":90,"heat":420,"must_visit":false,"best_time":"","tags":["历史"],"spots":[{{"name":"内部子景点名","guide":"这个点位怎么玩 / 要注意什么，20~60 字","stay_minutes":30}}]}}]}}

要求：
- name 必须是地图上能直接检索到的官方名称，不要用昵称（写「大熊猫繁育研究基地」而不是「熊猫基地」）
- intro 一句话 20~40 字，要有具体信息（几点去人少 / 从哪个门进 / 要不要预约 / 周边吃什么），禁止「值得一去」这类空话
- visit_minutes 是建议游览分钟数，含进出场时间，取 45 的倍数，范围 45~300
- heat 是**全国统一热度分**（0~1000 整数），不是城市内相对值，按 T 级给：
  · 600~1000  T0 世界级名片：故宫、长城、兵马俑这个级别，没去过等于没来过中国
  · 350~599   T1 全国闻名：专程去这座城市的核心理由之一（如成都大熊猫基地、都江堰）
  · 200~349   T2 知名景点：外地游客大概率会去（武侯祠、杜甫草堂级别）
  · 80~199    T3 省/市内知名，或特色街区、体验地
  · 1~79      T4 小众、本地人去处、顺路打卡
  同城景点必须按真实梯度拉开，不要挤在同一档；地标性免费打卡点（如天府广场）也有全国知名度，按实际给分
- must_visit 只给最核心的 4~6 个标 true
- best_time 用来约束「该上午去还是晚上去」，四选一，拿不准就留空字符串：
  · "morning" 看动物 / 自然类，午后状态差（动物园、熊猫基地、植物园）
  · "night"   夜景 / 夜游 / 酒吧街，要等亮灯（江边夜景、不夜城、灯光秀）
  · "museum"  博物馆 / 美术馆 / 纪念馆，闭馆早
  · ""        没有时段要求（绝大多数景点都是这个）
- **spots 是景点内部的主要点位**，按建议游览顺序排 3~6 个：
  · 大景区（都江堰、故宫、西湖这类）必填；单点式小景点（一家书店、一座塔）给空数组 []
  · guide 要写「**怎么玩**」而不是「是什么」：从哪个门进最顺、几点人流少、要不要排队、
    哪个机位出片、有什么坑（价格虚高 / 要提前买票），20~60 字
  · 不要编造不存在的小众点位；拿不准就少写几个
  · **不要写坐标** —— 坐标由地图接口补，你写的也不准
- 类型要混合：地标 / 历史古迹 / 博物馆 / 自然公园 / 商圈文创 / 美食街区，
  其中近郊景点（车程 1 小时以上）最多 3 个
只输出 JSON。"""


# ================================================================ 高德
# 客户端与匹配逻辑都在 amap.py（运行时搜真实餐饮也用同一份，避免两处各写一遍）
from app.trip.amap import search_poi  # noqa: E402


# ================================================================ 名单来源
def load_offline_seed() -> dict:
    if not SEED_FILE.exists():
        raise FileNotFoundError(f"离线种子文件不存在：{SEED_FILE}")
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def llm_attraction_list(city: str) -> list[dict] | None:
    """让 DeepSeek 出一份景点名单（不含坐标）。失败返回 None。"""
    if not settings.has_llm:
        return None
    try:
        raw = llm.chat_json(
            [
                {"role": "system", "content": LIST_SYSTEM},
                {"role": "user", "content": LIST_USER.format(city=city, n=TARGET_PER_CITY)},
            ]
        )
        data = llm.extract_json(raw)
        items = data.get("attractions") or []
        if not items:
            logger.warning("DeepSeek 未返回景点，回退离线种子")
            return None
        logger.info("DeepSeek 为「%s」产出 %d 个景点", city, len(items))
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("DeepSeek 生成名单失败（%s），回退离线种子", exc)
        return None


# ================================================================ 入库
def upsert_city(db: Session, payload: dict) -> City:
    city = db.scalar(select(City).where(City.name == payload["name"]))
    if city is None:
        city = City(name=payload["name"])
        db.add(city)
        logger.info("新建城市：%s", payload["name"])

    city.pinyin = payload.get("pinyin") or city.pinyin or ""
    city.emoji = payload.get("emoji") or city.emoji or ""
    city.tagline = payload.get("tagline") or city.tagline or ""
    city.heat = int(payload.get("heat") or city.heat or 0)
    city.center_lat = float(payload.get("center_lat") or city.center_lat or 0)
    city.center_lng = float(payload.get("center_lng") or city.center_lng or 0)
    city.hotel_areas = payload.get("hotel_areas") or city.hotel_areas or []
    db.flush()
    return city


def _fill_spot_coords(
    city_name: str,
    att_name: str,
    spots: list[dict],
    att_lat: float | None = None,
    att_lng: float | None = None,
) -> int:
    """给子景点补 GCJ-02 坐标（高德文本搜索），返回补到的个数。

    提示词里明确不让模型写坐标 —— 它报的坐标不可信，统一在这里补。

    多轮尝试：`{景点}{子景点}` → `{子景点}` → `{景点} {子景点}`。
    后两轮容易命中同名地点（「花径」「上清宫」遍地都是），
    所以命中后做**距离校验**：离母景点超过 SPOT_MAX_KM 一律丢弃，宁可留空。
    """
    got = 0
    for s in spots:
        spot_name = str(s.get("name") or "").strip()
        if not spot_name:
            continue
        for kw in (f"{att_name}{spot_name}", spot_name, f"{att_name} {spot_name}"):
            hit = search_poi(kw, city_name)
            time.sleep(AMAP_SLEEP)
            if not hit:
                continue
            if att_lat is not None and att_lng is not None:
                gap = _km(att_lat, att_lng, hit[0], hit[1])
                if gap > SPOT_MAX_KM:
                    logger.debug(
                        "「%s」命中「%.4f,%.4f」但离母景点 %.1fkm，判为同名地点，丢弃",
                        kw, hit[0], hit[1], gap,
                    )
                    continue
            s["lat"], s["lng"] = hit[0], hit[1]
            got += 1
            break
    return got


def upsert_spots(db: Session, attraction: Attraction, spots: list[dict]) -> int:
    """写入某景点的子景点（按 name 去重更新）。坐标缺失时保留旧值。"""
    written = 0
    for idx, s in enumerate(spots, start=1):
        spot_name = str(s.get("name") or "").strip()
        if not spot_name:
            continue
        row = db.scalar(
            select(Spot).where(
                Spot.attraction_id == attraction.id, Spot.name == spot_name
            )
        )
        if row is None:
            row = Spot(attraction_id=attraction.id, name=spot_name)
            db.add(row)

        row.guide = str(s.get("guide") or row.guide or "")[:1000]
        row.order_index = int(s.get("order_index") or idx)
        row.stay_minutes = int(s.get("stay_minutes") or row.stay_minutes or 0)
        if s.get("lat") is not None and s.get("lng") is not None:
            row.lat = float(s["lat"])
            row.lng = float(s["lng"])
        written += 1
    db.flush()
    return written


def upsert_attractions(
    db: Session,
    city: City,
    items: list[dict],
    use_amap: bool,
) -> tuple[int, int]:
    """返回 (新增/更新数, 坐标被校准数)。"""
    written = 0
    calibrated = 0

    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue

        lat = item.get("lat")
        lng = item.get("lng")
        district = str(item.get("district") or "")
        # 种子里标了 coord_source=amap 且带坐标的，是上一轮高德校准过的，直接信任，
        # 不再重复打接口（省配额）；没标记的才走高德检索
        pre_calibrated = bool(
            item.get("coord_source") == "amap" and lat is not None and lng is not None
        )
        coord_source = "amap" if pre_calibrated else "seed"

        if use_amap and not pre_calibrated:
            hit = search_poi(name, city.name)
            time.sleep(AMAP_SLEEP)
            if hit:
                lat, lng, amap_district = hit
                district = district or amap_district
                coord_source = "amap"
                calibrated += 1

        if lat is None or lng is None:
            logger.warning("跳过无坐标的景点：%s", name)
            continue

        row = db.scalar(
            select(Attraction).where(Attraction.city_id == city.id, Attraction.name == name)
        )
        if row is None:
            row = Attraction(city_id=city.id, name=name)
            db.add(row)

        row.intro = str(item.get("intro") or row.intro or "")
        row.district = district or row.district or ""
        row.lat = float(lat)
        row.lng = float(lng)
        row.visit_minutes = int(item.get("visit_minutes") or row.visit_minutes or 90)
        row.heat = int(item.get("heat") or row.heat or 50)
        row.must_visit = bool(item.get("must_visit", row.must_visit))
        # 时段约束：morning / night / museum / 空 —— planner.time_rule() 优先读它，
        # 换城市不用改代码；早期数据留空时仍回退到名称关键词
        best_time = str(item.get("best_time") or "").strip().lower()
        if best_time not in ("morning", "night", "museum"):
            best_time = ""
        row.best_time = best_time or row.best_time or ""
        row.tags = list(item.get("tags") or row.tags or [])
        row.coord_source = coord_source
        written += 1

        # 子景点：模型只给名称 + 攻略，**坐标交给高德补**（它报的坐标不可信）
        raw_spots = item.get("spots") or []
        if raw_spots:
            db.flush()  # 新建的景点要到这时才有 id
            if use_amap:
                _fill_spot_coords(
                    city.name, name, raw_spots,
                    att_lat=row.lat, att_lng=row.lng,
                )
            upsert_spots(db, row, raw_spots)

    db.flush()
    return written, calibrated


def seed_city(
    db: Session,
    payload: dict,
    source: str,
    use_amap: bool,
    reset: bool,
) -> None:
    city = upsert_city(db, payload)
    logger.info("—— %s ——", city.name)

    if reset:
        deleted = db.execute(delete(Attraction).where(Attraction.city_id == city.id)).rowcount
        db.flush()
        logger.info("已清空 %s 的 %d 条景点", city.name, deleted)

    items: list[dict] | None = None
    if source in ("auto", "llm"):
        items = llm_attraction_list(city.name)
    if items is None:
        items = payload.get("attractions") or []
        logger.info("使用离线种子名单（%d 条）", len(items))

    # 用离线种子补 LLM 名单缺的坐标（同名匹配）
    offline_map = {a["name"]: a for a in (payload.get("attractions") or [])}
    for item in items:
        base = offline_map.get(str(item.get("name") or ""))
        if base:
            item.setdefault("lat", base.get("lat"))
            item.setdefault("lng", base.get("lng"))
            item.setdefault("district", base.get("district"))

    written, calibrated = upsert_attractions(db, city, items, use_amap)
    db.commit()
    logger.info(
        "%s 完成：写入 %d 个景点，其中 %d 个坐标经高德校准", city.name, written, calibrated
    )


# ================================================================ CLI
def show_status(db: Session) -> None:
    rows = db.execute(
        select(City.name, City.heat, func.count(Attraction.id))
        .outerjoin(Attraction, Attraction.city_id == City.id)
        .group_by(City.id)
        .order_by(City.heat.desc())
    ).all()
    print("\n当前库中数据：")
    if not rows:
        print("  （空，请先跑一次初始化）")
    for name, heat, count in rows:
        print(f"  {name:<6} 热度 {heat:>3}  景点 {count:>3} 个")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="TripTide 数据初始化")
    parser.add_argument("--city", help="只处理指定城市（名称或拼音）")
    parser.add_argument(
        "--source", choices=["auto", "llm", "offline"], default="auto",
        help="景点名单来源：auto=有 Key 用 LLM，否则离线种子",
    )
    parser.add_argument("--no-amap", action="store_true", help="跳过高德坐标校准")
    parser.add_argument("--reset", action="store_true", help="先清空该城景点再重建")
    parser.add_argument("--list", action="store_true", help="只打印当前数据情况")
    parser.add_argument(
        "--only-cities", action="store_true",
        help="只更新城市信息（含候选住宿片区），不重新生成景点",
    )
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.list:
            show_status(db)
            return

        payloads = load_offline_seed()["cities"]
        if args.city:
            key = args.city.strip()
            payloads = [
                p for p in payloads if p["name"] == key or p.get("pinyin") == key.lower()
            ]
            if not payloads:
                print(f"离线种子里没有「{args.city}」。可用：{', '.join(c['name'] for c in load_offline_seed()['cities'])}")
                return

        use_amap = not args.no_amap and settings.has_amap
        if not args.no_amap and not settings.has_amap:
            logger.warning("未配置 AMAP_KEY，坐标直接使用离线种子值")

        print(f"\n数据源：{'DeepSeek + 高德' if settings.has_llm and use_amap else '离线种子'}")
        print(f"待处理城市：{', '.join(p['name'] for p in payloads)}\n")

        if args.only_cities:
            for payload in payloads:
                upsert_city(db, payload)
                logger.info("已更新城市：%s（候选住宿片区 %d 个）",
                            payload["name"], len(payload.get("hotel_areas") or []))
            db.commit()
            show_status(db)
            return

        for payload in payloads:
            seed_city(db, payload, args.source, use_amap, args.reset)

        show_status(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
