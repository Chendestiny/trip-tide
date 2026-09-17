"""业务编排层。

流水线（「LLM 写内容 + 硬编码跑时间轴 + LLM 终检」的落地）：

    ① 查景点         按 city + attraction_ids 取出本次勾选的景点
    ② 硬编码基线     planner.plan_fallback() 先算一份完整方案
                     —— 既是无 Key 时的兜底，也是任何一步失败时的退路
    ③ LLM 并行流水线 llm.run_pipeline()：硬编码分天 → 每天并行生成内容 → 硬编码物化
    ④ LLM 终检       llm.review_plan()：读最终时间轴，提意见 + 润色总述
    ⑤ 落库返回       存 trip_plan 快照（入参 + 出参 + 终检 + 轨迹）

另有两个**不需要 LLM** 的轻接口（毫秒级、不花 token）：
    preview_plan()  选景点时预估紧凑度，让用户点「开始规划」前就知道结果
    adjust_plan()   按倾向微调已有方案（复用原分天，只重排时间轴）

任何一步失败都降级到 ②，并在 summary 末尾如实注明原因——静默降级会让人误判生成质量。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.trip import llm, planner, tools
from app.trip.models import Attraction, City, Spot, TripPlan
from app.trip.schemas import (
    AttractionOut,
    AutoPlanRequest,
    CityOut,
    DayPlan,
    DroppedItem,
    PlanBrief,
    PlanPreview,
    PlanRequest,
    PlanResponse,
    PlanResult,
    PlanReview,
    SpotOut,
)

logger = logging.getLogger("triptide.service")

# 首页目的地宫格的进程内缓存：{key: (写入时刻 monotonic, 结果)}。
# 目的地/景点只在 seed 时变化，而前端每次进首页都会调 `/cities`；
# 库在远端（一个来回约 40ms），缓存命中时该接口不查库。
# TTL 见 core/config.py 的 `cities_cache_ttl`；seed 完会「重算排序分 + 清缓存」。
_CITIES_CACHE_KEY = "all"
_CITIES_CACHE: dict[str, tuple[float, list[CityOut]]] = {}


# ================================================================ 查询
def resolve_city(db: Session, key: str) -> City:
    """按城市名或拼音找城市，找不到给友好提示。"""
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少 city 参数")

    city = db.scalar(select(City).where(City.name == key))
    if city is None:
        city = db.scalar(select(City).where(City.pinyin == key.lower()))
    if city is None:
        names = db.scalars(select(City.name).order_by(City.heat.desc())).all()
        raise HTTPException(
            status_code=404,
            detail=f"暂无「{key}」的景点数据，当前已开放：{'、'.join(names) or '（空）'}",
        )
    return city


def clear_cities_cache() -> None:
    """让下一次 `list_cities()` 重新读库。seed / 重算排序分之后调用。"""
    _CITIES_CACHE.clear()


# ---------------------------------------------------------------- 首页排序分
# 排序键 = 每个目的地「前 RANK_TOP 个高热度景点」的**指数加权**热度之和：
#
#     score = Σ_{i=1..min(RANK_TOP, n)}  heat_i × RANK_DECAY^(i-1) × B_i
#     其中 B_1 = RANK_FIRST_BOOST(1.5)，B_2 = B_3 = RANK_TOP3_BOOST(1.2)，其余 = 1
#
# 为什么不是简单求和（原来就是「前 10 之和」）：库里曾有 30 个目的地恰好只有 8 个景点
# ——那是 seed 给名单时的默认规模，不代表这地方只有 8 个可玩的。简单求和时，
# 这 30 个的「前 10 之和」就等于「全部景点之和」，截断没生效 → 排序键退化成
# 「景点数量 × 人均热度」，而「景点数量」这一半信号是数据录得全不全，不是值不值得去。
#
# 取 RANK_TOP=8 而非 10：8 正好是当时那 30 个目的地的实际条目数 → 68 个里 **64 个能凑满 8 条**，
# 绝大多数目的地在同一个基数上比（不足 8 条的从 35 个降到 4 个）。
# 实测（68 个目的地，逐个目的地的名次见 `scripts/check_rank.py`、重算见 `scripts/rank_cities.py`）：
#
#     | 口径                            | ρ(名次, 景点数量) | 洛阳 | 北疆 | 拉萨 | 深圳 | 桂西南 |
#     | 前10之和（最旧）                 |     +0.858       |  21  |  53  |   9  |  32  |   44   |
#     | 前8之和                          |     +0.770       |  12  |  52  |   5  |  44  |   60   |
#     | 前8 + 指数加权（前三都×1.3）       |     +0.714       |   9  |  37  |   5  |  53  |   64   |
#     | 前8 + 指数加权（w1=1.5, w2/3=1.2）|     +0.538*      |   8  |  15  |   5  |  60  |   64   |
#
#     * 0.538 是**补齐景点数据之后**的值；「前三都×1.3」的口径在补齐前是 0.714、补齐后 0.541 ——
#       两列不可直接比，看趋势即可：补数据与调权重都在把 ρ 往 0 压。
#
# 「第 1 名单独抬到 1.5、第 2/3 名降到 1.2」是用户 2026-09-17 定的：更看重
# 「有没有一张顶级名片」，少看「前三整体」。实测影响温和：前 7 名不变，
# 洛阳 9→8（反超南京）、泰安 58→53（泰山 720 拿到 1.5）、长沙 16→19；ρ 0.541 → 0.538。
# ⚠️ ρ 必须用**平均秩**处理并列：库里大量目的地挤在同一个景数档（曾 30 个都是 8 景，
# 补齐后 26 个都是 10 景），不处理并列会把这批排成一条假斜坡，把 ρ 抬到 0.95
# （我第一版就是这么算错的）。`scripts/check_rank.py` 里已按平均秩实现。
# ⚠️ RANK_DECAY 在 **0.85~0.95 之间对前 20 的名单没有任何影响**（只差南京/杭州互换一次），
# 所以刻意不暴露成配置项 —— 本仓库已有 6 个零引用的死配置，不再增加。
RANK_TOP = 8
RANK_DECAY = 0.9
RANK_FIRST_BOOST = 1.5
RANK_TOP3_BOOST = 1.2


def rank_score(heats: Iterable[int]) -> int:
    """单个目的地的排序分。纯函数，`recompute_rank_scores()` 与诊断脚本共用。"""
    total = 0.0
    for i, heat in enumerate(sorted(heats, reverse=True)[:RANK_TOP]):
        weight = RANK_DECAY ** i
        if i == 0:
            weight *= RANK_FIRST_BOOST    # 第 1 名 = 这座城市的名片，权重最高
        elif i < 3:
            weight *= RANK_TOP3_BOOST     # 第 2、3 名
        total += heat * weight
    return round(total)


def recompute_rank_scores(db: Session) -> int:
    """重算所有目的地的 `City.rank_score` 并落库，返回被改动的条数。

    **什么时候必须调**：① seed 写完景点之后（`seed.main` 已自动调）
    ② 手工改过 `trip_attraction.heat` / 增删过景点之后。
    漏调**不会报错**，但首页顺序会和实际数据对不上 —— 所以 `scripts/check_rank.py`
    专门做了「库里存的值 vs 现算的值」一致性校验，把它变成可检测的问题。
    """
    by_city: dict[int, list[int]] = {}
    for city_id, heat in db.execute(select(Attraction.city_id, Attraction.heat)).all():
        by_city.setdefault(int(city_id), []).append(int(heat))

    changed = 0
    for city in db.scalars(select(City)).all():
        score = rank_score(by_city.get(city.id, []))
        if city.rank_score != score:
            city.rank_score = score
            changed += 1
    db.commit()
    clear_cities_cache()
    return changed


def list_cities(db: Session) -> list[CityOut]:
    """首页目的地宫格。

    一条 SQL：`LEFT JOIN` 数出每个目的地的景点数，再按**预存的** `City.rank_score` 排序
    （排序分由 `recompute_rank_scores()` 维护，seed 会自动重算；口径见上面 RANK_* 的注释）。

    ⚠️ 排序分**预存**而不是每次现算，不是为了省时间 —— 现算也就一条窗口函数查询，
    耗时几乎全在那个网络往返上（库在远端，实测单条 41ms）。预存是为了
    **能反复调口径、并且能人工看/人工改**。代价是「改了景点忘记重算」会静默不一致，
    由 `scripts/check_rank.py` 的一致性校验兜着。

    并列时回落 `City.heat`，最后 `City.id` 兜底（末位 tie-break 必须显式写，
    否则同分城市的顺序不稳定）。结果进进程内缓存（`settings.cities_cache_ttl`）。
    """
    cached = _CITIES_CACHE.get(_CITIES_CACHE_KEY)
    if cached and time.monotonic() - cached[0] < settings.cities_cache_ttl:
        return cached[1]

    rows = db.execute(
        select(City, func.count(Attraction.id))
        .outerjoin(Attraction, Attraction.city_id == City.id)
        .group_by(City.id)
        .order_by(City.rank_score.desc(), City.heat.desc(), City.id.asc())
    ).all()

    out: list[CityOut] = []
    for city, count in rows:
        item = CityOut.model_validate(city)
        item.attraction_count = int(count or 0)
        out.append(item)

    _CITIES_CACHE[_CITIES_CACHE_KEY] = (time.monotonic(), out)
    return out


def list_attractions(db: Session, city_key: str) -> list[AttractionOut]:
    city = resolve_city(db, city_key)
    rows = db.scalars(
        select(Attraction)
        .where(Attraction.city_id == city.id)
        .order_by(Attraction.heat.desc(), Attraction.visit_minutes.desc())
    ).all()
    # 一条 group by 拿全部子景点数，避免 N+1；>0 时前端显示「详情」入口
    counts: dict[int, int] = {}
    if rows:
        counts = dict(
            db.execute(
                select(Spot.attraction_id, func.count())
                .where(Spot.attraction_id.in_([r.id for r in rows]))
                .group_by(Spot.attraction_id)
            ).all()
        )
    out = []
    for r in rows:
        item = AttractionOut.model_validate(r)
        item.spot_count = counts.get(r.id, 0)
        # 离目的地中心的直线距离：前端 30/45 km 分档显示「周边 / 远郊」标记
        item.distance_km = round(
            planner.haversine_m(city.center_lat, city.center_lng, r.lat, r.lng) / 1000.0, 1
        )
        out.append(item)
    return out


def list_spots(db: Session, attraction_id: int) -> list[SpotOut]:
    """某景点的内部子景点（坐标 + 攻略）。

    单独一个接口而不是塞进 `AttractionOut`：景点列表页用不到它，
    带上会让响应体积翻好几倍；只有展开某个景点详情时才取。
    """
    att = db.get(Attraction, attraction_id)
    if att is None:
        raise HTTPException(status_code=404, detail=f"景点 {attraction_id} 不存在")
    return [SpotOut.model_validate(s) for s in att.spots]


def _all_of_city(db: Session, city: City) -> list[Attraction]:
    """该城市的全部景点，按热度降序——「一键 AI」的候选池。"""
    return list(
        db.scalars(
            select(Attraction)
            .where(Attraction.city_id == city.id)
            .order_by(Attraction.heat.desc(), Attraction.visit_minutes.desc())
        ).all()
    )


def _load_selected(db: Session, city: City, ids: list[int]) -> list[Attraction]:
    """按 id 取景点，校验是否都属于该城市。"""
    rows = db.scalars(
        select(Attraction).where(Attraction.city_id == city.id, Attraction.id.in_(ids))
    ).all()
    if not rows:
        raise HTTPException(status_code=400, detail="所选景点不属于该城市，请重新勾选")
    missing = set(ids) - {a.id for a in rows}
    if missing:
        logger.warning("忽略无效景点 id：%s", sorted(missing))
    return sorted(rows, key=lambda a: -a.heat)


# ================================================================ 紧凑度预估
def preview_plan(db: Session, req: PlanRequest) -> PlanPreview:
    """纯硬编码预估：这份清单排这个天数，是偏松、刚好，还是装不下。

    选景点页实时调用（毫秒级，不碰 LLM），让用户在点「开始规划」之前就知道结果。

    **判定不再看「时间占用率」**——那个指标对天数不敏感（分子分母同比例增长），
    永远算不出「6 天只排了 4 天的量」这种事。现在直接比较两个天数：

        最少需要几天（地理下限 = 大景点/远郊各一天 + 每个片区簇一天）
        vs
        用户设的天数
    """
    city = resolve_city(db, req.city)
    attractions = _load_selected(db, city, req.attraction_ids)
    days = req.days

    # ① 容量裁剪（容量口径 = 逐天预算求和）② 地理优先分天
    kept, will_drop = planner.prune_to_capacity(city, attractions, req)
    groups = planner.assign_days(city, kept, days, req.transport, req.pace)

    # 「按片区排最舒服需要几天」——只作提示，不作判定依据（簇是可以合并的）
    days_needed = planner.estimate_days_needed(city, kept, req.transport)

    # 逐天裁剪必须**与真实生成同口径**：trim 的往返要按「当天住宿片区」算，
    # 不能退回市中心 —— region（环线）目的地的中心到景点动辄几百公里，
    # 不传 origin 会把莫高窟这种走廊两端的景点误判成「装不下」（实测踩过）。
    hotels = planner.plan_hotels(city, groups)
    visit_total = 0
    travel_total = 0
    per_day: list[dict] = []
    for idx, group in enumerate(groups, start=1):
        budget = planner.day_budget(req, idx, days)
        origin = planner._hotel_origin(city, hotels[idx - 1])
        day_kept, spilled = planner.trim_day(city, group, req, budget, origin=origin)
        for a in spilled:
            will_drop.append(
                DroppedItem(name=a.name, reason="当天时间装不下", attraction_id=a.id)
            )

        used = sum(a.visit_minutes for a in day_kept)
        day_load = planner._group_load(city, day_kept, req.transport) if day_kept else 0
        visit_total += used
        travel_total += max(0, day_load - used)

        start_s, _ = planner.day_window(req, idx, days)
        per_day.append({
            "day": idx,
            "count": len(day_kept),
            "visit_minutes": used,
            "budget_minutes": budget,
            "start_time": start_s,
            "names": [a.name for a in day_kept],
        })

    capacity = sum(planner.day_budget(req, i, days) for i in range(1, days + 1))
    load = (visit_total + travel_total) / max(1, capacity)

    # 判定：**勾选总量摊到每天，再和节奏目标比**。
    #
    # 为什么用「勾选总量」而不是「排入总量」：后者会随天数增加而增加
    # （天数多 → 容量裁剪丢得少 → 排入更多），导致加天数时日均几乎不降，
    # 反而看不出松紧变化。用勾选总量，日均会随天数单调下降，才符合直觉。
    # 也不看「时间占用率」：它分子分母同随天数增长，加天数几乎不动。
    wanted_visit = sum(a.visit_minutes for a in attractions)
    avg_visit = wanted_visit / max(1, days)
    target = planner.PACE_TARGET_MINUTES.get(req.pace, 360)
    ratio = avg_visit / max(1, target)

    if ratio >= 1.5:
        tightness = "超载"
    elif ratio >= 1.0:
        tightness = "紧凑"
    elif ratio >= 0.65:
        tightness = "适中"
    else:
        tightness = "轻松"

    # 有景点被舍弃 → 至少算「紧凑」；丢得多了直接「超载」
    drop_ratio = len(will_drop) / max(1, len(attractions))
    if drop_ratio > 0.25:
        tightness = "超载"
    elif drop_ratio > 0 and tightness in ("轻松", "适中"):
        tightness = "紧凑"

    names = [d.name for d in will_drop]
    short = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
    n_sel = len(attractions)
    pace_label = req.pace_label
    next_hint = "已经是 7 天上限，建议减少景点" if days >= 7 else f"加到 {days + 1} 天"

    if names and ratio < 1.0:
        # 有舍弃但日均并不超标 —— 原因是「路程太远 / 单点太长」，
        # 别再说「远超目标」（日均 240 分钟却喊远超 450，自相矛盾，实测踩过）。
        tail = f"，会舍弃 {len(names)} 个（{short}）"
        suggestion = (
            f"{n_sel} 个景点摊到 {days} 天，日均游览约 {avg_visit:.0f} 分钟，本不算多{tail}。"
            f"装不下的原因是路程太远或单点耗时太长，加天数也未必解决，"
            f"建议把远郊点换成近郊的，或单独安排一天。"
        )
    elif tightness == "轻松":
        suggestion = (
            f"{n_sel} 个景点摊到 {days} 天，日均游览约 {avg_visit:.0f} 分钟，"
            f"低于「{pace_label}」的 {target} 分钟目标 —— 安排偏松。"
            f"可以再多勾几个景点，或者把天数减少一些。"
        )
    elif tightness == "适中":
        suggestion = (
            f"{n_sel} 个景点摊到 {days} 天，日均游览约 {avg_visit:.0f} 分钟，"
            f"接近「{pace_label}」的 {target} 分钟目标，节奏正常。"
        )
    elif tightness == "紧凑":
        tail = f"，并且会舍弃 {len(names)} 个（{short}）" if names else ""
        suggestion = (
            f"{n_sel} 个景点摊到 {days} 天，日均游览约 {avg_visit:.0f} 分钟，"
            f"高于「{pace_label}」的 {target} 分钟目标{tail}。{next_hint}会舒服些。"
        )
    else:
        tail = f"，会舍弃 {len(names)} 个（{short}）" if names else ""
        suggestion = (
            f"{n_sel} 个景点摊到 {days} 天，日均游览约 {avg_visit:.0f} 分钟，"
            f"远超「{pace_label}」的 {target} 分钟目标{tail}。{next_hint}。"
        )

    return PlanPreview(
        city=city.name,
        days=days,
        pace=req.pace,
        transport=req.transport,
        selected_count=len(attractions),
        scheduled_count=len(kept),
        total_visit_minutes=visit_total,
        total_travel_minutes=travel_total,
        capacity_minutes=capacity,
        load_ratio=round(load, 2),
        tightness=tightness,
        days_needed=days_needed,
        per_day=per_day,
        will_drop=names,
        suggestion=suggestion,
    )


# ================================================================ 生成
def audit_plan(plan: PlanResult, req: PlanRequest) -> list[str]:
    """落库前的最后一道硬编码复核。只记日志、不改内容。

    ⚠️ 别把「同一天内出现两次」当重复：景点跨午饭会被**有意**拆成
    「上午段 + 下午继续」两个同名节点（见 `planner.materialize_day` 的跨午饭拆分），
    它们的 `attraction_id` 相同。只有**跨天**重复才是真问题。
    以前这里不区分，导致任何带长景点的方案都会稳定报「落库前复核发现 1 处问题」，
    而文档说这行「正常应为 0」—— 一个永远修不掉的假警报会让人忽略真问题。
    """
    issues: list[str] = []
    seen: dict[int, int] = {}
    for dp in plan.day_plans:
        issues.extend(f"Day {dp.day}: {x}" for x in tools.audit_day(dp, req))
        for n in dp.nodes:
            if n.type == "attraction" and n.attraction_id is not None:
                prev_day = seen.get(n.attraction_id)
                if prev_day is not None and prev_day != dp.day:
                    issues.append(
                        f"景点「{n.name}」在 Day {prev_day} 与 Day {dp.day} 重复"
                    )
                seen[n.attraction_id] = dp.day
    if issues:
        logger.warning("落库前复核发现 %d 处问题：%s", len(issues), "；".join(issues[:5]))
    return issues


def _store(
    db: Session, req: PlanRequest, result: PlanResult, source: str,
    review: PlanReview | None, trace: list[str], title: str,
) -> TripPlan:
    record = TripPlan(
        city=result.city or req.city,
        days=req.days,
        attraction_count=len(req.attraction_ids),
        source=source,
        title=title,
        request_json=req.model_dump(),
        result_json={
            "plan": result.model_dump(),
            "review": review.model_dump() if review else None,
            "trace": trace,
        },
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def generate_plan(db: Session, req: PlanRequest) -> PlanResponse:
    city = resolve_city(db, req.city)
    attractions = _load_selected(db, city, req.attraction_ids)

    # ② 硬编码基线：永远先算一份，既是兜底也是退路
    baseline = planner.plan_fallback(city, attractions, req)

    source = "fallback"
    trace: list[str] = []
    review: PlanReview | None = None
    result = baseline
    fallback_note = ""

    # ③ LLM 并行流水线
    if llm.is_available():
        try:
            result, trace = llm.run_pipeline(city, attractions, req, baseline)
            source = "llm"
        except llm.LLMError as exc:
            logger.error("LLM 流水线失败，降级到规则引擎：%s", exc)
            result, trace = baseline, []
            fallback_note = f"（DeepSeek 生成失败，已用本地规则引擎兜底：{exc}）"
    else:
        fallback_note = "（未配置 DeepSeek Key，由本地规则引擎生成）"

    if source == "fallback":
        result.summary = (result.summary + fallback_note).strip()
    else:
        # ④ LLM 终检（失败不影响主流程）
        review = llm.review_plan(result, req)
        if review and review.summary.strip():
            result.summary = review.summary.strip()[:300]

    audit_plan(result, req)

    title = f"{city.name} · {req.days} 天 {len(attractions)} 景"
    record = _store(db, req, result, source, review, trace, title)

    logger.info(
        "规划完成 #%d source=%s 轨迹=%d 终检问题=%d",
        record.id, source, len(trace), len(review.issues) if review else 0,
    )
    return _to_response(record, attractions, review=review, trace=trace)


def generate_auto_plan(db: Session, payload: AutoPlanRequest) -> PlanResponse:
    """一键 AI：只给城市 + 天数 + 节奏，服务端自动挑景点，再复用 generate_plan。

    挑景点原则与分天一致（必去优先、其次热度，累计游览时长逼近
    `天数 × 节奏目标`）。挑完转成普通 PlanRequest 后走**同一条生成链路**，
    所以「一键」与「勾选」产出的方案结构、来源标记、降级行为完全一致；
    实际选中的景点回填进 `request.attraction_ids`，前端和历史记录都能看到。
    """
    city = resolve_city(db, payload.city)
    pool = _all_of_city(db, city)
    if not pool:
        raise HTTPException(
            status_code=400, detail=f"「{city.name}」还没有景点数据，先跑一次 seed"
        )

    picked = planner.pick_attractions(
        city, pool, payload.days, payload.pace, payload.transport
    )
    preview_names = "、".join(a.name for a in picked[:5])
    logger.info(
        "一键 AI：%s %s 天 %s 节奏 → 自动挑选 %d 个景点（%s%s）",
        city.name, payload.days, payload.pace, len(picked),
        preview_names, "…" if len(picked) > 5 else "",
    )

    req = PlanRequest(
        city=city.name,
        attraction_ids=[a.id for a in picked],
        **payload.model_dump(exclude={"city"}),
    )
    return generate_plan(db, req)


# ================================================================ 倾向微调
def adjust_plan(db: Session, plan_id: int, patch: PlanRequest) -> PlanResponse:
    """按倾向微调已有方案：复用原来的分天，只重排时间轴。

    和「重新生成」的区别：
      · 微调是纯硬编码（毫秒级、不花 token）：按新的天数/时间/倾向重排、增删景点；
      · 重新生成会重新走一遍 LLM 流水线（文案也会变）。
    倾向的作用：宽松把塞不下的剔掉让每天早点收工，紧凑尽量把舍弃的加回来。
    """
    record = db.get(TripPlan, plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划 #{plan_id} 不存在")

    city = resolve_city(db, patch.city)
    old_raw, _, _ = _unwrap(record.result_json or {})
    old = PlanResult.model_validate(old_raw)
    old_req = PlanRequest.model_validate(record.request_json)

    all_map = {a.id: a for a in _load_selected(db, city, old_req.attraction_ids)}
    if not all_map:
        raise HTTPException(status_code=400, detail="原方案的景点数据已不存在，请重新规划")

    # 沿用原来的分天（景点 id 顺序）
    groups: list[list[Attraction]] = []
    for dp in old.day_plans[: patch.days]:
        ids = [
            n.attraction_id for n in dp.nodes
            if n.type == "attraction" and n.attraction_id in all_map
        ]
        groups.append([all_map[i] for i in ids])
    while len(groups) < patch.days:
        groups.append([])

    # 紧凑：把原先舍弃的加回来，塞进还有余量的天
    restored: list[str] = []
    if patch.pace == "packed":
        for d in old.dropped:
            aid = d.attraction_id
            if aid not in all_map or any(aid in [x.id for x in g] for g in groups):
                continue
            item = all_map[aid]
            best_idx, best_slack = None, 0
            for idx in range(len(groups)):
                trial = [*groups[idx], item]
                kept, spilled = planner.trim_day(
                    city, trial, patch, planner.day_budget(patch, idx + 1, patch.days)
                )
                if len(kept) == len(trial):
                    slack = planner.day_budget(patch, idx + 1, patch.days) - sum(
                        a.visit_minutes for a in kept
                    )
                    if slack > best_slack:
                        best_idx, best_slack = idx, slack
            if best_idx is not None:
                groups[best_idx].append(item)
                restored.append(item.name)

    # 沿用原有的主题与景点建议（微调不重写文案）
    old_advice = {
        n.attraction_id: n.advice
        for dp in old.day_plans for n in dp.nodes
        if n.type == "attraction" and n.attraction_id and n.advice
    }
    old_meal = {}
    for dp in old.day_plans:
        for n in dp.nodes:
            if n.type == "meal" and "本地特色" not in n.name:
                old_meal.setdefault("lunch" if n.time < "15:00" else "dinner", n.name)

    hotels = planner.plan_hotels(city, groups)
    day_plans: list[DayPlan] = []
    dropped: list[DroppedItem] = []
    for idx in range(1, patch.days + 1):
        items = groups[idx - 1]
        if not items:
            day_plans.append(planner.empty_day(city, patch, idx, hotels[idx - 1]))
            continue
        origin = planner._hotel_origin(city, hotels[idx - 1])
        kept, spilled = planner.trim_day(
            city, items, patch, planner.day_budget(patch, idx, patch.days), origin=origin
        )
        for a in spilled:
            dropped.append(
                DroppedItem(
                    name=a.name,
                    reason=f"按「{patch.pace_label}」倾向重排后 Day {idx} 装不下，建议下次单独安排。",
                    attraction_id=a.id,
                )
            )
        theme = next(
            (
                dp.theme for dp in old.day_plans
                if {n.attraction_id for n in dp.nodes if n.type == "attraction"}
                & {a.id for a in kept}
            ),
            "",
        )
        day_plans.append(
            planner.materialize_day(
                city, idx, kept, patch,
                depart_from=hotels[idx - 2] if idx >= 2 else None,
                total_days=patch.days,
                hotel=hotels[idx - 1],
                theme=theme,
                advice=old_advice,
            )
        )

    if restored:
        note = f"（按「紧凑」倾向把 {'、'.join(restored[:3])} 重新排了进来）"
    elif patch.pace == "relaxed":
        note = "（按「宽松」倾向精简了每天的量，收工更早）"
    else:
        note = ""
    summary = (old.summary[:280].rstrip("。") + "。" + note)[:300] if note else old.summary[:300]

    result = PlanResult(
        city=city.name,
        days=patch.days,
        summary=summary,
        day_plans=day_plans,
        must_visit_reasons=old.must_visit_reasons,
        dropped=dropped,
    )
    audit_plan(result, patch)

    trace = [f"adjust mode=tweak pace={patch.pace} days={patch.days} restored={len(restored)}"]
    title = f"{city.name} · {patch.days} 天 {len(patch.attraction_ids)} 景"
    new_record = _store(db, patch, result, "tweak", None, trace, title)

    logger.info(
        "微调完成 #%d → #%d pace=%s 恢复=%d 舍弃=%d",
        plan_id, new_record.id, patch.pace, len(restored), len(dropped),
    )
    return _to_response(new_record, list(all_map.values()), trace=trace)


# ================================================================ 读取
def _unwrap(raw: dict) -> tuple[dict, dict | None, list[str]]:
    """兼容两种存储形态：新的 {"plan","review","trace"} 包装，与老的裸 PlanResult。"""
    if isinstance(raw, dict) and "plan" in raw and "day_plans" not in raw:
        return raw["plan"], raw.get("review"), list(raw.get("trace") or [])
    return raw, None, []


def _to_response(
    record: TripPlan,
    attractions: list[Attraction],
    review: PlanReview | None = None,
    trace: list[str] | None = None,
) -> PlanResponse:
    plan_raw, stored_review, stored_trace = _unwrap(record.result_json or {})
    created = record.created_at
    return PlanResponse(
        plan_id=record.id,
        created_at=created.strftime("%Y-%m-%d %H:%M:%S") if isinstance(created, datetime) else str(created),
        source=record.source,
        title=record.title,
        request=PlanRequest.model_validate(record.request_json),
        result=PlanResult.model_validate(plan_raw),
        attractions=[AttractionOut.model_validate(a) for a in attractions],
        review=review if review is not None else (
            PlanReview.model_validate(stored_review) if stored_review else None
        ),
        trace=trace if trace is not None else stored_trace,
    )


def get_plan(db: Session, plan_id: int) -> PlanResponse:
    record = db.get(TripPlan, plan_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"规划 #{plan_id} 不存在")

    ids = list((record.request_json or {}).get("attraction_ids") or [])
    attractions = (
        db.scalars(select(Attraction).where(Attraction.id.in_(ids))).all() if ids else []
    )
    return _to_response(record, list(attractions))


def list_plans(db: Session, limit: int = 20) -> list[PlanBrief]:
    records = db.scalars(
        select(TripPlan).order_by(TripPlan.id.desc()).limit(max(1, min(limit, 100)))
    ).all()
    out: list[PlanBrief] = []
    for r in records:
        created = r.created_at
        out.append(
            PlanBrief(
                plan_id=r.id,
                city=r.city,
                days=r.days,
                title=r.title,
                source=r.source,
                created_at=created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else str(created),
                attraction_count=r.attraction_count,
            )
        )
    return out
