"""行程规划的 LLM 编排层。

## 架构（两阶段，阶段二并行）

    阶段 ①  硬编码分天        planner.prune_to_capacity → assign_days
                             纯本地、瞬时、确定。地理顺路 + 容量裁剪 + 时段排序
                             （熊猫基地排上午这类硬知识都在这里生效）
    阶段 ②  LLM 并行写内容     每天一路，ThreadPoolExecutor 并发
                             每路只输出：当天主题 + 各景点建议 + 餐饮区域与特色小吃
    阶段 ③  硬编码物化时间轴   planner.materialize_day
                             时间、饭点、路程全部算出来，LLM 碰不到
    阶段 ④  LLM 终检          读最终时间轴，提意见 + 润色总述

## 为什么这么切

原来让 LLM 用 11 个工具自己编排（get_city_info → plan_days → build_day ×N → …），
一次规划 26 次工具调用、7~8 轮 API 往返，实测 1.1 分钟。问题是：

1. **轮次是串行的**，每轮都要等模型想完才发下一次；
2. 模型大量时间花在**重新推导硬编码已经算好的东西**（分天、路程、容量）；
3. 而且它推得还更差——实测把熊猫基地挪到 14:20（熊猫在午睡），
   提示词写了两遍都没拦住。

改成「先硬编码分天、再并行写内容」之后：
- 分天质量由硬编码保证（不会被模型改坏）；
- 每天一路互不依赖 → **并发**，N 天只花 1 路的墙钟时间；
- 每路的输出小（主题 + 几句话），提示词也小 → 单次更快；
- 阶段 ② 完全不产出时间，所以时间不可能算错。

代价是模型失去了「探索」能力（不能自己试算某天塞不塞得下）。
但实测它的探索结论基本等同于 plan_days 的输出，这部分能力本来就没产生价值。

## 保留的工具编排

`tools.py` 那套 11 个工具的 function calling 编排仍然保留，用 `LLM_MODE=tools` 打开。
它适合需要模型深度介入分天的场景；默认走本文件的并行流水线。
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from app.core import LLMFormatError, extract_json, llm, settings
from app.trip import planner
from app.trip.models import Attraction, City
from app.trip.schemas import (
    DayPlan,
    DroppedItem,
    HotelAdvice,
    MustVisitReason,
    PlanRequest,
    PlanResult,
    PlanReview,
)

logger = logging.getLogger("triptide.trip.llm")

__all__ = [
    "LLMError",
    "chat_json",
    "extract_json",
    "run_pipeline",
    "review_plan",
    "is_available",
]

MAX_WORKERS = 4          # 并行路数：一般 1~7 天，4 路够用且不会打爆限流
DAY_RETRY = 1            # 单天内容生成失败重试次数


class LLMError(RuntimeError):
    """模型不可用，或流水线无法产出可用方案。"""


# ================================================================ 提示词
DAY_SYSTEM = """你是「TripTide」的行程编辑。系统已经用地理邻近度把景点分好了天，
你只负责为**指定的这一天**补充内容。不要质疑分天结果，不要调整顺序，不要增删景点。

只输出 JSON，不要任何解释：
{
  "theme": "当天主题一句话",
  "notes": [{"attraction_id": 1, "advice": "一句具体建议"}],
  "lunch": {"area": "午餐区域", "hint": "本地特色小吃"},
  "dinner": {"area": "晚餐区域", "hint": "本地特色"}
}

硬性要求：
1. notes 必须覆盖当天**每一个**景点，一条不漏。
2. advice 要具体可执行：几点去人少、从哪个门进、要不要预约、排队多久、周边吃什么。
   禁止「值得一去」「风景优美」「不容错过」这类空话。
3. 餐饮只给**区域 + 本地特色小吃/菜系**，**不要写具体店名**——店铺随时会换，写了会误导。
   例：{"area":"宽窄巷子周边","hint":"甜水面、钟水饺、钵钵鸡"}。
4. 不要输出任何时间（几点几分），不要输出停留时长，不要输出交通方式。

