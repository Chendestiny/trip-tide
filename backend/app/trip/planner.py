"""地理工具 + 规则规划器（本地兜底引擎）。

两个用途：
  1. 无 DEEPSEEK_API_KEY 时独立产出完整方案，保证「勾选→生成→展示」闭环永远跑得通；
  2. 有 Key 时给 service 提供距离/时间计算能力，用于给 LLM 补充上下文。

设计原则：所有时间都是「算出来的」而不是「编出来的」——
    到达时间 = 上一站离开时间 + 路程耗时
    离开时间 = 到达时间 + 停留时长
因此规则产出的时间轴天然自洽，可作为 LLM 输出自洽性的参照基线。

分天策略（四步，顺序不能换）：
    ① 容量裁剪  先算总时间预算，装不下的按性价比丢掉，避免「最后一天被迫舍弃必去景点」
    ② 独占型隔离 把「游览 ≥4h」或「单程 ≥45min」的景点单独占一天，否则会把市区线挤崩
    ③ 成链均分  剩下的按最近邻成链，再按游览时长均分成天
    ④ 逐天裁剪  每天按真实时间预算再裁一次，溢出的顺延到下一天，最后一天溢出则舍弃
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from app.trip.schemas import (
    DayPlan,
    DroppedItem,
    HotelAdvice,
    HotelAlternative,
    MustVisitReason,
    PlanRequest,
    PlanResult,
    TimelineNode,
)

# ---------------------------------------------------------------- 常量
EARTH_RADIUS_M = 6_371_000.0

# ================================================================ 交通模型
# 用户只选一个大方向（自驾 / 打车+公共 / 公共），具体方式由硬编码按距离与场景挑。
# 全部本地计算，**不调用任何地图 API**——一次规划十几条腿，每条都打接口既慢又费配额，
# 而规划阶段的精度要求不高（±10 分钟不影响方案可用性）。
#
# 三档场景：
#   城区腿    至少一端在城区内且距离 ≤30km
#   近郊腿    两端都在城区外（如都江堰↔青城山），或城区→近郊（30~80km）
#   远郊腿    >80km


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    speed_kmh: float
    overhead_min: int
    max_km: float


WALK = Mode("walk", "步行", 4.5, 0, 1.5)
METRO = Mode("metro", "地铁/公交", 22.0, 9, 40)
TAXI = Mode("taxi", "打车", 26.0, 5, 40)
DRIVE_CITY = Mode("drive", "自驾", 30.0, 8, 30)
DRIVE_HWY = Mode("drive", "自驾·高速", 62.0, 8, 80)
DRIVE_FAR = Mode("drive", "自驾·高速", 85.0, 8, 9999)
CARPOOL = Mode("carpool", "顺风车", 50.0, 14, 90)
COACH = Mode("coach", "城际大巴", 42.0, 22, 150)
RAIL = Mode("rail", "城际铁路", 130.0, 40, 9999)

CITY_RADIUS_KM = 20.0     # 距市中心超过这个值算「城区外」
URBAN_LEG_KM = 30.0       # 城区腿的距离上限
SUBURB_LEG_KM = 80.0      # 近郊腿的距离上限
SHORT_TAXI_KM = 6.0       # 混合模式里，短于这个距离直接打车（地铁不划算）

MEAL_LUNCH_MINUTES = 60
MEAL_DINNER_MINUTES = 75
LUNCH_FROM = "11:20"        # 午饭锚点
LUNCH_AFTER = "11:40"       # 景点结束后过了这个点 → 出来立刻吃
LUNCH_MAX_WAIT = 75         # 为了在饭点前吃饭最多愿意等多久；超过就改成玩完再吃
DINNER_FROM = "18:00"       # 晚餐锚点：回到住宿片区之后才吃，不在返程路上吃
EARLIEST_DINNER = "17:00"   # 允许开饭的最早时刻；再早就不能算「晚饭」了
DINNER_TO_HOTEL = 10        # 饭后走回酒店的分钟数
MIN_STAY_MINUTES = 20       # 为按时返回，单个景点最少保留的停留时间
STANDALONE_MINUTES = 240    # 游览 ≥4h → 独占一天
STANDALONE_TRAVEL_MIN = 45  # 单程 ≥45min → 独占一天
CAPACITY_SLACK = 1.10       # 容量裁剪的松弛系数（路程有共享，但不能按 0 计）
OVERRUN_TOLERANCE = 30      # 超过目标返回时间多少分钟算「远郊日跑长了」
OVERNIGHT_KM = 45.0         # 当天景点离常住片区超过这个距离 → 建议就近过夜

# ================================================================ 分天模型 v2
# 分天的第一原则是「地理距离」，其次是景点重要性。为此先把景点量化成两件事：
#   · 级别（小/中/大）—— 由基础游览时长推导，决定「一天能放几个」
#   · 是否远郊      —— 单程 ≥45min，额外吃掉两倍单程时间，与级别是两个独立维度
# 再按通行时间把地理紧邻的景点聚成「片区簇」，簇在分天时是**原子、不可拆**。

GRADE_SMALL_MAX = 90        # ≤90 分钟        → 小景点（顺路打卡）
GRADE_MEDIUM_MAX = 180      # 91~180 分钟     → 中景点（半天量）；>180 → 大景点（占一天）

# 每天目标「游玩时长」（分钟）：决定一天排几个，与用户确认过
PACE_TARGET_MINUTES = {"relaxed": 240, "balanced": 360, "packed": 450}

# 弹性填充：(填充率 r, 倍数上限 m)
#     最终游玩 = clamp(可用游玩 × r, 基础游玩, 基础游玩 × m)
PACE_FLEX = {
    "relaxed": (0.80, 1.00),    # 按最少，早点收工
    "balanced": (0.95, 1.25),   # 适当扩充
    "packed": (1.00, 1.50),     # 填满可用时间
}
BIG_FLEX_MAX = 1.50         # 大景点 / 远郊不受节奏限制，一律允许扩到 1.5

CLUSTER_MAX_METERS = 1200   # 直线 ≤1.2km → 同一片区簇（步行约 15 分钟，簇内不拆）
MIN_SPLIT_MINUTES = 30      # 大景点跨午饭拆分时，前后每段至少这么长才值得拆


# ================================================================ 地理工具
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点球面距离（米）。输入为 GCJ-02 经纬度，同坐标系下差值可直接使用。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Leg:
    """一段行程的交通安排。"""

    mode: str          # walk / metro / taxi / drive / carpool / coach / rail
    label: str         # 中文名，直接展示在时间轴上
    km: float
    minutes: int


def _mk_leg(mode: Mode, km: float) -> Leg:
    minutes = km / mode.speed_kmh * 60.0 + mode.overhead_min
    minutes = max(5.0, minutes)
    return Leg(mode=mode.key, label=mode.label, km=round(km, 1),
               minutes=int(round(minutes / 5.0) * 5))


def leg(
    city: Any,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    transport: str,
) -> Leg:
    """算一段路怎么走、要多久。纯本地计算，不打任何地图接口。

    transport 是用户选的大方向，具体方式按距离与场景挑：
      · drive   自驾全程（城区 30 / 高速 62 / 长途 85 km/h）
      · mixed   打车+公共：≤6km 打车、跨区中长途地铁、近郊顺风车、远郊城际铁路
      · transit 公共：地铁公交 → 城际大巴 → 城际铁路
    """
    km = haversine_m(from_lat, from_lng, to_lat, to_lng) / 1000.0

    if km <= WALK.max_km:
        return _mk_leg(WALK, km)

    from_out = (
        haversine_m(city.center_lat, city.center_lng, from_lat, from_lng) / 1000.0
        > CITY_RADIUS_KM
    )
    to_out = (
        haversine_m(city.center_lat, city.center_lng, to_lat, to_lng) / 1000.0
        > CITY_RADIUS_KM
    )
    both_outside = from_out and to_out
    urban = km <= URBAN_LEG_KM and not both_outside

    if transport == "drive":
        mode = DRIVE_CITY if urban else (DRIVE_HWY if km <= SUBURB_LEG_KM else DRIVE_FAR)

    elif transport == "transit":
        mode = METRO if urban else (COACH if km <= SUBURB_LEG_KM else RAIL)

    else:  # mixed（含历史值 taxi）
        if urban and km <= SHORT_TAXI_KM:
            mode = TAXI          # 短途打车最划算，地铁进出站反而慢
        elif urban:
            mode = METRO         # 跨区中长途地铁更稳，不堵
        elif km <= SUBURB_LEG_KM:
            mode = CARPOOL       # 近郊：地铁不通、打车贵，顺风车是真实选择
        else:
            mode = RAIL

    return _mk_leg(mode, km)


def travel_from(city: Any, item: Any, transport: str) -> int:
    """从市中心到某景点的单程分钟数。"""
    return leg(city, city.center_lat, city.center_lng, item.lat, item.lng, transport).minutes


def route_minutes(distance_m: float, transport: str) -> int:
    """只有距离、没有端点时的粗算（保留给外部调用，主流程一律走 leg()）。"""
    km = distance_m / 1000.0
    if km <= WALK.max_km:
        mode = WALK
    elif transport == "drive":
        mode = DRIVE_CITY if km <= URBAN_LEG_KM else DRIVE_HWY
    elif transport == "transit":
        mode = METRO if km <= URBAN_LEG_KM else COACH
    else:
        mode = TAXI if km <= SHORT_TAXI_KM else METRO
    return _mk_leg(mode, km).minutes


# ================================================================ 每天的时间窗
def day_window(req: Any, day_index: int, total_days: int) -> tuple[str, str]:
    """取某天的 (出发时间, 目标返回时间)。

    首末天常常和中间几天不一样：第一天可能中午才到，最后一天要赶飞机。
    用户不填就用统一的 start_time / return_time。
    """
    start = req.start_time
    if day_index == 1 and getattr(req, "first_day_start_time", None):
        start = req.first_day_start_time
    end = req.return_time
    if day_index == total_days and getattr(req, "last_day_return_time", None):
        end = req.last_day_return_time
    return start, end


def meal_minutes(end_hhmm: str) -> int:
    """当天要预留的用餐分钟数。

    只有「吃完晚饭还来得及按时返回」才算上晚餐 —— 最后一天 16:00 赶返程这种，
    只预留午餐，`materialize_day` 也不会给它排晚餐。
    早先无论返程多早都扣两餐 135 分钟，既低估了最后一天的可用时间，
    又会在时间轴末尾硬塞一顿 18:00 的晚餐，把结束时间拖到 19:25。
    """
    latest_dinner = parse_hhmm(end_hhmm) - (MEAL_DINNER_MINUTES + DINNER_TO_HOTEL)
    if latest_dinner >= parse_hhmm(EARLIEST_DINNER):
        return MEAL_LUNCH_MINUTES + MEAL_DINNER_MINUTES
    return MEAL_LUNCH_MINUTES


def day_budget(req: Any, day_index: int = 1, total_days: int = 1) -> int:
    """某天可用于「游览 + 赶路」的分钟数（已扣当天实际要吃的餐）。

    **不乘倾向系数** —— 出发 / 返回时间是用户定的硬约束，物理时间不会被「紧凑」
    抻长 15%。早先乘了 `PACE_FACTOR`，等于凭空多给 54 分钟（420 分钟的时间窗
    算成 480），最后一天 16:00 赶返程就会溢出。
    倾向的差异体现在「每天目标游玩时长」（`PACE_TARGET_MINUTES`）和弹性填充上。
    """
    start_s, end_s = day_window(req, day_index, total_days)
    start, end = parse_hhmm(start_s), parse_hhmm(end_s)
    if end <= start:
        end += 24 * 60
    return max(180, (end - start) - meal_minutes(end_s))


def parse_hhmm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def fmt_hhmm(total_minutes: int) -> str:
    total_minutes = int(total_minutes)
    # 跨天时折回 24 小时制，避免出现 25:10 这种时间
    total_minutes %= 24 * 60
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _is_standalone(city: Any, item: Any, transport: str) -> bool:
    """是否属于「独占型」：自己就要吃掉一整天。"""
    return item.visit_minutes >= STANDALONE_MINUTES or travel_from(
        city, item, transport
    ) >= STANDALONE_TRAVEL_MIN
    

# ================================================================ 时段硬约束
# 少数景点的最佳时段是硬知识，写死在代码里比让模型猜可靠得多：
#   · 看动物的要赶早（熊猫午后基本在睡觉，14:00 进去等于看睡姿）
#   · 夜景类要等亮灯（白天去看洪崖洞等于白跑）
#   · 博物馆闭馆早（多数 17:00 关门，15:30 后进不去）
# 命中规则后 build_day 会硬性拒绝，逼模型把景点挪到合适的位置——
# 实测「在提示词里写一遍」是拦不住的，必须在工具层拦。

# 时段硬知识的**权威来源是景点的 `best_time` 字段**（seed 时由 LLM 标注，换城市零改动）。
# 下面这组关键词只是**兜底**：早期数据没有该字段时，仍按名称/标签猜一次。
_MORNING_KEYS = ("熊猫", "动物园", "植物园", "繁育研究基地")
_NIGHT_KEYS = ("洪崖洞", "不夜城", "九眼桥", "夜景", "夜游", "酒吧", "灯光秀", "兰桂坊")
_MUSEUM_KEYS = ("博物馆", "博物院", "纪念馆", "美术馆")


@dataclass(frozen=True)
class TimeRule:
    kind: str            # morning / night / museum
    label: str           # 一句话说明，回给模型
    latest_start: str    # 最晚开始时间，"" 表示无限制
    earliest_start: str  # 最早开始时间，"" 表示无限制


# best_time 取值 → 具体约束。**改这一张表就能调整所有城市的时段规则。**
_TIME_RULES: dict[str, TimeRule] = {
    "morning": TimeRule("morning", "看动物要赶早，午后动物多在半睡", "12:00", ""),
    "night": TimeRule("night", "夜景类要等亮灯后才有意义", "", "15:00"),
    "museum": TimeRule("museum", "博物馆一般 17:00 闭馆", "15:30", ""),
}


def time_rule(item: Any) -> TimeRule | None:
    """判断景点的时段约束，命中不了返回 None。

    优先读 `best_time` 字段（seed 时由 LLM 标注，**换城市零改动**）；
    字段为空时回退到名称/标签关键词，兼容早期数据。
    """
    kind = str(getattr(item, "best_time", "") or "").strip().lower()
    if kind in _TIME_RULES:
        return _TIME_RULES[kind]

    text = f"{getattr(item, 'name', '') or ''} {' '.join(getattr(item, 'tags', None) or [])}"
    if any(k in text for k in _MORNING_KEYS):
        return _TIME_RULES["morning"]
    if any(k in text for k in _NIGHT_KEYS):
        return _TIME_RULES["night"]
    if any(k in text for k in _MUSEUM_KEYS):
        return _TIME_RULES["museum"]
    return None


def rule_sort_key(item: Any) -> int:
    """排日顺序：早场类必须打头，夜景类必须压尾。"""
    rule = time_rule(item)
    if rule is None:
        return 1
    return {"morning": 0, "night": 2, "museum": 1}[rule.kind]


def apply_time_rules(items: Sequence[Any]) -> list[Any]:
    """稳定排序：早场类提前、夜景类推后，其余保持原有顺路顺序。"""
    return sorted(items, key=rule_sort_key)


def rule_violations(day_plan: DayPlan, by_id: dict[int, Any]) -> list[str]:
    """检查已物化的时间轴有没有违反时段规则。"""
    out: list[str] = []
    for n in day_plan.nodes:
        if n.type != "attraction" or n.attraction_id is None:
            continue
        item = by_id.get(n.attraction_id)
        if item is None:
            continue
        rule = time_rule(item)
        if rule is None:
            continue
        if rule.latest_start and n.time > rule.latest_start:
            out.append(
                f"「{item.name}」{n.time} 才开始，{rule.label}，最晚应 {rule.latest_start} 前进入"
            )
        elif rule.earliest_start and n.time < rule.earliest_start:
            out.append(
                f"「{item.name}」{n.time} 就去了，{rule.label}，建议 {rule.earliest_start} 之后再去"
            )
    return out


# ================================================================ ① 容量裁剪
# 「必去」的热度线：全国 T 级 ≥500（T0 世界级 / T1 全国顶流）直接视为必去。
# 热度是**全国统一尺度**（0~1000），不是城市内相对值——故宫/长城可以到 800+，
# 同城比较时单调性不变，所以排序/性价比逻辑不受尺度影响。
MUST_HEAT = 500


def _cost(city: Any, item: Any, transport: str) -> int:
    """粗估某个景点要吃掉多少时间：游览 + 从市中心往返的路程。"""
    return int(item.visit_minutes + 2 * travel_from(city, item, transport))


def _is_must(item: Any) -> bool:
    """「必去」判定——与 materialize_day 里写 must_visit 的口径保持一致。"""
    return bool(getattr(item, "must_visit", False)) or (
        getattr(item, "heat", 0) or 0
    ) >= MUST_HEAT


def _value(city: Any, item: Any, transport: str) -> float:
    """性价比：单位时间能换来多少「值得去」。必去景点给加成。"""
    bonus = 30 if _is_must(item) else 0
    return (item.heat + bonus) / max(1, _cost(city, item, transport))


def _drop_reason(city: Any, item: Any, transport: str, days: int) -> str:
    t = travel_from(city, item, transport)
    hours = item.visit_minutes / 60
    if _is_standalone(city, item, transport):
        if t >= STANDALONE_TRAVEL_MIN:
            return (
                f"单程约 {t} 分钟、游览 {hours:.1f} 小时，基本要占掉一整天；"
                f"{days} 天预算装不下市区线 + 远郊，按性价比取舍后舍弃。"
            )
        return (
            f"游览 {hours:.1f} 小时，本身就占掉大半天；{days} 天预算装不下，"
            f"按性价比取舍后舍弃（建议单独留半天再来）。"
        )
    return f"{days} 天装不下全部勾选景点，按「热度 ÷ 耗时」排序后它排在最后，取舍后舍弃。"


def prune_to_capacity(
    city: Any,
    attractions: Sequence[Any],
    req: PlanRequest,
    capacity_minutes: int | None = None,
) -> tuple[list[Any], list[DroppedItem]]:
    """总时长超容量时，按性价比从低到高丢弃，直到装得下。

    先裁再排，是为了避免「排到最后一天才发现塞不下，结果把必去景点丢了」——
    那种情况下丢掉的往往不是最该丢的那个。

    `capacity_minutes` 是「逐天可用预算之和」；**不传就自己按逐天求和算**。
    早先用 `min(单天预算) × 天数` 是错的：首末天时间窗不同时（第一天中午才到、
    最后一天要赶飞机），会把整趟都按最紧的那天算，容量低估 40% 以上 → 误判超载。
    """
    kept = list(attractions)
    dropped: list[DroppedItem] = []
    if capacity_minutes is None:
        capacity_minutes = sum(
            day_budget(req, i, req.days) for i in range(1, req.days + 1)
        )
    capacity = int(capacity_minutes * CAPACITY_SLACK)

    while len(kept) > 1:
        total = sum(_cost(city, a, req.transport) for a in kept)
        if total <= capacity:
            break
        # 先丢普通景点；普通景点丢完了仍装不下，才轮到必去（must_visit 或热度 ≥90）。
        # 早先只靠 _value 的 +30 加成做倾斜，实测抵不过远郊景点的高耗时——
        # 成都「前 10 景 × 1 天」会把必去的大熊猫基地丢掉。
        pool = [a for a in kept if not _is_must(a)] or kept
        victim = min(pool, key=lambda a: _value(city, a, req.transport))
        kept.remove(victim)
        dropped.append(
            DroppedItem(
                name=victim.name,
                reason=_drop_reason(city, victim, req.transport, req.days),
                attraction_id=victim.id,
            )
        )
    return kept, dropped


# ================================================================ ②③ 排序与分天
def _nn_chain(items: Sequence[Any], start: Any) -> list[Any]:
    """从 start 出发的最近邻贪心链——「顺路」的最朴素实现。"""
    remaining = [x for x in items if x is not start]
    chain = [start]
    cur = start
    while remaining:
        nxt = min(remaining, key=lambda x: haversine_m(cur.lat, cur.lng, x.lat, x.lng))
        chain.append(nxt)
        remaining.remove(nxt)
        cur = nxt
    return chain


# ---------------------------------------------------------------- 分级与地理聚类
def grade_attraction(item: Any) -> str:
    """按基础游览时长给景点分级：小 / 中 / 大。

    级别决定「一天能放几个」——小景点可顺路带两个，大景点自己占一天。
    只依赖 visit_minutes，所以换城市零成本。
    """
    minutes = getattr(item, "visit_minutes", 90) or 90
    if minutes <= GRADE_SMALL_MAX:
        return "小"
    if minutes <= GRADE_MEDIUM_MAX:
        return "中"
    return "大"


def is_remote(city: Any, item: Any, transport: str) -> bool:
    """远郊：单程通行 ≥45 分钟。与级别是两个独立维度。"""
    return travel_from(city, item, transport) >= STANDALONE_TRAVEL_MIN


def travel_minutes(city: Any, a: Any, b: Any, transport: str) -> int:
    """两景点之间的通行分钟数——分天矩阵的最小单元。"""
    return leg(city, a.lat, a.lng, b.lat, b.lng, transport).minutes


def travel_matrix(
    city: Any, attractions: Sequence[Any], transport: str
) -> dict[tuple[int, int], int]:
    """景点两两通行时间矩阵，键为 (小 id, 大 id)。

    默认用 leg() 的分档速度估算；「直线 1.5~8km」的模糊对可由 amap.route_minutes()
    用真实路网覆盖（见 amap.py 的节制策略）。
    """
    out: dict[tuple[int, int], int] = {}
    items = list(attractions)
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            lo, hi = (a, b) if a.id < b.id else (b, a)
            out[(lo.id, hi.id)] = travel_minutes(city, lo, hi, transport)
    return out


def cluster_attractions(
    city: Any,
    attractions: Sequence[Any],
    transport: str,
    threshold_m: float = CLUSTER_MAX_METERS,
) -> list[list[Any]]:
    """按**直线距离**把景点聚成「片区簇」——步行可达的归为一簇。

    先最近邻成链（保证地理顺路），再在「相邻直线距离 > threshold_m」处断开。
    用直线距离而不是通行时间，是为了避开速度模型的两个噪音：
    「1.5km 内一律步行」和「耗时取整到 5 分钟」——它们会让 0.6km 与 1.4km
    都算步行、耗时却差三倍，导致阈值判断在 8~15 分钟之间出现悬崖。

    **簇是分天的原子单位，簇内景点不会被拆到不同天** —— 所以像
    锦里古街↔武侯祠（实测 0.24km、步行 5 分钟）这种组合必然落在同一天。
    """
    items = list(attractions)
    if not items:
        return []
    if len(items) == 1:
        return [items]

    anchor = max(items, key=lambda a: (a.heat, a.visit_minutes))
    chain = _nn_chain(items, anchor)

    clusters: list[list[Any]] = [[chain[0]]]
    for prev, item in zip(chain, chain[1:]):
        if haversine_m(prev.lat, prev.lng, item.lat, item.lng) <= threshold_m:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    return clusters


def _split_cluster(
    city: Any,
    group: Sequence[Any],
    transport: str,
    threshold_m: float = CLUSTER_MAX_METERS,
) -> tuple[list[Any], list[Any]] | None:
    """把簇切成两半：两侧游览时长尽量接近，但**绝不在紧邻对处切**。

    切点必须落在「相邻直线距离 > threshold_m」的真断点上；若整个簇在阈值内
    串成一片，返回 None 表示它是一个地理整体、不该拆。
    """
    if len(group) < 2:
        return None
    chain = _nn_chain(group, max(group, key=lambda a: (a.heat, a.visit_minutes)))
    total = sum(a.visit_minutes for a in chain)

    best_cut, best_score = None, None
    acc = 0
    for k in range(1, len(chain)):
        acc += chain[k - 1].visit_minutes
        gap_m = haversine_m(
            chain[k - 1].lat, chain[k - 1].lng, chain[k].lat, chain[k].lng
        )
        if gap_m <= threshold_m:
            continue                       # 紧邻对，不许切
        score = abs(acc - (total - acc))   # 两边时长越接近越好
        if best_score is None or score < best_score:
            best_cut, best_score = k, score
    if best_cut is None:
        return None
    return chain[:best_cut], chain[best_cut:]


def _group_load(city: Any, group: Sequence[Any], transport: str) -> int:
    """一组景点排成一天要占用多少分钟 = **游览 + 路程**（含从市中心往返）。

    只算游览会严重低估：远郊景点游览 1 小时、往返却要 3 小时。
    顺序按传入顺序——分配阶段用的是最近邻链序，本身已经地理顺路。
    """
    items = list(group)
    if not items:
        return 0
    visit = sum(a.visit_minutes for a in items)
    travel = 0
    lat, lng = city.center_lat, city.center_lng
    for a in items:
        travel += leg(city, lat, lng, a.lat, a.lng, transport).minutes
        lat, lng = a.lat, a.lng
    travel += leg(city, lat, lng, city.center_lat, city.center_lng, transport).minutes
    return visit + travel


def _alloc_clusters_to_days(
    city: Any,
    clusters: Sequence[Sequence[Any]],
    days: int,
    transport: str,
    target_minutes: int,
) -> list[list[Any]]:
    """把片区簇分配到 days 天。簇是原子——只有必要时才合并或拆分。

    一天的「量」= 游览 + 路程（见 `_group_load`），不是只看游览时长。

    簇太多 → 合并「地理最近」的两簇（优先合并不超目标时长的）。
    簇太少 → 只拆「内部有 >CLUSTER_MAX_METERS 断点」的簇；全都紧凑时
             **宁可让某些天空着**，也不违反「地理优先」把紧邻景点拆开 ——
             紧凑度会据此提示「天数偏多」。
    """
    groups = [list(c) for c in clusters if c]
    if not groups:
        return [[] for _ in range(days)]

    def size(g: Sequence[Any]) -> int:
        return _group_load(city, g, transport)

    while len(groups) > days:
        best_i, best_score = 0, None
        for i in range(len(groups) - 1):
            a, b = groups[i], groups[i + 1]
            gap = min(haversine_m(x.lat, x.lng, y.lat, y.lng) for x in a for y in b)
            over = max(0, size(a) + size(b) - target_minutes)
            score = (over, gap)
            if best_score is None or score < best_score:
                best_i, best_score = i, score
        groups[best_i] = groups[best_i] + groups[best_i + 1]
        del groups[best_i + 1]

    while len(groups) < days:
        cands = []
        for i, g in enumerate(groups):
            split = _split_cluster(city, g, transport)
            if split:
                cands.append((sum(a.visit_minutes for a in g), i, split))
        if not cands:
            break
        _, i, (left, right) = max(cands, key=lambda x: x[0])   # 优先拆时长最大的
        groups[i:i + 1] = [left, right]

    groups.extend([] for _ in range(days - len(groups)))
    return groups[:days]


def assign_days(
    city: Any,
    attractions: Sequence[Any],
    days: int,
    transport: str,
    pace: str = "balanced",
) -> list[list[Any]]:
    """把景点分配到每一天。**地理优先**：片区簇是骨架，簇内不可拆。

    规则（顺序不能换）：
      ① 大景点 / 远郊 → 独占一天（它们本身就装不下别的）
      ② 其余按通行时间聚成片区簇
      ③ 簇分配到天：簇太多就合并，太少只在「真断点」处拆
      ④ 天与天按地理顺路排序，减少跨天折返
      ⑤ 每天内部应用时段硬规则（早场打头、夜景压尾）

    这样像锦里古街↔武侯祠（步行 5 分钟）这类紧邻景点必然落在同一天，
    不会再出现「按游览时长均分、恰好把它俩切开」的问题。
    """
    if not attractions:
        return [[] for _ in range(days)]

    standalone = [a for a in attractions if _is_standalone(city, a, transport)]
    standalone_ids = {a.id for a in standalone}
    normal = [a for a in attractions if a.id not in standalone_ids]

    standalone.sort(key=lambda a: (-a.heat, -a.visit_minutes))
    if len(standalone) > days:
        # 独占型比天数还多，多出来的回到普通池，交给容量裁剪/逐天裁剪处理
        normal.extend(standalone[days:])
        standalone = standalone[:days]

    groups: list[list[Any]] = [[a] for a in standalone]
    left_days = days - len(groups)

    if normal:
        if left_days > 0:
            clusters = cluster_attractions(city, normal, transport)
            target = PACE_TARGET_MINUTES.get(pace, PACE_TARGET_MINUTES["balanced"])
            groups.extend(
                _alloc_clusters_to_days(city, clusters, left_days, transport, target)
            )
        else:
            # 没位置了，塞进最后一个独占日，随后由逐天裁剪决定去留
            groups[-1].extend(normal)

    groups.extend([] for _ in range(days - len(groups)))
    groups = groups[:days]

    # 让第 k 天从「离上一天收尾最近」的那端开始，减少跨天折返
    for k in range(1, len(groups)):
        prev, cur = groups[k - 1], groups[k]
        if len(cur) < 2 or not prev:
            continue
        tail = prev[-1]
        head_gap = haversine_m(tail.lat, tail.lng, cur[0].lat, cur[0].lng)
        rev_gap = haversine_m(tail.lat, tail.lng, cur[-1].lat, cur[-1].lng)
        if rev_gap < head_gap:
            groups[k] = list(reversed(cur))

    # 时段硬约束：早场类（熊猫基地）必须打头，夜景类（洪崖洞）必须压尾
    return [apply_time_rules(g) for g in groups]


def pick_attractions(
    city: Any,
    attractions: Sequence[Any],
    days: int,
    pace: str = "balanced",
    transport: str = "mixed",
) -> list[Any]:
    """一键 AI：按「天数 + 节奏」自动挑选景点，用户不必逐个勾选。

    原则与分天保持一致——**必去优先，其次热度**，累计游览时长逼近
    「天数 × 节奏目标游玩时长」，超出 10% 后收口。
    选出来的集合照常交给 assign_days 做地理优先分天。
    """
    items = list(attractions)
    if not items or days <= 0:
        return []

    target = days * PACE_TARGET_MINUTES.get(pace, PACE_TARGET_MINUTES["balanced"])
    ordered = sorted(items, key=lambda a: (0 if getattr(a, "must_visit", False) else 1, -a.heat))

    picked: list[Any] = []
    acc = 0
    for a in ordered:
        if picked and acc + a.visit_minutes > target * 1.1:
            break
        # 独占型（大景点 / 远郊）一天只能放一个，超出天数就别再选了
        if _is_standalone(city, a, transport) and len(picked) >= days:
            continue
        picked.append(a)
        acc += a.visit_minutes

    return picked or items[:1]


def estimate_days_needed(
    city: Any, attractions: Sequence[Any], transport: str
) -> int:
    """「按片区排最舒服需要几天」——用于给用户提示，**不用作硬判定**。

    口径是分天算法的地理理想值：大景点 / 远郊各占一天，其余每个片区簇占一天
    （簇不可拆）。实际天数不够时 `assign_days` 会合并簇、每天跨片区跑，
    所以它只是「舒适下限」，不是「能不能排下」的界限。
    """
    items = list(attractions)
    if not items:
        return 0
    standalone = [a for a in items if _is_standalone(city, a, transport)]
    sids = {a.id for a in standalone}
    normal = [a for a in items if a.id not in sids]
    clusters = cluster_attractions(city, normal, transport) if normal else []
    return len(standalone) + len(clusters)


# ================================================================ ④ 逐天裁剪
def trim_day(
    city: Any,
    items: Sequence[Any],
    req: PlanRequest,
    per_day_budget: int,
    origin: tuple[float, float] | None = None,
    depart: tuple[float, float] | None = None,
) -> tuple[list[Any], list[Any]]:
    """按当天真实时间预算裁剪，返回 (保留, 溢出)。

    预算里含「最后一站回住宿片区」的返程时间——不算返程的话，
    远郊景点会看着能塞进去，实际收尾时间要晚一个多小时。
    origin 是当天过夜点（收尾点）；depart 是出发地（昨晚过夜点，转场日与 origin 不同）；
    两者缺省都用 city.center（城市目的地的老口径）。
    至少保留 1 个（否则一整天就空了）。
    """
    kept: list[Any] = []
    overflow: list[Any] = []
    used = 0
    olat, olng = origin if origin else (city.center_lat, city.center_lng)
    dlat, dlng = depart if depart else (olat, olng)
    lat, lng = dlat, dlng

    for item in items:
        t = leg(city, lat, lng, item.lat, item.lng, req.transport).minutes
        back = leg(city, item.lat, item.lng, olat, olng, req.transport).minutes
        if kept and used + t + item.visit_minutes + back > per_day_budget:
            overflow.append(item)
            continue
        used += t + item.visit_minutes
        lat, lng = item.lat, item.lng
        kept.append(item)
    return kept, overflow


# ================================================================ 住宿取舍
def pick_hotel(city: Any, attractions: Sequence[Any]) -> HotelAdvice:
    """选离这组景点几何中心最近的住宿片区。"""
    areas: list[dict] = list(city.hotel_areas or [])
    if not areas:
        return HotelAdvice(
            area=f"{city.name}市中心",
            reason="未配置候选住宿片区，默认建议住在市中心，便于向各方向辐射。",
            alternatives=[],
        )

    def avg_distance(area: dict) -> float:
        if not attractions:
            return 0.0
        return sum(
            haversine_m(area["lat"], area["lng"], a.lat, a.lng) for a in attractions
        ) / len(attractions)

    ranked = sorted(areas, key=avg_distance)
    best = ranked[0]
    dist_km = avg_distance(best) / 1000.0

    reason = f"到本次各景点平均单程约 {dist_km:.1f} km，位置最居中。"
    if best.get("pros"):
        reason += str(best["pros"])

    return HotelAdvice(
        area=str(best["area"]),
        reason=reason,
        alternatives=[
            HotelAlternative(
                area=str(x["area"]),
                tradeoff=str(x.get("cons") or "位置略偏，但周边环境/价格可能更优。"),
            )
            for x in ranked[1:3]
        ],
    )


def _avg_km_hotel(city: Any, hotel: HotelAdvice, items: Sequence[Any]) -> float:
    """某住宿片区到一组景点的平均直线距离（km）。"""
    hlat, hlng = _hotel_origin(city, hotel)
    if not items:
        return 0.0
    return (
        sum(haversine_m(hlat, hlng, a.lat, a.lng) for a in items)
        / len(items)
        / 1000.0
    )


def plan_hotels(city: Any, groups: Sequence[Sequence[Any]]) -> list[HotelAdvice]:
    """一次定**全程**住宿。城市目的地与区域游目的地的策略不同：

    city（城市+周边，现状）：按「全程景点的几何中心」选一个常住片区，
        只有远郊日（如成都的都江堰日）才建议就近住一晚，次日回常住片区。
    region（区域游，贵州这类多基地环线）：**今晚住哪 = 今天走到哪** ——
        第一晚住当天景点就近基地；连住优先（不搬行李），
        当天景点离昨晚过夜点太远且本地有明显更优基地时才搬。
    """
    all_items = [a for g in groups for a in g]
    base = pick_hotel(city, all_items)

    if getattr(city, "kind", "city") == "region":
        return _plan_hotels_region(city, groups, base)

    base_lat, base_lng = _hotel_origin(city, base)

    out: list[HotelAdvice] = []
    for items in groups:
        if not items:
            out.append(base)
            continue

        avg_km = (
            sum(haversine_m(base_lat, base_lng, a.lat, a.lng) for a in items)
            / len(items)
            / 1000.0
        )
        if avg_km <= OVERNIGHT_KM:
            out.append(base)
            continue

        # 当天离常住片区太远 → 看看本地有没有更合适的片区
        local = pick_hotel(city, items)
        local_lat, local_lng = _hotel_origin(city, local)
        local_km = (
            sum(haversine_m(local_lat, local_lng, a.lat, a.lng) for a in items)
            / len(items)
            / 1000.0
        )
        if local.area != base.area and local_km < avg_km * 0.6:
            out.append(
                HotelAdvice(
                    area=local.area,
                    reason=(
                        f"当天在「{local.area}」一带，离常住片区「{base.area}」平均 {avg_km:.0f} km，"
                        f"往返太远，建议就近住一晚（本地片区平均单程 {local_km:.1f} km）。"
                    ),
                    alternatives=[HotelAlternative(area=base.area, tradeoff="不用搬行李，但当天来回要多花 2~3 小时车程。")],
                )
            )
        else:
            out.append(
                HotelAdvice(
                    area=base.area,
                    reason=(
                        f"继续住「{base.area}」（全程不换酒店，省去搬行李）。"
                        f"注意当天景点平均单程 {avg_km:.0f} km，建议早出发、当天行程别再加密。"
                    ),
                    alternatives=base.alternatives,
                )
            )
    return out


def _plan_hotels_region(
    city: Any, groups: Sequence[Sequence[Any]], base: HotelAdvice
) -> list[HotelAdvice]:
    """区域游的推进式住宿：链式转场的核心。

    规则（都服务于「少搬行李 + 明天不从 300km 外瞎跑」）：
      ① 第一晚：住第一天景点就近的基地（base 是全程几何中心，不一定是起点）
      ② 连住优先：当天景点离昨晚过夜点 ≤OVERNIGHT_KM → 继续住，不动
      ③ 换住：太远、且本地基地平均距离 < 昨晚基地的 60% → 搬过去
      ④ 换不动（本地没有明显更优）→ 留在原地硬跑，reason 里提醒早出发
    """
    out: list[HotelAdvice] = []
    cur: HotelAdvice | None = None

    for items in groups:
        if not items:
            out.append(cur or base)
            continue

        local = pick_hotel(city, items)
        if cur is None:
            cur = local  # ① 第一晚
            out.append(cur)
            continue

        cur_km = _avg_km_hotel(city, cur, items)
        if cur_km <= OVERNIGHT_KM:
            out.append(
                HotelAdvice(
                    area=cur.area,
                    reason=f"继续住「{cur.area}」，离今天的景点近，不搬行李。",
                    alternatives=cur.alternatives,
                )
            )
            continue

        local_km = _avg_km_hotel(city, local, items)
        if local.area != cur.area and local_km < cur_km * 0.6:
            cur = HotelAdvice(
                area=local.area,
                reason=(
                    f"今天玩「{local.area}」一带，从「{out[-1].area}」过去平均 {cur_km:.0f} km，"
                    f"今晚搬过来住（本地平均单程 {local_km:.1f} km），明天从这里出发。"
                ),
                alternatives=[
                    HotelAlternative(area=out[-1].area, tradeoff="不搬行李，但明天一早要多花 2 小时以上车程折返。")
                ],
            )
        else:
            cur = HotelAdvice(
                area=cur.area,
                reason=(
                    f"继续住「{cur.area}」硬跑今天这一线（换住省不了多少路）。"
                    f"当天平均单程 {cur_km:.0f} km，务必早出发，行程别再加密。"
                ),
                alternatives=cur.alternatives,
            )
        out.append(cur)
    return out


# ================================================================ 单日构建
def _meal_name(city_name: str, area: str, kind: str) -> str:
    """餐饮节点不写具体店名——店铺随时会换，写了反而误导。

    改为「区域 + 本地特色」，具体吃什么放在 advice 里。
    """
    where = (area or "").strip() or f"{city_name}市区"
    return f"{where}·本地特色{'午餐' if kind == 'lunch' else '晚餐'}"


def _meal_advice(kind: str, hint: str, area: str) -> str:
    base = (
        "就近在当天片区解决，别为了一顿饭跨区，会打乱下午的动线。"
        if kind == "lunch"
        else "晚餐安排在住宿片区附近，吃完直接回酒店，不用再折腾交通。"
    )
    if hint:
        return f"推荐本地特色：{hint}。{base}"
    if area:
        return f"在「{area}」一带找家本地小馆即可。{base}"
    return base


def _hotel_origin(city: Any, hotel: HotelAdvice) -> tuple[float, float]:
    for area in city.hotel_areas or []:
        if area.get("area") == hotel.area:
            return area["lat"], area["lng"]
    return city.center_lat, city.center_lng


def _make_theme(city_name: str, items: Sequence[Any]) -> str:
    if not items:
        return f"{city_name}市区自由漫步 · 机动日"
    names = [a.name for a in items]
    districts = [a.district for a in items if a.district]
    if districts and len(set(districts)) == 1:
        prefix = f"{districts[0]}深度线"
    elif districts:
        prefix = f"{districts[0]}—{districts[-1]} 顺路线"
    else:
        prefix = f"{city_name}市区经典线"
    return f"{prefix}｜{' → '.join(names[:3])}"[:80]


def _estimate_travel(
    city: Any,
    items: Sequence[Any],
    origin_lat: float,
    origin_lng: float,
    transport: str,
    end_lat: float | None = None,
    end_lng: float | None = None,
) -> int:
    """按给定顺序预估当天赶路总时长（含最后一站回住宿片区）。

    origin 是出发地，end 是收尾地；转场日两者不同
    （昨晚退房处 → … → 今晚入住处），缺省同为一点。
    """
    end_lat = origin_lat if end_lat is None else end_lat
    end_lng = origin_lng if end_lng is None else end_lng
    total = 0
    lat, lng = origin_lat, origin_lng
    for a in items:
        total += leg(city, lat, lng, a.lat, a.lng, transport).minutes
        lat, lng = a.lat, a.lng
    total += leg(city, lat, lng, end_lat, end_lng, transport).minutes
    return total


def _flex_scale(
    city: Any, items: Sequence[Any], req: PlanRequest, base_total: int, available: int
) -> float:
    """当天游玩时长的放大倍数——**弹性填充**。

    景点排得少的日子，余量不该浪费成一个假的「自由活动」节点，而应还给景点本身：

        最终游玩 = clamp(可用游玩 × r, 基础游玩, 基础游玩 × m)

    r / m 按节奏取（轻松按最少、平衡适当、紧凑填满）；**大景点与远郊不受节奏限制**
    —— 都江堰玩一整天很正常，不该因为选了「轻松」就只逛 4 小时。
    """
    if base_total <= 0 or available <= 0:
        return 1.0
    ratio, cap = PACE_FLEX.get(req.pace, PACE_FLEX["balanced"])
    if any(
        a.visit_minutes > GRADE_MEDIUM_MAX
        or travel_from(city, a, req.transport) >= STANDALONE_TRAVEL_MIN
        for a in items
    ):
        cap = max(cap, BIG_FLEX_MAX)
    return max(1.0, min(available * ratio / base_total, cap))


def materialize_day(
    city: Any,
    day_index: int,
    items: Sequence[Any],
    req: PlanRequest,
    *,
    total_days: int | None = None,
    theme: str = "",
    advice: dict[int, str] | None = None,
    lunch_area: str = "",
    lunch_hint: str = "",
    dinner_area: str = "",
    dinner_hint: str = "",
    hotel: HotelAdvice | None = None,
    depart_from: HotelAdvice | None = None,
    moved_from: dict[int, dict] | None = None,
) -> DayPlan:
    """把「当天去哪几个景点」物化成完整时间轴。

    这是全项目唯一产出时间的地方，也是「LLM 只出大纲、硬编码跑时间」的分界线：
      · 入参 items / theme / advice / 餐饮区域 / 酒店 可以来自 LLM 的大纲；
      · 所有 time、stay_minutes、travel_minutes、饭点位置、pace 都由本函数算，
        LLM 无从插手，因此不可能算错时间、漏插饭点或把景点排两遍。

    餐饮只收「区域 + 本地特色提示」，不收具体店名——店铺随时会换。
    items 必须已经过 trim_day 裁剪，本函数不再做容量判断。
    """
    items = list(items)
    total_days = total_days or day_index
    moved_from = moved_from or {}
    advice = advice or {}

    start_s, end_s = day_window(req, day_index, total_days)
    start_min = parse_hhmm(start_s)
    return_min = parse_hhmm(end_s)
    if return_min <= start_min:
        return_min += 24 * 60  # 允许跨零点，如 09:00 → 01:00

    # 住宿片区：调用方给了就用它的（全程统一或远郊过夜），否则按当天景点群几何中心选
    hotel = hotel or pick_hotel(city, items)
    origin_lat, origin_lng = _hotel_origin(city, hotel)          # 今晚的住处（收尾点）
    # 转场日：昨晚住在别处（region 环线的上一站），今早从那里退房出发
    dep_hotel = depart_from or hotel
    depart_lat, depart_lng = _hotel_origin(city, dep_hotel)
    is_transit = dep_hotel.area != hotel.area

    nodes: list[TimelineNode] = []
    cur_time = start_min
    cur_lat, cur_lng = depart_lat, depart_lng

    def push(node: TimelineNode) -> None:
        """所有节点统一从这里进——保证「到达 = 上一站离开 + 路程」永远成立。"""
        nonlocal cur_time
        nodes.append(node)
        cur_time = parse_hhmm(node.time) + node.stay_minutes

    def add_meal(kind: str, at: int, district: str, gap: int | None = None) -> None:
        """插一个餐饮节点。

        gap 是「从上一站到餐厅」的时间。不传则按 at - cur_time 反推，
        这样即使为了凑饭点等了半小时，时间轴依然是自洽的。
        """
        stay = MEAL_LUNCH_MINUTES if kind == "lunch" else MEAL_DINNER_MINUTES
        area = lunch_area if kind == "lunch" else dinner_area
        hint = lunch_hint if kind == "lunch" else dinner_hint
        # 晚餐回到住宿片区吃；午餐用当天所在的区
        where = area or (hotel.area if kind == "dinner" else district)
        if gap is None:
            gap = max(0, at - cur_time)
        push(
            TimelineNode(
                time=fmt_hhmm(at),
                type="meal",
                name=_meal_name(city.name, where, kind),
                advice=_meal_advice(kind, hint, where),
                stay_minutes=stay,
                travel_minutes=gap,
            )
        )

    def push_attr(
        at: int,
        item: Any,
        stay: int,
        travel: int,
        mode: str,
        moved: dict | None,
        resumed: bool = False,
    ) -> None:
        """压入一个景点节点。

        大景点跨越午饭窗口时会压两个（上午段 + 下午段），
        这样午饭仍然落在正常饭点，而不是被拖到 15:00 之后。
        """
        push(
            TimelineNode(
                time=fmt_hhmm(at),
                type="attraction",
                name=f"{item.name}（下午继续）" if resumed else item.name,
                attraction_id=item.id,
                # 优先用大纲给的建议（模型写的更具体：哪个门进、几点人少、要不要预约）
                advice=advice.get(item.id) or item.intro or "建议留足时间慢慢逛。",
                stay_minutes=stay,
                travel_minutes=travel,
                travel_mode="步行" if resumed else mode,
                must_visit=_is_must(item),
                moved_to_day=day_index if moved else None,
                moved_from_day=moved["from_day"] if moved else None,
                move_reason=moved["reason"] if moved else "",
            )
        )

    push(
        TimelineNode(
            time=fmt_hhmm(start_min),
            type="depart",
            name=(
                f"从「{dep_hotel.area}」退房出发" if is_transit
                else f"从「{dep_hotel.area}」出发"
            ),
            advice=(
                (
                    f"转场日：昨晚住在「{dep_hotel.area}」，今天先赶路去「{hotel.area}」一带，"
                    f"行李带上车，下午游玩后直接入住新住处。"
                )
                if is_transit
                else {
                    "drive": "自驾建议提前确认停车场位置，景区周边车位紧张。",
                    "transit": "地铁早高峰较挤，避开 8:00-9:00 换乘大站。",
                }.get(req.transport, "早高峰建议提前 10 分钟叫车，先到最远的一站再往回走。")
            ),
            stay_minutes=0,
            travel_minutes=0,
        )
    )

    # ---------- 弹性游玩时长 ----------
    base_total = sum(a.visit_minutes for a in items)
    # 转场日从「昨晚住处」起算：depart → 景点 → … → 今晚住处，一段不多一段不少
    travel_est = _estimate_travel(city, items, depart_lat, depart_lng, req.transport, end_lat=origin_lat, end_lng=origin_lng)
    window = return_min - start_min
    available = window - meal_minutes(end_s) - travel_est
    scale = _flex_scale(city, items, req, base_total, available)

    def stretch(item: Any) -> int:
        """按当天余量放大后的游览时长，对齐到 5 分钟。"""
        return max(5, int(round(item.visit_minutes * scale / 5.0) * 5))

    lunch_done = False
    for item in items:
        go = leg(city, cur_lat, cur_lng, item.lat, item.lng, req.transport)
        travel = go.minutes
        arrive = cur_time + travel

        # 午饭要落在 11:20-14:00 之间。若这个景点会把午饭挤到 14:00 之后，就「到了先吃」；
        # 但等待超过 75 分钟就不值得等（比如 09:00 就到华山脚下），那种情况改为下来后再吃。
        if not lunch_done:
            wait = max(0, parse_hhmm(LUNCH_FROM) - arrive)
            if arrive + stretch(item) > parse_hhmm("14:00") and wait <= LUNCH_MAX_WAIT:
                # 不传 gap：让它按「lunch_at - cur_time」反推，
                # 这样「路程 80 分 + 等到 11:20 的 60 分」都算进去，时间轴依然自洽
                add_meal("lunch", max(arrive, parse_hhmm(LUNCH_FROM)), item.district)
                lunch_done = True
                travel = 5
                arrive = cur_time + travel

        moved = moved_from.get(item.id)
        stay = stretch(item)
        lunch_at = parse_hhmm(LUNCH_FROM)

        # 弹性拉长后，大景点的游玩会跨过午饭窗口（如 09:15 → 15:15）。
        # 这时把午饭插在**景点中途**（上午段 → 午餐 → 下午段），而不是「玩完再吃」，
        # 否则午餐会被拖到 15:00 之后，时间轴上多出一段莫名其妙的长游览。
        morning = lunch_at - arrive
        afternoon = stay - morning
        if (
            not lunch_done
            and arrive < lunch_at < arrive + stay
            and morning >= MIN_SPLIT_MINUTES
            and afternoon >= MIN_SPLIT_MINUTES
        ):
            back_to_spot = 5      # 从餐厅走回景区只需要步行
            push_attr(arrive, item, morning, travel, go.label, moved)
            cur_lat, cur_lng = item.lat, item.lng
            add_meal("lunch", lunch_at, item.district, gap=0)
            lunch_done = True
            push_attr(
                cur_time + back_to_spot, item, afternoon, back_to_spot,
                go.label, moved, resumed=True,
            )
            cur_lat, cur_lng = item.lat, item.lng
            continue

        push_attr(arrive, item, stay, travel, go.label, moved)
        cur_lat, cur_lng = item.lat, item.lng

        # 这个景点跨过了午饭点 → 出来立刻吃，别拖到下一站
        if not lunch_done and cur_time >= parse_hhmm(LUNCH_AFTER):
            add_meal("lunch", cur_time, item.district)
            lunch_done = True

    # 午餐兜底：一天都在赶路没吃上
    if not lunch_done and items:
        add_meal("lunch", max(cur_time, parse_hhmm("12:00")), items[-1].district)

    # ---------- 收尾前的硬约束：绝不超目标返回时间 ----------
    # trim_day 的估算按「市中心往返」算、还含路程取整误差，与这里按住宿片区实算的结果
    # 可能差十几分钟。最后一天赶高铁 / 飞机时这不能接受，所以在这里做最后一次收敛：
    # 宁可压缩最后一个景点的停留，也不能让「16:00 前返回」落空。
    # 只有在「最后一天 **且** 用户显式设了返回时间」时才当作赶返程的硬约束 ——
    # 那通常意味着要赶高铁 / 飞机。中间几天回来晚十几分钟可以接受（会给超时提示），
    # 不能因此就砍掉一顿晚饭。
    strict_return = day_index >= total_days and bool(req.last_day_return_time)

    back_probe = leg(city, cur_lat, cur_lng, origin_lat, origin_lng, req.transport).minutes
    if strict_return and cur_time + back_probe > return_min:
        excess = cur_time + back_probe - return_min
        last = next((n for n in reversed(nodes) if n.type == "attraction"), None)
        if last is not None:
            cut = min(excess, max(0, last.stay_minutes - MIN_STAY_MINUTES))
            if cut > 0:
                last.stay_minutes -= cut
                note = f"（为按时返回压缩 {cut} 分钟）"
                last.advice = (last.advice[: 200 - len(note)] + note).strip()
                cur_time -= cut

    # ---------- 收尾：晚餐 → 回住宿片区 ----------
    back_leg = leg(city, cur_lat, cur_lng, origin_lat, origin_lng, req.transport)
    back = back_leg.minutes
    arrive_back = cur_time + back

    # 能不能排晚餐，取决于「吃完是否还来得及按时返回」。
    # 最后一天 16:00 赶飞机/高铁时 latest_dinner < EARLIEST_DINNER → 不排晚餐，
    # 否则 18:00 的晚餐锚点会把结束时间硬拖到 19:25（实测超时 205 分钟）。
    latest_dinner = return_min - (MEAL_DINNER_MINUTES + DINNER_TO_HOTEL)
    dinner_at: int | None = None
    if latest_dinner >= parse_hhmm(EARLIEST_DINNER):
        dinner_at = max(arrive_back, min(parse_hhmm(DINNER_FROM), latest_dinner))
        # 最后一站回来得太晚时，上面那个 max 会把晚餐顶到排不下的时刻
        # （如 18:15 回、19:30 要返程 → 吃完 19:40）。这时宁可不吃，也不超时。
        if strict_return and dinner_at + MEAL_DINNER_MINUTES + DINNER_TO_HOTEL > return_min:
            dinner_at = None

    if dinner_at is None:
        # 赶返程：回到住宿片区就收尾，不排晚餐
        push(
            TimelineNode(
                time=fmt_hhmm(arrive_back),
                type="hotel",
                name=f"返回「{hotel.area}」，准备返程",
                advice=f"{hotel.reason} 今天的目标是 {fmt_hhmm(return_min)} 前返回，行程到此收尾。",
                stay_minutes=0,
                travel_minutes=back,
                travel_mode=back_leg.label,
            )
        )
    else:
        # 下午早早收工 → 补一个留白节点，否则时间轴上会凭空缺好几个小时
        if dinner_at - arrive_back > 90:
            push(
                TimelineNode(
                    time=fmt_hhmm(arrive_back),
                    type="transit",
                    name="返回住宿片区休整 · 自由活动",
                    advice="这一段刻意留白：可以回酒店歇脚，也可以临时加一个住宿片区附近的去处。",
                    stay_minutes=dinner_at - arrive_back,
                    travel_minutes=back,
                    travel_mode=back_leg.label,
                )
            )
            add_meal("dinner", dinner_at, "", gap=0)
        else:
            add_meal("dinner", dinner_at, "")

        hotel_at = cur_time + DINNER_TO_HOTEL
        overrun = hotel_at - return_min
        advice = hotel.reason
        if overrun > OVERRUN_TOLERANCE:
            advice += (
                f" 注意：当天路程较远，实际回到酒店约 {fmt_hhmm(hotel_at)}，"
                f"比目标时间晚 {overrun} 分钟，建议这天的行程不要再加东西。"
            )
        push(
            TimelineNode(
                time=fmt_hhmm(hotel_at),
                type="hotel",
                name=f"入住「{hotel.area}」",
                advice=advice,
                stay_minutes=0,
                travel_minutes=DINNER_TO_HOTEL,
                travel_mode="步行",
            )
        )

    # 丰富度：只看「真实活动」——景点停留 + 全部赶路。
    # 必须排除留白节点（type=transit 的「自由活动」），否则一天只玩 1 小时
    # 也会被留白时长撑成「紧凑」，完全反直觉。
    active = sum(n.stay_minutes for n in nodes if n.type != "transit") + sum(
        n.travel_minutes for n in nodes
    )
    if active <= 300:
        pace = "轻松"
    elif active <= 450:
        pace = "适中"
    else:
        pace = "紧凑"

    return DayPlan(
        day=day_index,
        theme=theme or _make_theme(city.name, items),
        pace=pace,
        nodes=nodes,
        hotel=hotel,
    )


def build_day(city, day_index, items, req, moved_from=None, **kwargs) -> DayPlan:
    """纯规则模式下的单日构建（不带大纲）。保留这个名字，调用方读起来更直白。"""
    return materialize_day(city, day_index, items, req, moved_from=moved_from, **kwargs)


# ================================================================ 机动日
def empty_day(city: Any, req: PlanRequest, day_index: int, hotel: HotelAdvice) -> DayPlan:
    """没有任何景点的一天（机动日）。时间轴仍然是自洽的。"""
    start_s, end_s = day_window(req, day_index, req.days)
    start_min = parse_hhmm(start_s)
    end_min = parse_hhmm(end_s)
    if end_min <= start_min:
        end_min += 24 * 60

    nodes: list[TimelineNode] = []
    cur = start_min

    def push(node: TimelineNode) -> None:
        nonlocal cur
        nodes.append(node)
        cur = parse_hhmm(node.time) + node.stay_minutes

    push(TimelineNode(
        time=fmt_hhmm(cur), type="depart", name=f"从「{hotel.area}」出发",
        advice="这一天留白，可按体力临时加一个住宿片区附近的去处。",
        stay_minutes=0, travel_minutes=0,
    ))

    lunch_at = max(cur, parse_hhmm("12:00"))
    push(TimelineNode(
        time=fmt_hhmm(lunch_at), type="meal",
        name=_meal_name(city.name, hotel.area, "lunch"),
        advice="不赶行程，就近找家本地小馆。",
        stay_minutes=MEAL_LUNCH_MINUTES, travel_minutes=max(0, lunch_at - cur),
    ))

    dinner_at = max(cur, parse_hhmm(DINNER_FROM))
    push(TimelineNode(
        time=fmt_hhmm(dinner_at), type="meal",
        name=_meal_name(city.name, hotel.area, "dinner"),
        advice="吃完回酒店休息。",
        stay_minutes=MEAL_DINNER_MINUTES, travel_minutes=max(0, dinner_at - cur),
    ))

    hotel_at = max(cur + DINNER_TO_HOTEL, end_min)
    push(TimelineNode(
        time=fmt_hhmm(hotel_at), type="hotel", name=f"入住「{hotel.area}」",
        advice=hotel.reason, stay_minutes=0, travel_minutes=max(0, hotel_at - cur),
    ))

    return DayPlan(
        day=day_index,
        theme=f"{city.name}机动日 · 自由安排",
        pace="轻松",
        nodes=nodes,
        hotel=hotel,
    )


# ================================================================ 主入口
def plan_fallback(city: Any, attractions: Sequence[Any], req: PlanRequest) -> PlanResult:
    """规则引擎主入口，签名与 LLM 流水线一致，可互换。"""
    attractions = list(attractions)
    days = req.days

    # ① 容量裁剪（容量 = 逐天预算求和，由 prune_to_capacity 自己算）
    kept, dropped = prune_to_capacity(city, attractions, req)
    # ②③ 独占型隔离 + 成链均分
    groups = assign_days(city, kept, days, req.transport, req.pace)
    # 住宿：一次定全程，只有远郊日才换
    hotels = plan_hotels(city, groups)

    day_plans: list[DayPlan] = []
    carried: list[Any] = []
    carried_from: dict[int, dict] = {}

    for idx, group in enumerate(groups, start=1):
        hotel = hotels[idx - 1]
        # 裁剪按「当天过夜点」估往返：region 目的地的转场日若按市中心估，
        # 会把住安顺玩黄果树的组合误判成装不下
        origin = _hotel_origin(city, hotel)
        prev_hotel = hotels[idx - 2] if idx >= 2 else None
        depart = _hotel_origin(city, prev_hotel) if prev_hotel else None
        if depart and depart == origin:
            depart = None

        # 昨天没排下的先插到最前面
        items = list(carried) + list(group)
        kept_day, overflow = trim_day(
            city, items, req, day_budget(req, idx, days),
            origin=origin, depart=depart,
        )

        day_plans.append(
            build_day(
                city, idx, kept_day, req,
                moved_from=carried_from,
                total_days=days,
                hotel=hotel,
                depart_from=prev_hotel if depart else None,
            )
        )

        carried = []
        carried_from = {}
        for item in overflow:
            if idx >= days:
                dropped.append(
                    DroppedItem(
                        name=item.name,
                        reason=f"Day {idx} 装不下，且已是最后一天，无法顺延。",
                        attraction_id=item.id,
                    )
                )
            else:
                carried.append(item)
                carried_from[item.id] = {
                    "from_day": idx,
                    "reason": f"Day {idx} 排不下，顺延到 Day {idx + 1}，动线上仍然顺路。",
                }

    # 极端情况下最后一天仍有外抛
    for item in carried:
        dropped.append(
            DroppedItem(
                name=item.name,
                reason="行程最后仍未排入，建议单独安排一天或下次再去。",
                attraction_id=item.id,
            )
        )

    must_reasons = [
        MustVisitReason(
            name=a.name,
            reason=f"热度 {a.heat}｜{a.intro or '当地标志性去处'}",
            attraction_id=a.id,
        )
        for a in sorted(attractions, key=lambda x: -x.heat)[:3]
    ]

    total_hours = sum(a.visit_minutes for a in kept) / 60
    summary = (
        f"{city.name} {days} 天：按地理邻近度把 {len(kept)} 个景点排成 "
        f"{days} 条顺路线，日均游览约 {total_hours / max(1, days):.1f} 小时，"
        f"自动插入午晚餐，{req.transport_label}通勤，"
        f"住宿固定在同一片区，目标 {req.return_time} 前回到酒店。"
    )
    if dropped:
        names = "、".join(d.name for d in dropped[:3])
        summary += f" 因时间预算舍弃 {len(dropped)} 个（{names}）。"

    return PlanResult(
        city=city.name,
        days=days,
        summary=summary,
        day_plans=day_plans,
        must_visit_reasons=must_reasons,
        dropped=dropped,
    )
