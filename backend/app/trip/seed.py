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
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.trip import llm, service
from app.trip.models import Attraction, City, Spot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger("triptide.seed")

SEED_FILE = Path(__file__).resolve().parent / "data" / "seed_attractions.json"
TARGET_PER_CITY = 22   # 一个城市的景点池参考规模（含远郊，约 3~4 天量）
AMAP_SLEEP = 0.8       # 控制 QPS。0.25 实测会撞 CUQPS_HAS_EXCEEDED_THE_LIMIT
SPOT_MAX_KM = 3.0      # 子景点离母景点超过这个距离 → 判定为命中同名地点，丢弃

# 景点命中的坐标离「最近的那个落脚点」超过这个距离 → 判为「全国范围内搜到的同名地点」，丢弃。
# 为什么按「落脚点（hotel_areas）」而不是「目的地中心」判：region 本身就跨上千公里
# （黑龙江的漠河离牡丹江 1125km），按中心判会把合法景点也毙掉。
#
# 踩过的坑：**region 的 `city.name` 不是高德认得的城市名**（「北疆」「西疆」「川西」），
# `search_poi(name, city.name)` 的 `citylimit` 于是失效 → 变成全国匹配：
# 「五彩滩」搜到了**广西涠洲岛**那个（3500km 外）、「火焰山」搜到了北京的同名点。
# 城市目的地也一并用这个兜底（防「人民公园」这类遍地都是的名字）。
MAX_ATTRACTION_KM = 400.0


def _too_far(city: Any, name: str, lat: float, lng: float) -> bool:
    """命中的点是否离这个目的地的所有落脚点都太远（大概率是同名地点）。"""
    anchors = [
        (float(h["lat"]), float(h["lng"]))
        for h in (city.hotel_areas or [])
        if h.get("lat") is not None and h.get("lng") is not None
    ] or [(city.center_lat, city.center_lng)]
    gap = min(_km(a_lat, a_lng, lat, lng) for a_lat, a_lng in anchors)
    if gap > MAX_ATTRACTION_KM:
        logger.warning(
            "「%s」命中的点离「%s」最近落脚点 %.0fkm（阈值 %.0fkm），判为同名地点、丢弃",
            name, city.name, gap, MAX_ATTRACTION_KM,
        )
        return True
    return False
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
{bases}{exclude}
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
  近郊景点（车程 1 小时以上）控制在 2~4 个
- ⚠️ **如果这座城市最核心的景区在近郊，必须包含进去，不受上面的数量限制。**
  例：桂林必须含阳朔（遇龙河 / 西街 / 兴坪），黄山必须含黄山风景区与宏村，泰安必须含泰山。
  踩过这个坑：桂林的名单被市区一堆小公园占满了 22 个名额，阳朔一个没进 ——
  而外地游客来桂林，多半就是冲着阳朔来的。**近郊名额该给核心大景区，不是给市区凑数。**
- ⚠️ **只列「{city}」自己的地方。** 别把周边城市/邻省的景点写进来 ——
  每个目的地只负责自己那一片，写重了会让用户在两个目的地里看到同一个景点。
  拿不准某个景点算不算，就别写。