格式示例（照这个结构输出）：
{"theme":"青羊区老城线｜宽窄巷子 → 人民公园","notes":[{"attraction_id":2,"advice":"从窄巷子东口进人最少，掏耳朵和盖碗茶在巷子中段，井巷子的老照片墙别错过。"}],"lunch":{"area":"宽窄巷子周边","hint":"甜水面、钟水饺、钵钵鸡"},"dinner":{"area":"春熙路/太古里","hint":"火锅、串串、老妈蹄花"}}"""


REVIEW_SYSTEM = """你是行程审核员。你会收到一份**已经生成好的**时间轴，它的时间、路程、饭点都经过硬编码校验，
所以不要质疑时间数字本身。你要审的是「安排是否合理」，并输出 JSON。

只输出 JSON：
{
  "ok": true/false,
  "summary": "润色后的方案总述，不超过 300 字，说清分天逻辑与取舍",
  "issues": [{"day": 1, "level": "warn", "text": "一句话说明问题，不超过 200 字"}]
}

审这些点（没有就返回空数组，不要硬凑）：
- 某天动线是否来回折返、有没有更好的顺序
- 各天活动量差异是否过大（一天 3 小时、另一天 12 小时）
- 住宿是否跟着动线走（本次行程默认全程不换酒店，判断这个选择是否合理）
- 有没有常识问题（博物馆闭馆早、夜景类排在早上、远郊日还要塞市区）
- 必去景点有没有被无故舍弃

