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
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.trip import llm, planner, tools
from app.trip.models import Attraction, City, TripPlan
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
)

logger = logging.getLogger("triptide.service")


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


def list_cities(db: Session) -> list[CityOut]:
    cities = db.scalars(select(City).order_by(City.heat.desc())).all()
    return [
        CityOut.model_validate({**c.__dict__, "attraction_count": len(c.attractions)})
        for c in cities
    ]


def list_attractions(db: Session, city_key: str) -> list[AttractionOut]:
    city = resolve_city(db, city_key)
    rows = db.scalars(
        select(Attraction)
        .where(Attraction.city_id == city.id)
        .order_by(Attraction.heat.desc(), Attraction.visit_minutes.desc())
    ).all()
    return [AttractionOut.model_validate(r) for r in rows]


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
    """纯硬编码预估：这些景点按这个天数排会有多紧、哪些会被舍弃。

    选景点页实时调用（毫秒级，不碰 LLM），让用户在点「开始规划」之前就知道结果。
    """
    city = resolve_city(db, req.city)
    attractions = _load_selected(db, city, req.attraction_ids)
    days = req.days

    per_day_budget = min(planner.day_budget(req, i, days) for i in range(1, days + 1))
    capacity = per_day_budget * days
    kept, will_drop = planner.prune_to_capacity(city, attractions, req, per_day_budget)
    groups = planner.assign_days(city, kept, days, req.transport, req.pace)

    visit_total = 0
    travel_total = 0
    per_day: list[dict] = []
    for idx, group in enumerate(groups, start=1):
        budget = planner.day_budget(req, idx, days)
        day_kept, spilled = planner.trim_day(city, group, req, budget)
        for a in spilled:
            will_drop.append(
                DroppedItem(name=a.name, reason="当天时间装不下", attraction_id=a.id)
            )

        used = sum(a.visit_minutes for a in day_kept)
        visit_total += used
        lat, lng = city.center_lat, city.center_lng
        for a in day_kept:
            travel_total += planner.leg(city, lat, lng, a.lat, a.lng, req.transport).minutes
            lat, lng = a.lat, a.lng
        travel_total += planner.leg(
            city, lat, lng, city.center_lat, city.center_lng, req.transport
        ).minutes

        start_s, _ = planner.day_window(req, idx, days)
        per_day.append({
            "day": idx,
            "count": len(day_kept),
            "visit_minutes": used,
            "budget_minutes": budget,
            "start_time": start_s,
            "names": [a.name for a in day_kept],
        })

    # 紧凑度不能只看负载率——被舍弃的景点也是「装不下」的证据。
    # 10 个景点排 1 天、塞进 4 个丢掉 6 个，负载率照样好看，但那显然不是「刚好」。
    load = (visit_total + travel_total) / max(1, capacity)
    drop_ratio = len(will_drop) / max(1, len(attractions))

    if drop_ratio > 0.25 or load > 1.05:
        tightness = "超载"
    elif drop_ratio > 0.08 or load > 0.88:
        tightness = "紧凑"
    elif load > 0.6:
        tightness = "适中"
    else:
        tightness = "轻松"

    names = [d.name for d in will_drop]
    short = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
    per_day_visit = visit_total // max(1, days)

    if tightness == "超载":
        suggestion = (
            f"{len(attractions)} 个景点排 {days} 天装不下，约 {len(names)} 个会被舍弃（{short}）。"
            f"想都去建议加到 {min(7, days + 1)} 天，或把远郊景点去掉。"
        )
    elif tightness == "紧凑":
        suggestion = (
            f"{days} 天排下来会舍弃 {len(names)} 个（{short}），其余能排进去，节奏偏紧。"
            if names
            else f"{days} 天刚好排得下，节奏偏紧，每天约 {per_day_visit} 分钟在景点里。"
        )
    elif tightness == "轻松":
        suggestion = "安排偏松还有余量，可以再勾几个景点，或者减一天。"
    else:
        suggestion = (
            f"{days} 天排下来会舍弃 {len(names)} 个（{short}），整体节奏适中。"
            if names
            else f"{days} 天节奏刚好，每天约 {per_day_visit} 分钟在景点里。"
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
        per_day=per_day,
        will_drop=names,
        suggestion=suggestion,
    )


# ================================================================ 生成
def audit_plan(plan: PlanResult, req: PlanRequest) -> list[str]:
    """落库前的最后一道硬编码复核。只记日志、不改内容。"""
    issues: list[str] = []
    seen: dict[int, int] = {}
    for dp in plan.day_plans:
        issues.extend(f"Day {dp.day}: {x}" for x in tools.audit_day(dp, req))
        for n in dp.nodes:
            if n.type == "attraction" and n.attraction_id is not None:
                if n.attraction_id in seen:
                    issues.append(
                        f"景点「{n.name}」在 Day {seen[n.attraction_id]} 与 Day {dp.day} 重复"
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
        kept, spilled = planner.trim_day(
            city, items, patch, planner.day_budget(patch, idx, patch.days)
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