只输出 JSON。"""


def _bases_hint(city_name: str, hotel_areas: list | None) -> str:
    """区域目的地要额外告诉模型「过夜基地在哪」。

    不然它会按名字瞎猜 —— 实测「西疆」这种非通用地名，模型会吐一份通用新疆名单，
    和「北疆」整份重复（10 个同名景点）。
    """
    names = [str(h.get("area") or "") for h in (hotel_areas or []) if h.get("area")]
    if not names:
        return ""
    return (
        f"\n「{city_name}」是一条**跨州 / 跨市的长线**，全程的过夜基地是："
        f"{'、'.join(names)}。\n"
        f"景点要分布在这些基地附近，**不要跑到别的城市或别的省去**。\n"
    )


# 多远算「邻居」——用来提醒模型别把邻居的景点写进来。
# 设得比较宽松（600km）是因为 region 的中心常常就是某个 city（山西的中心=太原、
# 贵州的中心=贵阳、青海湖环线的中心≈西宁），距离算出来是 0，必须覆盖到。
NEIGHBOR_KM = 600.0
NEIGHBOR_MAX = 8
NEIGHBOR_NAMES_MAX = 30   # 点名列出「已被邻居收走」的景点，最多列这么多


def _neighbors_hint(db: Session, city: City) -> str:
    """告诉模型「同区域还有哪些目的地」，并**点名**它们已经收走的景点。

    ⚠️ 踩过的坑：**先灌区域、后灌城市**，而城市那边的提示词不知道区域目的地的存在 ——
    模型不知道「西宁」和「青海湖环线」是两个目的地，把塔尔寺、东关清真大寺、
    莫家街、青海省博物馆又收了一遍；「太原」和「山西」重复了晋祠、山西博物院……
    **一次冒出 19 个跨目的地重名。**

    ⚠️ **只写「别把它们的地方写进来」不够** —— 实测重灌「山西」时它照样收了太原的
    晋祠 / 山西博物院 / 太原古县城（前两个还换了名字：`晋祠` vs `晋祠博物馆`，
    连重名检查都抓不到）。**必须点名**：把邻居名下的景点列出来说「这些已经被收走了」。
    """
    others = []
    for c in db.scalars(select(City)).all():
        if c.id == city.id or not c.center_lat:
            continue
        gap = _km(city.center_lat, city.center_lng, c.center_lat, c.center_lng)
        if gap <= NEIGHBOR_KM:
            others.append((gap, c))
    if not others:
        return ""

    others.sort(key=lambda x: x[0])
    near = others[:NEIGHBOR_MAX]

    # 按「离得近的邻居优先」收集它们名下的景点，凑够上限就停 ——
    # 别按字母排序，否则最相关的那几个（离得最近的邻居的）可能被截掉
    taken: list[str] = []
    for _, c in near:
        if len(taken) >= NEIGHBOR_NAMES_MAX:
            break
        for a in db.scalars(select(Attraction).where(Attraction.city_id == c.id)).all():
            if len(taken) >= NEIGHBOR_NAMES_MAX:
                break
            taken.append(a.name)

    lines = [
        "",
        f"⚠️ 同区域还有这些**独立的目的地**：{'、'.join(c.name for _, c in near)}。",
        f"它们各自负责自己那一片，**只列「{city.name}」自己的地方**。",
    ]
    if taken:
        lines.append(
            f"以下景点**已经被它们收走了，绝对不要再列**（连换个名字写同一处地方也不行）："
            f"{'、'.join(taken)}。"
        )
    lines.append(
        "（踩过：西宁把青海湖环线的塔尔寺收了、太原把山西的晋祠收了 —— "
        "用户在两个目的地里看到同一个景点，方案还会给同一个地方算两次停留时间。）"
    )
    return "\n".join(lines) + "\n"


# 给「补景点」点名排除的已有景点上限（不足 8 景的目的地最多也就 7 个，远用不满）
EXCLUDE_NAMES_MAX = 40


def _exclude_hint(existing: list[str]) -> str:
    """「补景点」时把已有景点**点名**排除 —— 不然模型会换个说法把同一处再写一遍。

    ⚠️ 踩过的坑：`晋祠` / `晋祠博物馆`、`洪崖洞` / `洪崖洞民俗风貌区` ——
    连重名检查都抓不到。所以除了点名，调用方还要做一层「互相包含」的兜底过滤。
    """
    if not existing:
        return ""
    shown = existing[:EXCLUDE_NAMES_MAX]
    extra = "" if len(existing) <= EXCLUDE_NAMES_MAX else f"（另有 {len(existing) - EXCLUDE_NAMES_MAX} 个略）"
    return (
        f"\n⚠️ 以下景点**已经在库里了，绝对不要再列**（连换个说法写同一处也不行，"
        f"例：已收「晋祠」就别再写「晋祠博物馆」）：\n{'、'.join(shown)}{extra}。\n"
        f"只列上面没有的**新景点**。\n"
    )


# ================================================================ 高德
# 客户端与匹配逻辑都在 amap.py（运行时搜真实餐饮也用同一份，避免两处各写一遍）
from app.trip.amap import search_address, search_poi  # noqa: E402


# ================================================================ 名单来源
def load_offline_seed() -> dict:
    if not SEED_FILE.exists():
        raise FileNotFoundError(f"离线种子文件不存在：{SEED_FILE}")
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


def llm_attraction_list(
    city: str, n: int = TARGET_PER_CITY, hint: str = "", exclude: str = ""
) -> list[dict] | None:
    """让 DeepSeek 出一份景点名单（不含坐标）。失败返回 None。

    `n` 是目标条数，由 CLI 的 `--per` 传入。**别改 `TARGET_PER_CITY` 的默认值**——
    它是全局常量，改了会让以后重灌老城也只出那么几个。

    `hint` 是 caller 拼好的地理上下文，两块（都由 `seed_city` 提供）：
      · `_bases_hint`     —— region 的过夜基地（不然「西疆」会被当成整个新疆）
      · `_neighbors_hint` —— 同区域的其它目的地（不然会把邻居的景点收进来）
    `exclude` 是「补景点」时点名排除的已入库景点（`_exclude_hint` 拼的），全量重灌时传空串。
    """
    if not settings.has_llm:
        return None
    try:
        raw = llm.chat_json(
            [
                {"role": "system", "content": LIST_SYSTEM},
                {
                    "role": "user",
                    "content": LIST_USER.format(city=city, n=n, bases=hint, exclude=exclude),
                },
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
    # kind: city=城市+周边（默认）；region=区域游（贵州/北疆环线这类多基地目的地）
    city.kind = str(payload.get("kind") or city.kind or "city")
    city.region = str(payload.get("region") or city.region or "")
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
        # 已经有坐标的直接信任，不再打接口 —— 与 `upsert_attractions` 里 attractions 的
        # `pre_calibrated` 分支同一个思路，都是为了省配额。
        # 种子回写后每个子景点都带坐标，这里能省掉整整一轮检索：
        # 实测深圳 71 个子景点，不加这个判断要白打 71 次接口、多花 85 秒。
        if s.get("lat") is not None and s.get("lng") is not None:
            got += 1
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
            if hit and _too_far(city, name, hit[0], hit[1]):
                hit = None   # 见 _too_far 的说明
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
    per: int = TARGET_PER_CITY,
) -> None:
    city = upsert_city(db, payload)
    logger.info("—— %s ——", city.name)

    if reset:
        deleted = db.execute(delete(Attraction).where(Attraction.city_id == city.id)).rowcount
        db.flush()
        logger.info("已清空 %s 的 %d 条景点", city.name, deleted)

    items: list[dict] | None = None
    if source in ("auto", "llm"):
        hint = _bases_hint(city.name, payload.get("hotel_areas")) + _neighbors_hint(db, city)
        items = llm_attraction_list(city.name, per, hint)
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


# ================================================================ 补景点（增量）
def _dup_with_existing(name: str, existing: set[str]) -> bool:
    """候选名是否和已有景点是同一处（含「晋祠」vs「晋祠博物馆」这类换说法的）。"""
    return any(name == e or name in e or e in name for e in existing)


def topup_city(
    db: Session, city: City, target: int, use_amap: bool
) -> tuple[int, int, list[dict]]:
    """给景点数不足 `target` 的目的地**增量**补景点，返回 (现有个数, 新增个数, 新增明细)。

    与 `seed_city` 的区别（用户 2026-09-17 定的规矩）：
      · **不动已有景点** —— 只补缺的，已有的绝不再补（避免重复）；
      · 名单生成时把已有景点**点名排除**（`_exclude_hint`），入库前再做一层
        「互相包含」兜底（`_dup_with_existing`，拦 `晋祠` vs `晋祠博物馆`）；
      · 坐标**只走地理编码**（基础 LBS，15 万/月），**不打 POI 关键字搜索**
        （place/text，5 千/月 —— 用户反馈额度已经见底）。命中的坐标直接带上
        `coord_source="amap"` 交给 `upsert_attractions`，它对 pre_calibrated 条目不再打接口；
      · region 的名字（北疆 / 青海湖环线）不是真实行政区划，地理编码的 city 参数留空、
        靠 `_too_far` 的 400km 距离闸拦同名地点（堵过「五彩滩」命中广西涠洲岛的坑）；
      · **不补子景点** —— 每个子景点要多打 1~3 次接口，这次的目标是排名用的「景点数」；
        子景点以后用正常 seed 流程单独补。
    """
    have = db.scalars(
        select(Attraction).where(Attraction.city_id == city.id)
        .order_by(Attraction.heat.desc())
    ).all()
    existing = {a.name for a in have}
    need = target - len(existing)
    if need <= 0:
        return len(existing), 0, []

    hint = _bases_hint(city.name, city.hotel_areas) + _neighbors_hint(db, city)
    items = llm_attraction_list(city.name, need, hint, _exclude_hint(sorted(existing)))
    if not items:
        logger.warning("「%s」没能生成新景点名单，本次跳过", city.name)
        return len(existing), 0, []

    # region 的名字不是行政区划，地理编码不带 city 约束，靠距离闸兜底
    geo_city = city.name if city.kind == "city" else ""

    picked: list[dict] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if _dup_with_existing(name, existing):
            logger.info("「%s」与已有景点是同一处（重名或换说法），跳过", name)
            continue
        item["spots"] = []                     # 这次不补子景点，省接口
        if not use_amap:
            picked.append(item)                # 离线模式：交给 upsert 的「无坐标跳过」
            continue
        hit = search_address(name, geo_city)
        time.sleep(AMAP_SLEEP)
        if hit and _too_far(city, name, hit[0], hit[1]):
            hit = None
        if not hit:
            logger.warning("「%s」地理编码未命中，本次不补", name)
            continue
        item["lat"], item["lng"] = hit[0], hit[1]
        item["district"] = str(item.get("district") or "") or hit[2]
        item["coord_source"] = "amap"          # 让 upsert 走 pre_calibrated，不再打接口
        picked.append(item)
        existing.add(name)

    added, _calibrated = upsert_attractions(db, city, picked, use_amap=False)
    return len(existing), added, picked


def _sync_offline_seed(city_name: str, items: list[dict]) -> int:
    """把新增景点回写进离线种子（铁律 21：种子 = 库里现状）。返回写进几条。

    种子是版本化备份 + 离线兜底；不回写的话，下次 `--source offline` 重灌这个目的地时
    这些新景点就没了（upsert 只加不删，库里还在，但种子从此缺一块）。
    """
    if not items:
        return 0
    data = load_offline_seed()
    payload = next((c for c in data["cities"] if c["name"] == city_name), None)
    if payload is None:
        logger.warning("离线种子里没有「%s」，跳过回写", city_name)
        return 0

    have = {a["name"] for a in (payload.get("attractions") or [])}
    added = 0
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name or name in have or item.get("lat") is None:
            continue
        payload.setdefault("attractions", []).append(
            {
                "name": name,
                "district": str(item.get("district") or ""),
                "lat": float(item["lat"]),
                "lng": float(item["lng"]),
                "visit_minutes": int(item.get("visit_minutes") or 90),
                "heat": int(item.get("heat") or 50),
                "must_visit": bool(item.get("must_visit")),
                "best_time": str(item.get("best_time") or ""),
                "tags": list(item.get("tags") or []),
                "intro": str(item.get("intro") or ""),
                "coord_source": "amap",
            }
        )
        have.add(name)
        added += 1

    if added:
        SEED_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        logger.info("离线种子已回写：%s 新增 %d 条", city_name, added)
    return added


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
    parser = argparse.ArgumentParser(description="AI 旅行搭子 数据初始化")
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
    parser.add_argument(
        "--per", type=int, default=TARGET_PER_CITY, metavar="N",
        help=f"每个目的地生成几个景点（默认 {TARGET_PER_CITY}；区域长线可给大一点）",
    )
    parser.add_argument(
        "--only-empty", action="store_true",
        help="只处理「库里还没有任何景点」的目的地 —— 新增目的地专用，"
             "**不会碰老目的地**（这是它的全部意义）",
    )
    parser.add_argument(
        "--topup", action="store_true",
        help="增量补景点：只给景点数不足 --per 的目的地补到 --per 个，**已有的绝不动**。"
             "坐标只走地理编码（基础 LBS，15 万/月），不打 POI 关键字搜索（5 千/月）",
    )
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.list:
            show_status(db)
            return

        if args.topup:
            if not settings.has_amap:
                print("补景点需要 AMAP_KEY：坐标只走地理编码（15 万/月），不打 POI 搜索")
                return
            rows = db.execute(
                select(City, func.count(Attraction.id))
                .outerjoin(Attraction, Attraction.city_id == City.id)
                .group_by(City.id)
            ).all()
            targets = [
                (c, n)
                for c, n in rows
                if n < args.per and (not args.city or c.name == args.city.strip())
            ]
            if not targets:
                print(f"\n没有需要补的目的地（景点数都已 ≥ {args.per}）")
                show_status(db)
                return
            print(f"\n补景点：目标 {args.per} 个/地，待补 {len(targets)} 个 —— "
                  f"{', '.join(f'{c.name}({n})' for c, n in targets)}\n")
            total = 0
            for c, n in targets:
                have, added, picked = topup_city(db, c, args.per, use_amap=True)
                db.commit()
                _sync_offline_seed(c.name, picked)
                total += added
                logger.info("%s：原 %d 个，新增 %d 个，现 %d 个", c.name, n, added, have + added)
            print(f"\n共新增 {total} 个景点")
            service.recompute_rank_scores(db)   # 排序分依赖景点数与热度，必须重算
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

        # --only-empty：只留「库里查不到、或名下 0 个景点」的目的地。
        # 没有这一步的话，不带 --city 跑一次会把**所有**目的地都重灌一遍，
        # 用 LLM 现生成的名单盖掉手工校对过的老城数据。
        if args.only_empty:
            populated = {
                cid for (cid,) in db.execute(select(Attraction.city_id).group_by(Attraction.city_id)).all()
            }
            kept = []
            for p in payloads:
                row = db.scalar(select(City).where(City.name == p["name"]))
                if row is None or row.id not in populated:
                    kept.append(p)
            logger.info(
                "--only-empty：%d 个目的地里跳过 %d 个已有景点的，实际处理 %d 个",
                len(payloads), len(payloads) - len(kept), len(kept),
            )
            payloads = kept

        if not payloads:
            print("\n没有需要处理的目的地（--only-empty 下都已灌过数据）\n")
            show_status(db)
            return

        use_amap = not args.no_amap and settings.has_amap
        if not args.no_amap and not settings.has_amap:
            logger.warning("未配置 AMAP_KEY，坐标直接使用离线种子值")

        print(f"\n数据源：{'DeepSeek + 高德' if settings.has_llm and use_amap else '离线种子'}")
        print(f"每个目的地目标景点数：{args.per}")
        print(f"待处理目的地（{len(payloads)} 个）：{', '.join(p['name'] for p in payloads)}\n")

        if args.only_cities:
            for payload in payloads:
                upsert_city(db, payload)
                logger.info("已更新城市：%s（候选住宿片区 %d 个）",
                            payload["name"], len(payload.get("hotel_areas") or []))
            db.commit()
            service.recompute_rank_scores(db)
            show_status(db)
            return

        for payload in payloads:
            seed_city(db, payload, args.source, use_amap, args.reset, args.per)

        # 数据变了：重算首页排序分（顺带作废宫格缓存）。
        # 漏了这步接口不报错，但首页顺序会和实际数据对不上 —— 见 service.recompute_rank_scores 的说明。
        service.recompute_rank_scores(db)
        show_status(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