level：info（提醒）/ warn（建议调整）/ error（明显不合理）。确实没问题就 ok=true 且 issues 为空。"""


def _day_user_prompt(
    city: City, req: PlanRequest, day_index: int, items: list[Attraction],
    hotel: HotelAdvice, dropped: list[DroppedItem],
) -> str:
    start_s, end_s = planner.day_window(req, day_index, req.days)
    lines = [
        f"城市：{city.name}",
        f"这是 {req.days} 天行程里的第 {day_index} 天（{start_s} 出发，{end_s} 前回到住宿片区）",
        f"出行方式：{req.transport_label}｜住宿片区：{hotel.area}",
        "",
        "当天景点（顺序已按地理顺路排好，不要改）：",
    ]
    for a in items:
        flags = []
        if a.must_visit or a.heat >= 90:
            flags.append("必去")
        lines.append(
            f"  id={a.id} {a.name}｜{a.district or '—'}｜热度 {a.heat}｜"
            f"建议 {a.visit_minutes} 分钟｜{'/'.join(flags) or '可选'}｜{a.intro or '—'}"
        )
    districts = sorted({a.district for a in items if a.district})
    if districts:
        lines.append(f"\n当天涉及片区：{'、'.join(districts)}")
    if dropped:
        names = "、".join(d.name for d in dropped[:6])
        lines.append(f"本次行程已舍弃：{names}（不用管它们，仅供你理解取舍）")
    lines.append("\n请只输出这一天的 JSON。")
    return "\n".join(lines)


def _review_user_prompt(plan: PlanResult, req: PlanRequest) -> str:
    lines = [
        f"城市：{plan.city}｜{plan.days} 天｜{req.start_time} 出发｜"
        f"{req.transport_label}｜倾向：{req.pace_label}",
        "",
    ]
    for dp in plan.day_plans:
        lines.append(f"Day {dp.day}（{dp.pace}）{dp.theme}")
        for n in dp.nodes:
            tag = {
                "attraction": "景点", "meal": "餐饮", "hotel": "住宿",
                "depart": "出发", "transit": "活动",
            }.get(n.type, n.type)
            extra = f"（停留 {n.stay_minutes} 分）" if n.type == "attraction" else ""
            lines.append(f"  {n.time} [{tag}] {n.name}{extra}")
        if dp.hotel:
            lines.append(f"  住宿：{dp.hotel.area}")
        lines.append("")

    if plan.dropped:
        lines.append("舍弃的景点：" + "；".join(f"{d.name}（{d.reason}）" for d in plan.dropped))
    return "\n".join(lines)


# ================================================================ 轻量调用
def chat_json(messages, *, temperature: float = 0.8, max_tokens: int = 4096) -> str:
    """只要一段 JSON 文本、不绑定 schema 的调用（如 seed 生成景点名单）。"""
    try:
        resp = llm.chat(
            messages,
            model=settings.model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(str(exc)) from exc
    return resp.content


def is_available() -> bool:
    return settings.has_llm


# ================================================================ 阶段 ②
def _gen_day(
    city: City, req: PlanRequest, day_index: int, items: list[Attraction],
    hotel: HotelAdvice, dropped: list[DroppedItem],
) -> dict[str, Any]:
    """生成一天的内容（主题 + 景点建议 + 餐饮区域）。纯文本产出，不含任何时间。"""
    messages = [
        {"role": "system", "content": DAY_SYSTEM},
        {"role": "user", "content": _day_user_prompt(city, req, day_index, items, hotel, dropped)},
    ]
    last_err: Exception | None = None
    for attempt in range(1, DAY_RETRY + 2):
        try:
            resp = llm.chat(
                messages,
                model=settings.model_name,
                temperature=0.6,
                max_tokens=2000,
                response_format={"type": "json_object"},
            )
            data = extract_json(resp.content)
            if not isinstance(data, dict):
                raise ValueError("返回的不是 JSON 对象")
            return data
        except (LLMFormatError, ValueError) as exc:
            last_err = exc
            logger.warning("Day %d 内容生成第 %d 次失败：%s", day_index, attempt, exc)
            if attempt <= DAY_RETRY:
                messages.append({"role": "assistant", "content": "（上一次输出不合法）"})
                messages.append({
                    "role": "user",
                    "content": f"上一次输出解析失败：{exc}。请严格按要求的 JSON 结构重新输出，"
                               f"只输出 JSON，不要任何解释。",
                })
        except RuntimeError as exc:
            raise LLMError(str(exc)) from exc
    raise LLMError(f"Day {day_index} 内容生成失败：{last_err}")


def _apply_day_content(
    dp: DayPlan, data: dict[str, Any], items: list[Attraction]
) -> tuple[DayPlan, dict[int, str], str, str, str, str]:
    """把模型产出套回已物化的一天。只覆盖文案，不碰任何时间字段。"""
    valid_ids = {a.id for a in items}

    theme = str(data.get("theme") or "").strip()[:80]
    if theme:
        dp.theme = theme

    notes: dict[int, str] = {}
    for n in data.get("notes") or []:
        if not isinstance(n, dict):
            continue
        try:
            aid = int(n.get("attraction_id") or 0)
        except (TypeError, ValueError):
            continue
        text = str(n.get("advice") or "").strip()
        if aid in valid_ids and text:
            notes[aid] = text[:200]

    lunch = data.get("lunch") or {}
    dinner = data.get("dinner") or {}
    return (
        dp,
        notes,
        str(lunch.get("area") or "").strip()[:40],
        str(lunch.get("hint") or "").strip()[:120],
        str(dinner.get("area") or "").strip()[:40],
        str(dinner.get("hint") or "").strip()[:120],
    )


# ================================================================ 主入口
def run_pipeline(
    city: City,
    attractions: list[Attraction],
    req: PlanRequest,
    baseline: PlanResult,
) -> tuple[PlanResult, list[str]]:
    """并行流水线。返回 (最终方案, 执行轨迹)。"""
    days = req.days
    trace: list[str] = []
    t0 = time.time()

    # ---------- 阶段 ①：硬编码分天（瞬时、确定） ----------
    kept, dropped = planner.prune_to_capacity(city, attractions, req)
    groups = planner.assign_days(city, kept, days, req.transport, req.pace)
    hotels = planner.plan_hotels(city, groups)
    trace.append(f"plan_days days={days} kept={len(kept)} dropped={len(dropped)}")

    # 逐天裁剪（时间窗可能首末天不同）
    trimmed: list[list[Attraction]] = []
    for idx, group in enumerate(groups, start=1):
        day_kept, spilled = planner.trim_day(city, group, req, planner.day_budget(req, idx, days))
        trimmed.append(day_kept)
        for a in spilled:
            dropped.append(
                DroppedItem(
                    name=a.name,
                    reason=f"Day {idx} 时间装不下，且顺延会影响后续动线，建议单独安排。",
                    attraction_id=a.id,
                )
            )

    # ---------- 阶段 ①.5：LLM 审阅分天（读矩阵，硬校验把关） ----------
    pool_items = [a for g in trimmed for a in g]
    reviewed = plan_days_with_llm(city, pool_items, req, trimmed)
    if reviewed is not None:
        trimmed = reviewed
        hotels = planner.plan_hotels(city, trimmed)   # 分天变了，住宿片区要重算
        trace.append("review_days ok")
    else:
        trace.append("review_days skipped")

    # ---------- 阶段 ②：并行生成每天内容 ----------
    results: dict[int, dict[str, Any]] = {}
    errors: dict[int, str] = {}
    workers = max(1, min(MAX_WORKERS, days))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tripday") as pool:
        futures = {
            pool.submit(_gen_day, city, req, i, trimmed[i - 1], hotels[i - 1], dropped): i
            for i in range(1, days + 1)
            if trimmed[i - 1]
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
                trace.append(f"gen_day day={idx} ok")
            except Exception as exc:  # noqa: BLE001 —— 单天失败不该拖垮整份方案
                errors[idx] = str(exc)
                trace.append(f"gen_day day={idx} ✗")
                logger.warning("Day %d 内容生成失败，该天将退回规则文案：%s", idx, exc)

    if len(errors) == days and days > 0:
        raise LLMError(f"全部 {days} 天内容生成失败：{list(errors.values())[:2]}")

    # ---------- 阶段 ③：硬编码物化时间轴 ----------
    day_plans: list[DayPlan] = []
    for idx in range(1, days + 1):
        items = trimmed[idx - 1]
        if not items:
            day_plans.append(planner.empty_day(city, req, idx, hotels[idx - 1]))
            continue

        dp = planner.materialize_day(
            city, idx, items, req,
            total_days=days,
            hotel=hotels[idx - 1],
        )

        data = results.get(idx)
        if data:
            dp, notes, l_area, l_hint, d_area, d_hint = _apply_day_content(dp, data, items)
            # 用模型给的建议/餐饮区域重建一次，确保文案与时间轴一起生效
            dp = planner.materialize_day(
                city, idx, items, req,
                total_days=days,
                hotel=hotels[idx - 1],
                theme=dp.theme,
                advice=notes,
                lunch_area=l_area,
                lunch_hint=l_hint,
                dinner_area=d_area,
                dinner_hint=d_hint,
            )
        day_plans.append(dp)

    must_reasons = [
        MustVisitReason(
            name=a.name,
            reason=f"热度 {a.heat}｜{a.intro or '当地标志性去处'}",
            attraction_id=a.id,
        )
        for a in sorted(kept, key=lambda x: -x.heat)[:3]
    ]

    summary = _compose_summary(city, req, kept, dropped)
    plan = PlanResult(
        city=city.name,
        days=days,
        summary=summary,
        day_plans=day_plans,
        must_visit_reasons=must_reasons,
        dropped=dropped,
    )

    trace.append(f"materialize days={len(day_plans)} elapsed={time.time() - t0:.1f}s")
    logger.info("流水线完成：%d 天 / %d 景点 / 并行 %d 路 / %.1fs",
                days, len(kept), workers, time.time() - t0)
    return plan, trace


# ================================================================ 阶段 ①.5：LLM 审阅分天
PLAN_DAYS_SYSTEM = """你是行程编排助手。系统已按地理邻近度做过一版分天，你负责审阅并微调。

