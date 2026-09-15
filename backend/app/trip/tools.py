"""给 LLM 的行程规划工具集（function calling）。

架构分工（这是整个项目的核心设计）：

    LLM 负责编排与创作              硬编码负责算术与校验
    ─────────────────────           ─────────────────────
    · 决定去哪几个景点、怎么分天      · 算时间（到达 = 上一站离开 + 路程）
    · 起主题名、写具体建议            · 插饭点、算停留与路程
    · 挑住宿片区、讲取舍理由          · 判断装不装得下、按容量裁剪
    · 写必去理由、写舍弃原因          · 校验时间自洽、景点不重复
    · 找真实餐厅（走高德 POI）        · 组装最终时间轴

为什么这么切：让模型直接吐时间轴，它必然算错——实测出现过「12:10 到景点、12:30 吃午饭」
「晚餐合并成下午茶」「同一个景点排进两天」。这些不是 prompt 能彻底治好的，
只能靠「不给它算的机会」。反过来，写建议、起名字、讲理由是模型真正擅长且硬编码做不好的。

调用流程（系统提示词里会引导）：
    get_city_info → list_attractions → plan_days（拿硬编码骨架）
    → 按需 route_time / suggest_hotel
    → note_attraction ×N 写建议
    → build_day ×N 物化每一天
    → check_day 抽查
    → finalize_plan 收尾
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.trip import planner
from app.trip.models import Attraction, City
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

logger = logging.getLogger("triptide.tools")

@dataclass
class PlanSession:
    """一次规划请求的服务端状态。

    工具通过「改这个对象」来推进方案，而不是把大段 JSON 塞回模型上下文——
    这样多轮编排的 token 开销是常数级，不会随天数增长。
    """

    city: City
    req: PlanRequest
    attractions: dict[int, Attraction]
    per_day_budget: int
    baseline: PlanResult                       # 硬编码基线，永远先算好
    built: dict[int, DayPlan] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)
    dropped: list[DroppedItem] = field(default_factory=list)
    must_reasons: list[MustVisitReason] = field(default_factory=list)
    summary: str = ""
    result: PlanResult | None = None
    finalized: bool = False
    warnings: list[str] = field(default_factory=list)

    # ---------------- 辅助 ----------------
    def name_of(self, aid: int) -> str:
        a = self.attractions.get(aid)
        return a.name if a else f"id={aid}"

    def assigned_ids(self) -> set[int]:
        out: set[int] = set()
        for dp in self.built.values():
            for n in dp.nodes:
                if n.type == "attraction" and n.attraction_id is not None:
                    out.add(n.attraction_id)
        return out

    def dropped_ids(self) -> set[int]:
        return {d.attraction_id for d in self.dropped if d.attraction_id is not None}


# ================================================================ 工具实现
def _tool_get_city_info(s: PlanSession, args: dict) -> dict:
    c = s.city
    return {
        "city": c.name,
        "center": {"lat": c.center_lat, "lng": c.center_lng},
        "hotel_areas": [
            {"area": h.get("area"), "pros": h.get("pros", ""), "cons": h.get("cons", "")}
            for h in (c.hotel_areas or [])
        ],
        "selected_attractions": len(s.attractions),
        "trip": {
            "days": s.req.days,
            "start_time": s.req.start_time,
            "return_time": s.req.return_time,
            "transport": s.req.transport,
            "per_day_budget_minutes": s.per_day_budget,
            "note": "per_day_budget 已扣除午晚餐时间，是每天可用于「游览 + 赶路」的总分钟数",
        },
    }


def _tool_list_attractions(s: PlanSession, args: dict) -> dict:
    district = (args.get("district") or "").strip()
    min_heat = int(args.get("min_heat") or 0)
    limit = int(args.get("limit") or 40)

    rows = [
        a
        for a in s.attractions.values()
        if (not district or a.district == district) and a.heat >= min_heat
    ]
    rows.sort(key=lambda a: (-a.heat, -a.visit_minutes))
    return {
        "count": len(rows),
        "attractions": [
            {
                "id": a.id,
                "name": a.name,
                "district": a.district,
                "heat": a.heat,
                "visit_minutes": a.visit_minutes,
                "must_visit": a.must_visit,
                "tags": list(a.tags or []),
                "intro": a.intro,
                # 时段硬约束：有值的景点必须按提示排（build_day 会强制校验）
                "time_constraint": (
                    (r := planner.time_rule(a)) and (
                        f"必须 {r.latest_start} 前进入（{r.label}）" if r.latest_start
                        else f"必须 {r.earliest_start} 之后再去（{r.label}）"
                    )
                ) or "",
            }
            for a in rows[:limit]
        ],
        "note": "time_constraint 非空的景点有硬性时段要求，build_day 会校验，排错了会被打回。",
    }


def _tool_route_time(s: PlanSession, args: dict) -> dict:
    a = s.attractions.get(int(args.get("from_attraction_id") or 0))
    b = s.attractions.get(int(args.get("to_attraction_id") or 0))
    if not a or not b:
        return {"ok": False, "error": "景点 id 不在本次勾选范围内"}
    transport = args.get("transport") or s.req.transport
    lg = planner.leg(s.city, a.lat, a.lng, b.lat, b.lng, transport)
    return {
        "from": a.name,
        "to": b.name,
        "distance_km": lg.km,
        "minutes": lg.minutes,
        "mode": lg.label,
        "transport": transport,
        "note": "硬编码按距离分档选择交通方式，不打地图接口；时间含候车/取车开销",
    }


def _tool_plan_days(s: PlanSession, args: dict) -> dict:
    """硬编码给出地理顺路的分天骨架 + 容量评估。"""
    raw_ids = [int(i) for i in (args.get("attraction_ids") or [])]
    days = int(args.get("days") or s.req.days)
    transport = args.get("transport") or s.req.transport

    picked = [s.attractions[i] for i in raw_ids if i in s.attractions]
    if not picked:
        return {"ok": False, "error": "attraction_ids 里没有一个有效 id"}

    req = s.req.model_copy(update={"days": days, "transport": transport})
    budget = max(
        240,
        (planner.parse_hhmm(req.return_time) - planner.parse_hhmm(req.start_time))
        - (planner.MEAL_LUNCH_MINUTES + planner.MEAL_DINNER_MINUTES),
    )

    kept, over = planner.prune_to_capacity(s.city, picked, req)
    groups = planner.assign_days(s.city, kept, days, transport, req.pace)

    out_days = []
    for idx, items in enumerate(groups, start=1):
        kept_day, spilled = planner.trim_day(s.city, items, req, budget)
        visit = sum(a.visit_minutes for a in kept_day)
        out_days.append(
            {
                "day": idx,
                "attraction_ids": [a.id for a in kept_day],
                "attraction_names": [a.name for a in kept_day],
                "districts": sorted({a.district for a in kept_day if a.district}),
                "visit_minutes": visit,
                "spilled_ids": [a.id for a in spilled],
            }
        )

    return {
        "ok": True,
        "days": out_days,
        "over_capacity_ids": [a.id for a in over],
        "over_capacity_names": [a.name for a in over],
        "hint": (
            "这是硬编码算出的地理顺路骨架（已按容量裁剪、已按游览时长均分）。"
            "**顺序已经过优化，除非有明确理由（某景点只有夜间/清晨开放），否则不要打乱它**——"
            "实测把高热度景点（如熊猫基地，上午才活跃）挪到下午会显著降低体验。"
            "你可以调整景点归属与顺序，但调整后务必用 build_day 物化——"
            "build_day 会告诉你那天几点结束、是否超时。"
            "over_capacity 里的景点是硬编码判断装不下的，建议放进 dropped。"
        ),
    }


def _tool_suggest_hotel(s: PlanSession, args: dict) -> dict:
    ids = [int(i) for i in (args.get("attraction_ids") or [])]
    picked = [s.attractions[i] for i in ids if i in s.attractions]
    if not picked:
        return {"ok": False, "error": "attraction_ids 里没有一个有效 id"}
    h = planner.pick_hotel(s.city, picked)
    return {
        "area": h.area,
        "reason": h.reason,
        "alternatives": [{"area": x.area, "tradeoff": x.tradeoff} for x in h.alternatives],
        "candidate_areas": [x.get("area") for x in (s.city.hotel_areas or [])],
    }


def _tool_note_attraction(s: PlanSession, args: dict) -> dict:
    """给景点写一句具体建议。这是 LLM 的主要创作出口。"""
    aid = int(args.get("attraction_id") or 0)
    if aid not in s.attractions:
        return {"ok": False, "error": "景点 id 不在本次勾选范围内"}
    text = (args.get("advice") or "").strip()
    if not text:
        return {"ok": False, "error": "advice 不能为空"}
    s.notes[aid] = text[:200]
    return {"ok": True, "attraction": s.name_of(aid), "noted": len(s.notes)}


def _tool_build_day(s: PlanSession, args: dict) -> dict:
    """物化某一天：时间、饭点、路程全部由硬编码算出来。"""
    day = int(args.get("day") or 0)
    if not (1 <= day <= s.req.days):
        return {"ok": False, "error": f"day 必须在 1~{s.req.days} 之间"}

    raw_ids = [int(i) for i in (args.get("attraction_ids") or [])]
    invalid = [i for i in raw_ids if i not in s.attractions]
    picked: list[Attraction] = []
    seen: set[int] = set()
    for i in raw_ids:
        if i in s.attractions and i not in seen:
            seen.add(i)
            picked.append(s.attractions[i])

    # 不能和别的天重复排同一个景点
    used_elsewhere = {
        aid
        for d, dp in s.built.items()
        if d != day
        for n in dp.nodes
        if n.type == "attraction" and (aid := n.attraction_id) is not None
    }
    dups = [a for a in picked if a.id in used_elsewhere]
    picked = [a for a in picked if a.id not in used_elsewhere]
    if not picked:
        return {
            "ok": False,
            "error": "这一天没有任何有效景点"
            + (f"（{len(dups)} 个已在其它天排过：{[a.name for a in dups]}）" if dups else ""),
        }

    kept, spilled = planner.trim_day(s.city, picked, s.req, s.per_day_budget)

    # 住宿片区：LLM 指定了就用它的，没指定按几何中心选
    hotel: HotelAdvice | None = None
    want_area = (args.get("hotel_area") or "").strip()
    if want_area:
        for area in s.city.hotel_areas or []:
            if area.get("area") == want_area:
                hotel = HotelAdvice(
                    area=want_area,
                    reason=str(area.get("pros") or "按当天动线选择的住宿片区。"),
                    alternatives=[
                        HotelAlternative(
                            area=str(x["area"]),
                            tradeoff=str(x.get("cons") or "位置略偏，但环境/价格可能更优。"),
                        )
                        for x in (s.city.hotel_areas or [])
                        if x.get("area") != want_area
                    ][:2],
                )
                break

    dp = planner.materialize_day(
        s.city,
        day,
        kept,
        s.req,
        total_days=s.req.days,
        theme=(args.get("theme") or "").strip(),
        advice=s.notes,
        lunch_area=(args.get("lunch_area") or "").strip(),
        lunch_hint=(args.get("lunch_hint") or "").strip(),
        dinner_area=(args.get("dinner_area") or "").strip(),
        dinner_hint=(args.get("dinner_hint") or "").strip(),
        hotel=hotel,
    )
    s.built[day] = dp

    end_time = dp.nodes[-1].time if dp.nodes else s.req.start_time
    warnings: list[str] = []
    if invalid:
        warnings.append(f"忽略了不存在的 id：{invalid}")
    if dups:
        warnings.append(f"忽略了已在其它天排过的景点：{[a.name for a in dups]}")
    if spilled:
        warnings.append(
            f"当天装不下，已剔除：{[a.name for a in spilled]}"
            f"（可用 id {[a.id for a in spilled]}，请另找一天或放进 dropped）"
        )
    if want_area and hotel is None:
        warnings.append(f"住宿片区「{want_area}」不在候选里，已改为按几何中心自动选择")

    # 收尾明显超时：只要「少去一个景点就能收住」，就判失败逼模型重排；
    # 如果只去了一个景点还是超时（比如都江堰单点远郊日），那是物理限制，只给警告。
    overrun = planner.parse_hhmm(end_time) - planner.parse_hhmm(s.req.return_time)
    if overrun > planner.OVERRUN_TOLERANCE and len(kept) > 1:
        victim = kept[-1]
        return {
            "ok": False,
            "error": (
                f"Day {day} 收尾 {end_time}，比目标返回时间 {s.req.return_time} 晚 {overrun} 分钟，"
                f"不合格。请减少景点后重调 build_day —— 建议先去掉「{victim.name}」"
                f"（id={victim.id}），它可以挪到别的天或放进 dropped。"
            ),
            "day": day,
            "end": end_time,
            "overrun_minutes": overrun,
            "attractions": [a.name for a in kept],
        }

    # 时段硬约束（硬知识，写死在 planner.time_rule 里）
    violations = planner.rule_violations(dp, s.attractions)
    if violations:
        return {
            "ok": False,
            "error": (
                f"Day {day} 违反时段规则：{'；'.join(violations)}。"
                "请调整这些景点的顺序或换到别的天，然后重新调用 build_day。"
                "提示：早场类景点应排在当天第一位，夜景类应排在当天最后。"
            ),
            "day": day,
            "end": end_time,
            "attractions": [a.name for a in kept],
        }

    return {
        "ok": True,
        "day": day,
        "theme": dp.theme,
        "pace": dp.pace,
        "start": dp.nodes[0].time if dp.nodes else "",
        "end": end_time,
        "overrun_minutes": max(0, overrun),
        "attractions": [a.name for a in kept],
        "hotel_area": dp.hotel.area if dp.hotel else "",
        "timeline": _timeline_text(dp),
        "warnings": warnings,
    }


def _tool_check_day(s: PlanSession, args: dict) -> dict:
    day = int(args.get("day") or 0)
    dp = s.built.get(day)
    if dp is None:
        return {"ok": False, "error": f"Day {day} 还没用 build_day 物化过"}
    issues = audit_day(dp, s.req)
    return {"day": day, "ok": not issues, "issues": issues, "end": dp.nodes[-1].time}


def _tool_drop_attraction(s: PlanSession, args: dict) -> dict:
    aid = int(args.get("attraction_id") or 0)
    if aid not in s.attractions:
        return {"ok": False, "error": "景点 id 不在本次勾选范围内"}
    if aid in s.assigned_ids():
        return {"ok": False, "error": f"{s.name_of(aid)} 已经排进时间轴了，不能同时舍弃"}
    reason = (args.get("reason") or "").strip() or "与整体动线冲突，时间预算装不下。"
    s.dropped = [d for d in s.dropped if d.attraction_id != aid]
    s.dropped.append(DroppedItem(name=s.name_of(aid), reason=reason[:200], attraction_id=aid))
    return {"ok": True, "dropped_count": len(s.dropped)}


def _tool_finalize_plan(s: PlanSession, args: dict) -> dict:
    """收尾：补齐没物化的天与漏掉的景点，组装成最终时间轴。"""
    if (args.get("summary") or "").strip():
        s.summary = args["summary"].strip()[:300]

    reasons = args.get("must_visit_reasons") or []
    if reasons:
        s.must_reasons = [
            MustVisitReason(
                name=str(r.get("name") or s.name_of(int(r.get("attraction_id") or 0)))[:64],
                reason=str(r.get("reason") or "")[:200],
                attraction_id=int(r["attraction_id"]) if r.get("attraction_id") else None,
            )
            for r in reasons
            if isinstance(r, dict)
        ]

    notes: list[str] = []
    # ① 没物化的天：用硬编码基线补上
    for day in range(1, s.req.days + 1):
        if day in s.built:
            continue
        base = next((d for d in s.baseline.day_plans if d.day == day), None)
        if base and base.nodes:
            s.built[day] = base
            notes.append(f"Day {day} 你没物化，已用硬编码基线补上")
        else:
            s.built[day] = planner.empty_day(s.city, s.req, day, planner.pick_hotel(s.city, []))
            notes.append(f"Day {day} 你没物化且无剩余景点，已补为空白天")

    # ② 既没排入也没舍弃的景点：尽量塞进还有余量的天，塞不下才舍弃
    left = [
        aid
        for aid in s.attractions
        if aid not in s.assigned_ids() and aid not in s.dropped_ids()
    ]
    for aid in left:
        item = s.attractions[aid]
        target = _best_day_for(s, item)
        if target is None:
            s.dropped.append(
                DroppedItem(
                    name=item.name,
                    reason=f"{s.req.days} 天的预算已排满，装不下这个景点，建议下次单独安排。",
                    attraction_id=aid,
                )
            )
            continue
        dp = s.built[target]
        ids = [n.attraction_id for n in dp.nodes if n.type == "attraction" and n.attraction_id]
        s.built[target] = planner.materialize_day(
            s.city, target, [s.attractions[i] for i in [*ids, aid] if i in s.attractions],
            s.req, total_days=s.req.days, theme=dp.theme, advice=s.notes,
            hotel=dp.hotel,
        )
        notes.append(f"「{item.name}」你没安排，已自动补进 Day {target}")

    # ③ 舍掉已经不存在的 id 的 dropped 记录，去重
    seen_drop: set[int] = set()
    clean_dropped: list[DroppedItem] = []
    for d in s.dropped:
        if d.attraction_id is not None:
            if d.attraction_id in s.assigned_ids() or d.attraction_id in seen_drop:
                continue
            seen_drop.add(d.attraction_id)
        clean_dropped.append(d)

    day_plans = [s.built[d] for d in sorted(s.built)]

    # ④ 复核：时间自洽、饭点、重复景点
    audit: list[str] = []
    for dp in day_plans:
        audit.extend(f"Day {dp.day}: {x}" for x in audit_day(dp, s.req))
    dup = _find_duplicate(day_plans)
    if dup:
        audit.append(dup)

    if not s.must_reasons:
        s.must_reasons = [
            MustVisitReason(
                name=a.name,
                reason=f"热度 {a.heat}｜{a.intro or '当地标志性去处'}",
                attraction_id=a.id,
            )
            for a in sorted(s.attractions.values(), key=lambda x: -x.heat)[:3]
        ]

    if not s.summary:
        s.summary = s.baseline.summary

    s.result = PlanResult(
        city=s.city.name,
        days=s.req.days,
        summary=s.summary,
        day_plans=day_plans,
        must_visit_reasons=s.must_reasons,
        dropped=clean_dropped,
    )
    s.finalized = True
    s.warnings.extend(notes)
    s.warnings.extend(audit)

    return {
        "ok": True,
        "finalized": True,
        "days": len(day_plans),
        "attractions_scheduled": len(s.assigned_ids()),
        "dropped": [d.name for d in clean_dropped],
        "auto_filled": notes,
        "audit_issues": audit,
        "note": "方案已提交。如果 audit_issues 非空，说明硬编码复核发现了问题，请在最终回复里说明。",
    }


# ================================================================ 内部工具
def _timeline_text(dp: DayPlan) -> str:
    """紧凑时间轴，给模型读（判断动线是否合理）。"""
    parts = []
    for n in dp.nodes:
        if n.type == "attraction":
            parts.append(f"{n.time} {n.name}(停留{n.stay_minutes}分)")
        elif n.type == "meal":
            parts.append(f"{n.time} {n.name}")
        elif n.type == "hotel":
            parts.append(f"{n.time} 回酒店")
        elif n.type == "transit":
            parts.append(f"{n.time} 自由活动")
        else:
            parts.append(f"{n.time} 出发")
    return " → ".join(parts)


def audit_day(dp: DayPlan, req: PlanRequest) -> list[str]:
    """硬编码复核：时间自洽 + 饭点数量与时段 + 出发时间。"""
    issues: list[str] = []
    if not dp.nodes:
        return ["当天没有任何节点"]

    meals = [n for n in dp.nodes if n.type == "meal"]
    lunch = [n for n in meals if n.time < "15:00"]
    dinner = [n for n in meals if n.time >= "15:00"]
    if len(lunch) != 1:
        issues.append(f"午餐节点 {len(lunch)} 个（应为 1 个）")
    if len(dinner) != 1:
        issues.append(f"晚餐节点 {len(dinner)} 个（应为 1 个）")
    start_s, end_s = planner.day_window(req, dp.day, req.days)
    if dp.nodes[0].time != start_s:
        issues.append(f"出发时间 {dp.nodes[0].time} ≠ 设定的 {start_s}")

    for prev, cur in zip(dp.nodes, dp.nodes[1:]):
        expect = planner.parse_hhmm(prev.time) + prev.stay_minutes + cur.travel_minutes
        if planner.fmt_hhmm(expect) != cur.time:
            issues.append(
                f"时间不自洽：{prev.time}+{prev.stay_minutes}+{cur.travel_minutes}"
                f"={planner.fmt_hhmm(expect)}，实际 {cur.time}"
            )

    target_end = planner.parse_hhmm(end_s)
    end = planner.parse_hhmm(dp.nodes[-1].time)
    if end - target_end > 60:
        issues.append(f"收尾 {dp.nodes[-1].time} 比目标返回时间晚 {end - target_end} 分钟")
    return issues


def _find_duplicate(day_plans: list[DayPlan]) -> str:
    seen: dict[int, int] = {}
    for dp in day_plans:
        for n in dp.nodes:
            if n.type != "attraction" or n.attraction_id is None:
                continue
            if n.attraction_id in seen:
                return f"景点「{n.name}」在 Day {seen[n.attraction_id]} 与 Day {dp.day} 重复出现"
            seen[n.attraction_id] = dp.day
    return ""


def _best_day_for(s: PlanSession, item: Attraction) -> int | None:
    """找出还有余量、且离该景点最近的那一天。"""
    best: tuple[float, int] | None = None
    for day, dp in s.built.items():
        ids = [n.attraction_id for n in dp.nodes if n.type == "attraction" and n.attraction_id]
        items = [s.attractions[i] for i in ids if i in s.attractions]
        if not items:
            continue
        trial = [*items, item]
        kept, spilled = planner.trim_day(s.city, trial, s.req, s.per_day_budget)
        if item.id in {a.id for a in spilled} or len(kept) != len(trial):
            continue
        anchor = items[-1]
        dist = planner.haversine_m(anchor.lat, anchor.lng, item.lat, item.lng)
        if best is None or dist < best[0]:
            best = (dist, day)
    return best[1] if best else None


# ================================================================ 分发
_DISPATCH: dict[str, Any] = {
    "get_city_info": _tool_get_city_info,
    "list_attractions": _tool_list_attractions,
    "route_time": _tool_route_time,
    "plan_days": _tool_plan_days,
    "suggest_hotel": _tool_suggest_hotel,
    "note_attraction": _tool_note_attraction,
    "build_day": _tool_build_day,
    "check_day": _tool_check_day,
    "drop_attraction": _tool_drop_attraction,
    "finalize_plan": _tool_finalize_plan,
}


def dispatch(session: PlanSession, name: str, args: dict) -> dict:
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "error": f"未知工具 {name}，可用：{', '.join(_DISPATCH)}"}
    return fn(session, args or {})


def is_finalized(session: PlanSession) -> bool:
    return session.finalized


# ================================================================ 工具声明
def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


_AID = {"type": "integer", "description": "景点 id（必须来自 list_attractions / plan_days 返回的 id）"}
_AIDS = {
    "type": "array",
    "items": {"type": "integer"},
    "description": "景点 id 数组，按你想要的游览顺序排列",
}

TOOL_SPECS: list[dict] = [
    _tool(
        "get_city_info",
        "获取城市概况：候选住宿片区、本次行程的天数/出发返回时间/出行方式，以及每天可用的时间预算（分钟）。"
        "开始规划前先调这个。",
        {},
    ),
    _tool(
        "list_attractions",
        "列出用户勾选的景点池（含 id、所在区、热度、建议游览分钟、简介）。"
        "可以按区或最低热度筛选。规划前先调这个看清楚有什么可选。",
        {
            "district": {"type": "string", "description": "只看某个区，如「青羊区」，留空看全部"},
            "min_heat": {"type": "integer", "description": "只看热度不低于该值的景点"},
            "limit": {"type": "integer", "description": "最多返回几个，默认 40"},
        },
    ),
    _tool(
        "route_time",
        "查询两个景点之间的实际通行时间（硬编码按直线距离与出行方式折算，你不需要自己估算距离）。",
        {
            "from_attraction_id": _AID,
            "to_attraction_id": _AID,
            "transport": {"type": "string", "enum": ["taxi", "transit"], "description": "留空用本次行程的设定"},
        },
        ["from_attraction_id", "to_attraction_id"],
    ),
    _tool(
        "plan_days",
        "【强烈建议先调】让硬编码引擎按地理顺路 + 容量约束算出分天骨架。"
        "返回每天建议去哪些景点（已排好顺路顺序）、各天游览分钟数，以及装不下需要舍弃的景点。"
        "你可以在此基础上微调，但时间与容量以引擎的判断为准。",
        {
            "attraction_ids": _AIDS,
            "days": {"type": "integer", "description": "天数，留空用本次行程设定"},
            "transport": {"type": "string", "enum": ["taxi", "transit"], "description": "留空用本次行程设定"},
        },
        ["attraction_ids"],
    ),
    _tool(
        "suggest_hotel",
        "按一组景点的几何中心推荐住宿片区，返回推荐片区、理由和备选片区的取舍代价。"
        "你也可以直接从 get_city_info 的候选片区里自己挑。",
        {"attraction_ids": _AIDS},
        ["attraction_ids"],
    ),
    _tool(
        "note_attraction",
        "给某个景点写一句**具体可执行**的建议（几点去人少 / 从哪个门进 / 要不要预约 / 排队多久 / 周边吃什么）。"
        "禁止「值得一去」「风景优美」这类空话。可以在一轮里并行调用多次。",
        {
            "attraction_id": _AID,
            "advice": {"type": "string", "description": "一句话建议，不超过 200 字"},
        },
        ["attraction_id", "advice"],
    ),
    _tool(
        "build_day",
        "【核心】把某一天的景点顺序物化成完整时间轴。"
        "所有时间、停留时长、路程耗时、午晚餐插入位置都由硬编码算出，你不需要也不能指定时间。"
        "返回那天的开始/结束时间、紧凑时间轴、以及警告（比如「装不下已剔除」）。"
        "每天都在这里过一遍，如果结束时间太晚就减少景点。",
        {
            "day": {"type": "integer", "description": "第几天，从 1 开始"},
            "attraction_ids": _AIDS,
            "theme": {"type": "string", "description": "当天主题一句话，如「青羊区老城线｜宽窄巷子 → 人民公园」"},
            "lunch_area": {"type": "string", "description": "午餐所在区域，如「宽窄巷子周边」。不要写具体店名（店会换）"},
            "lunch_hint": {"type": "string", "description": "午餐本地特色，如「甜水面、钟水饺、钵钵鸡」"},
            "dinner_area": {"type": "string", "description": "晚餐所在区域，一般就是住宿片区"},
            "dinner_hint": {"type": "string", "description": "晚餐本地特色，如「火锅、串串、老妈蹄花」"},
            "hotel_area": {"type": "string", "description": "当天建议住宿片区，留空则按几何中心自动选"},
        },
        ["day", "attraction_ids"],
    ),
    _tool(
        "check_day",
        "复核某一天的时间轴（时间是否自洽、饭点是否齐、收尾是否超时）。finalize 前抽查几天。",
        {"day": {"type": "integer"}},
        ["day"],
    ),
    _tool(
        "drop_attraction",
        "明确舍弃一个景点并写明原因（会展示给用户）。装不下的景点请走这里，不要硬塞进时间轴。",
        {
            "attraction_id": _AID,
            "reason": {"type": "string", "description": "具体原因，说清为什么取舍"},
        },
        ["attraction_id", "reason"],
    ),
    _tool(
        "finalize_plan",
        "【最后调用】提交方案。会自动补齐你没物化的天与漏安排的景点，并做一次硬编码复核。"
        "调用后编排结束。",
        {
            "summary": {"type": "string", "description": "整体方案总述：分天逻辑 + 取舍，不超过 300 字"},
            "must_visit_reasons": {
                "type": "array",
                "description": "必去景点的理由",
                "items": {
                    "type": "object",
                    "properties": {
                        "attraction_id": {"type": "integer"},
                        "name": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
        ["summary"],
    ),
]