**你会拿到量化的通行时间矩阵（分钟），不要自己估算距离，直接读表。**

不可违背的原则：
1. **地理第一**：矩阵里通行 ≤15 分钟的景点必须同一天 —— 这类组合若被拆到不同天，方案会被直接拒绝。
2. 其次是热度与体验：让每天的动线更顺、强度更均衡。
3. 通勤 >45 分钟的（远郊）和大景点应独占一天。

只输出 JSON，不要任何解释：
{
  "days": [{"day": 1, "attraction_ids": [3, 4]}, {"day": 2, "attraction_ids": [7]}],
  "themes": ["当天主题一句话"],
  "reasoning": "一句话说明你调整了什么"
}

硬性要求：
- 每个 id 必须且只能出现一次，总数与输入完全一致
- day 从 1 连续递增，天数必须等于给定天数，不允许空天
- 不要发明输入里没有的 id
"""


def _items_text(city: City, items: list[Attraction], req: PlanRequest) -> str:
    """景点清单（含级别、远郊标记）——喂给模型的一半上下文。"""
    lines = []
    for a in items:
        flags = []
        if planner._is_must(a):
            flags.append("必去")
        if planner.is_remote(city, a, req.transport):
            flags.append("远郊")
        grade = planner.grade_attraction(a)
        lines.append(
            f"  id={a.id} {a.name}｜{a.district or '—'}｜{grade}景点｜"
            f"游览 {a.visit_minutes} 分｜热度 {a.heat}｜{'/'.join(flags) or '普通'}"
        )
    return "\n".join(lines)


def _matrix_text(city: City, items: list[Attraction], req: PlanRequest) -> str:
    """通行时间矩阵的紧凑文本。模型读表就行，不必猜距离。"""
    matrix = planner.travel_matrix(city, items, req.transport)
    ids = [a.id for a in items]
    head = "      " + "".join(f"{i:>5}" for i in ids)
    rows = [head]
    for a in items:
        cells = []
        for b in items:
            if a.id == b.id:
                cells.append("    -")
                continue
            lo, hi = (a.id, b.id) if a.id < b.id else (b.id, a.id)
            cells.append(f"{matrix.get((lo, hi), 0):>5}")
        rows.append(f"{a.id:>4}  " + "".join(cells))
    return "\n".join(rows)


def _plan_days_prompt(
    city: City, items: list[Attraction], req: PlanRequest, baseline: list[list[Attraction]]
) -> str:
    budgets = [planner.day_budget(req, i, req.days) for i in range(1, req.days + 1)]
    base_text = " ｜ ".join(
        f"Day{i}: {', '.join(str(a.id) for a in g) or '空'}" for i, g in enumerate(baseline, 1)
    )
    return (
        f"城市：{city.name}｜共 {req.days} 天｜出行方式：{req.transport_label}｜"
        f"节奏：{req.pace_label}\n"
        f"每天可用时间（游览 + 赶路）：{budgets} 分钟\n\n"
        f"景点清单：\n{_items_text(city, items, req)}\n\n"
        f"通行时间矩阵（分钟，行/列都是景点 id）：\n{_matrix_text(city, items, req)}\n\n"
        f"系统当前的分天（供参考，可以改）：\n  {base_text}\n\n"
        f"请审阅后输出 {req.days} 天的最终分天。"
    )


def _validate_days(
    city: City, req: PlanRequest, items: list[Attraction], groups: list[list[int]]
) -> str | None:
    """硬校验 LLM 的分天结果。返回错误说明；None 表示通过。"""
    want = {a.id for a in items}
    flat = [i for g in groups for i in g]
    if sorted(flat) != sorted(want):
        return "景点必须不重不漏，且只能用输入里给出的 id"
    if len(groups) != req.days:
        return f"天数必须是 {req.days}，你给了 {len(groups)}"
    if any(not g for g in groups):
        return "不允许出现空天"

    by_id = {a.id: a for a in items}
    day_of = {aid: idx for idx, g in enumerate(groups, 1) for aid in g}

    # 紧邻（直线 ≤ CLUSTER_MAX_METERS）必须同天 —— 这是「地理第一」的落点
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            close = planner.haversine_m(a.lat, a.lng, b.lat, b.lng) <= planner.CLUSTER_MAX_METERS
            if close and day_of[a.id] != day_of[b.id]:
                return f"「{a.name}」与「{b.name}」步行可达，必须排在同一天"

    # 独占型必须独占一天
    for g in groups:
        if len(g) > 1:
            for aid in g:
                if planner._is_standalone(city, by_id[aid], req.transport):
                    return f"「{by_id[aid].name}」需要独占一天"

    # 每天不能超出预算
    for idx, g in enumerate(groups, 1):
        load = planner._group_load(city, [by_id[i] for i in g], req.transport)
        budget = planner.day_budget(req, idx, req.days)
        if load > int(budget * 1.05):
            return f"Day {idx} 需要 {load} 分钟，超过当天预算 {budget} 分钟"

    return None


def plan_days_with_llm(
    city: City,
    items: list[Attraction],
    req: PlanRequest,
    baseline: list[list[Attraction]],
) -> list[list[Attraction]] | None:
    """阶段 ①.5：让 LLM 审阅并微调硬编码的分天。

    **关键设计**：给模型的是量化通行时间矩阵，不是让它猜距离 ——
    这样才不会重演「凭常识把熊猫基地排到下午」那类错误。
    输出必须通过 `_validate_days` 才会被采纳；两次都不过就返回 None，
    调用方继续用硬编码结果（保底，不引入新的失败面）。
    """
    if not settings.has_llm or not items:
        return None

    by_id = {a.id: a for a in items}
    prompt = _plan_days_prompt(city, items, req, baseline)

    for attempt in (1, 2):
        try:
            raw = chat_json(
                [
                    {"role": "system", "content": PLAN_DAYS_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            data = extract_json(raw)
            raw_days = data.get("days") or []
            groups = [
                [int(i) for i in (d.get("attraction_ids") or [])]
                for d in raw_days
                if isinstance(d, dict)
            ]
            err = _validate_days(city, req, items, groups)
            if err is None:
                logger.info(
                    "LLM 分天审阅通过（第 %d 次）：%s",
                    attempt, str(data.get("reasoning") or "")[:80],
                )
                return [[by_id[i] for i in g] for g in groups]
            logger.warning("LLM 分天审阅第 %d 次被拒：%s", attempt, err)
            prompt = f"{prompt}\n\n上一次的方案被拒绝，原因：{err}。请修正后重新输出完整 JSON。"
        except Exception as exc:  # noqa: BLE001 —— 审阅失败不该影响主流程
            logger.warning("LLM 分天审阅第 %d 次异常：%s", attempt, exc)

    logger.info("LLM 分天审阅未通过，沿用硬编码分天")
    return None


def _compose_summary(
    city: City, req: PlanRequest, kept: list[Attraction], dropped: list[DroppedItem]
) -> str:
    """规则兜底总述（终检会覆盖它）。"""
    total_h = sum(a.visit_minutes for a in kept) / 60
    parts = [
        f"{city.name} {req.days} 天：{len(kept)} 个景点按地理邻近度分成 {req.days} 条顺路线，"
        f"日均游览约 {total_h / max(1, req.days):.1f} 小时，{req.transport_label}通勤。",
        f"住宿全程固定在同一片区，避免频繁搬行李。",
    ]
    if dropped:
        parts.append(f"因时间预算舍弃 {len(dropped)} 个（{'、'.join(d.name for d in dropped[:3])}）。")
    return "".join(parts)[:300]


def review_plan(plan: PlanResult, req: PlanRequest) -> PlanReview | None:
    """阶段 ④：终检。失败不影响主流程。"""
    try:
        review, resp = llm.with_structured_output(
            [
                {"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": _review_user_prompt(plan, req)},
            ],
            PlanReview,
            model=settings.model_name,
            temperature=0.3,
            max_tokens=1200,
            return_raw=True,
        )
        usage = (resp.usage or {}).get("total_tokens", "-")
        logger.info("终检完成 ok=%s issues=%d tokens=%s", review.ok, len(review.issues), usage)
        return review
    except LLMFormatError as exc:
        logger.warning("终检输出不合法，跳过：%s", exc)
    except RuntimeError as exc:
        logger.warning("终检调用失败，跳过：%s", exc)
    return None
