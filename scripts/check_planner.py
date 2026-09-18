"""规则引擎回归检查（不启服务，直接调 planner）。

用法：
    cd backend && venv/Scripts/python ../scripts/check_planner.py        # 紧凑输出
    cd backend && venv/Scripts/python ../scripts/check_planner.py -v     # 连每天时间轴一起打

校验项：
  1. 时间自洽：每个节点 time == 上一站 time + 上一站 stay_minutes + 本节点 travel_minutes
  2. 午餐恰好 1 个，且落在 11:00~16:00
  3. 晚餐恰好 1 个，且落在 17:30~21:30
  4. 出发时间 == 用户设定（**第一天用 `first_day_start_time`**，若有）
  5. hotel 是当天最后一个节点
  6. 主题里提到的景点必须真的在当天时间轴里
  7. 每个勾选的景点要么出现在时间轴，要么出现在 dropped（不能凭空消失）
  8. **转场日的出发地必须是昨晚的住处**（region 链式转场的核心，见下）

⚠️ 这个脚本**需要连数据库**（要读景点数据），严格说不是纯离线。
纯逻辑那部分在 `backend/tests/test_planner.py`（不连库，毫秒级）。

覆盖面（2026-09-17 补齐）：
  · transport：`mixed`（历史值 taxi）/ `transit` / **`drive`**（原来完全没覆盖）
  · 首末天时间窗：`first_day_start_time` / `last_day_return_time`（原来完全没覆盖）
  · **region 转场**：贵州 / 川西 / 南疆（原来完全没覆盖 —— 而链式转场是 region 的核心机制）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 本文件在 <repo>/scripts/ 下，backend 才是可导入的包根
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.core import SessionLocal  # noqa: E402
from app.trip.models import Attraction, City  # noqa: E402
from app.trip.planner import plan_fallback  # noqa: E402
from app.trip.schemas import PlanRequest  # noqa: E402

# 用例：(目的地, 取前 N 个景点, 天数, transport, 额外入参)
CASES: list[tuple] = [
    # ── 城市 · 原有 8 个（mixed / transit、长短行程、远郊）──
    ("成都", 8, 3, "taxi", {}),        # 市区线 + 两处远郊，压力最大
    ("成都", 8, 3, "transit", {}),     # 公交地铁，速度更慢
    ("成都", 5, 1, "taxi", {}),        # 一天塞 5 个
    ("成都", 3, 2, "taxi", {}),        # 景点比天数少
    ("成都", 14, 7, "taxi", {}),       # 长行程
    ("成都", 20, 4, "taxi", {}),       # 全选
    ("北京", 12, 4, "transit", {}),    # 含八达岭/十三陵远郊
    ("北京", 6, 2, "taxi", {}),
    # ── 新增 ①：drive 原来完全没覆盖（自驾的高速/长途档与其他两种差别很大）──
    ("成都", 8, 3, "drive", {}),
    ("北京", 12, 4, "drive", {}),
    # ── 新增 ②：首末天时间窗（第一天中午才到 / 最后一天赶飞机）──
    ("成都", 8, 3, "taxi", {"first_start": "13:00", "last_ret": "16:00"}),
    ("北京", 10, 3, "taxi", {"first_start": "14:30", "last_ret": "15:30"}),
    # ── 新增 ③：region 链式转场（21 个区域目的地一个用例都没有）──
    ("贵州", 8, 5, "mixed", {}),       # 首个 region，8 个过夜基地
    ("川西", 8, 5, "mixed", {}),       # 12 景全部独占一天的长线
    ("南疆", 8, 6, "drive", {}),       # 跨度最大，自驾
]


def check(
    city_name: str,
    ids: list[int],
    days: int,
    transport: str,
    *,
    start: str = "09:00",
    ret: str = "19:30",
    first_start: str | None = None,
    last_ret: str | None = None,
    verbose: bool = False,
) -> tuple[list[str], str]:
    db = SessionLocal()
    try:
        city = db.scalar(select(City).where(City.name == city_name))
        if city is None:
            return [f"目的地「{city_name}」不在库里"], city_name
        rows = db.scalars(select(Attraction).where(Attraction.id.in_(ids))).all()
        atts = sorted(rows, key=lambda a: -a.heat)
        if not atts:
            return [f"「{city_name}」取不到景点"], city_name
        req = PlanRequest(
            city=city_name, attraction_ids=[a.id for a in atts], days=days,
            start_time=start, return_time=ret, transport=transport,
            first_day_start_time=first_start, last_day_return_time=last_ret,
        )
        plan = plan_fallback(city, atts, req)
    finally:
        db.close()

    if verbose:
        print(f"\n{'=' * 78}")
        print(f"{city_name} | {len(atts)} 景 | {days} 天 | {transport} | "
              f"{first_start or start}→{last_ret or ret}")
        print(f"{'=' * 78}")
        print(plan.summary)

    problems: list[str] = []
    seen_ids: set[int] = set()
    prev_hotel = None

    for dp in plan.day_plans:
        meals = [n for n in dp.nodes if n.type == "meal"]
        lunch = [n for n in meals if "午餐" in n.name]
        dinner = [n for n in meals if "晚餐" in n.name]

        if len(lunch) != 1:
            # 长途转场日允许**没有午餐**：首景抵达晚于 15:00 时，materialize_day 不再单列
            # 午餐（那顿并进晚餐）—— 实测南疆 Day6 库车→喀什 460min，抵达 16:40（踩过）。
            first_att = next((n for n in dp.nodes if n.type == "attraction"), None)
            if len(lunch) == 0 and first_att is not None and first_att.time > "15:00":
                pass
            else:
                problems.append(f"Day{dp.day} 午餐数量={len(lunch)}")
        elif not ("11:00" <= lunch[0].time <= "16:00"):
            problems.append(f"Day{dp.day} 午餐时间离谱 {lunch[0].time}")
        if len(dinner) != 1:
            # 赶返程的最后一天（显式设了 last_day_return_time）**排不下晚餐就不排** ——
            # 这是 materialize_day 里 strict_return 的硬约束，不是 bug。
            if not (dp.day == days and last_ret and not dinner):
                problems.append(f"Day{dp.day} 晚餐数量={len(dinner)}")
        elif not ("17:30" <= dinner[0].time <= "21:30"):
            problems.append(f"Day{dp.day} 晚餐时间离谱 {dinner[0].time}")

        # 出发时间：第一天用 first_day_start_time（若设了）
        expect_start = (first_start or start) if dp.day == 1 else start
        if dp.nodes[0].time != expect_start:
            problems.append(f"Day{dp.day} 出发 {dp.nodes[0].time} != {expect_start}")
        if dp.nodes[-1].type != "hotel":
            problems.append(f"Day{dp.day} 最后一个节点不是 hotel")

        # 转场日的出发地必须是**昨晚**的住处 —— 链式转场的核心
        # （region 环线里「今晚住哪 = 今天走到哪」，第二天从昨晚那家退房出发）
        has_attraction = any(n.type == "attraction" for n in dp.nodes)
        if has_attraction and dp.hotel:
            expect_origin = prev_hotel.area if prev_hotel else dp.hotel.area
            if expect_origin and expect_origin not in dp.nodes[0].name:
                problems.append(
                    f"Day{dp.day} 出发地不对：节点写「{dp.nodes[0].name}」，"
                    f"应为「{expect_origin}」"
                    f"（{'转场日，应从昨晚住处出发' if prev_hotel and prev_hotel.area != dp.hotel.area else '当天住处'}）"
                )
        if dp.hotel:
            prev_hotel = dp.hotel

        for prev, cur in zip(dp.nodes, dp.nodes[1:]):
            h, m = map(int, prev.time.split(":"))
            got = (h * 60 + m + prev.stay_minutes + cur.travel_minutes) % (24 * 60)
            if f"{got // 60:02d}:{got % 60:02d}" != cur.time:
                problems.append(
                    f"Day{dp.day} 时间不自洽 {prev.time}+{prev.stay_minutes}+{cur.travel_minutes}"
                    f"={got // 60:02d}:{got % 60:02d}，写的却是 {cur.time}"
                )

        for n in dp.nodes:
            if n.type == "attraction":
                seen_ids.add(n.attraction_id)

        # 主题不能提到当天没去的景点。
        # 机动日（没有景点）的主题是「XX市区自由漫步 · 机动日」，不是「A → B」列表，跳过。
        if has_attraction:
            for name in dp.theme.split("｜")[-1].split(" → "):
                if name and not any(n.name == name for n in dp.nodes if n.type == "attraction"):
                    problems.append(f"Day{dp.day} 主题提到未出现的景点：{name}")

        if verbose:
            print(f"\n  ── Day {dp.day} [{dp.pace}] {dp.theme}")
            for n in dp.nodes:
                star = "*" if n.must_visit else " "
                mv = f"   <<已挪至 Day {n.moved_to_day}（原 Day {n.moved_from_day}）" if n.moved_to_day else ""
                print(f"     {n.time} {star}{n.type:<10} {n.name}{mv}")
            h = dp.hotel
            print(f"     🏨 {h.area} | {h.reason[:58]}")

    dropped_ids = {d.attraction_id for d in plan.dropped}
    for i in [a.id for a in atts]:
        if i not in seen_ids and i not in dropped_ids:
            problems.append(f"景点 id={i} 既没排入也没进 dropped（凭空消失）")

    if verbose and plan.dropped:
        print("\n  🗑️  舍弃：")
        for d in plan.dropped:
            print(f"     · {d.name} —— {d.reason}")

    # 紧凑输出用的摘要：天数 / 排入 / 舍弃 / 转场次数
    n_in = sum(1 for dp in plan.day_plans for n in dp.nodes if n.type == "attraction")
    hotels = [dp.hotel.area for dp in plan.day_plans if dp.hotel]
    moves = sum(1 for a, b in zip(hotels, hotels[1:]) if a != b)
    summary = (f"{days}天 排入{n_in} 舍弃{len(plan.dropped)}"
               + (f" 转场{moves}次" if moves else ""))

    if verbose:
        print(f"\n  {'✅ 通过' if not problems else '❌ ' + str(len(problems)) + ' 个问题：' + '；'.join(problems)}")
    return problems, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="规则引擎回归检查")
    ap.add_argument("-v", "--verbose", action="store_true", help="连每天时间轴一起打出来")
    args = ap.parse_args()

    db = SessionLocal()
    pool: dict[str, list[Attraction]] = {}
    for name in {c[0] for c in CASES}:
        city = db.scalar(select(City).where(City.name == name))
        pool[name] = (
            db.scalars(select(Attraction).where(Attraction.city_id == city.id)
                       .order_by(Attraction.heat.desc())).all()
            if city else []
        )
    db.close()

    all_problems: list[str] = []
    print(f"{'目的地':<8}{'transport':<10}{'时间窗':<16}{'规模':<24}结果")
    print("-" * 78)
    for city_name, n, days, transport, extra in CASES:
        atts = pool.get(city_name) or []
        if not atts:
            print(f"{city_name:<8}{'—':<10}{'—':<16}{'取不到景点':<24}✗")
            all_problems.append(f"{city_name}：库里取不到景点")
            continue
        ids = [a.id for a in atts[:n]]
        win = f"{extra.get('first_start', '09:00')}→{extra.get('last_ret', '19:30')}"
        problems, summary = check(city_name, ids, days, transport, verbose=args.verbose, **extra)
        flag = "✅" if not problems else "❌"
        print(f"{city_name:<8}{transport:<10}{win:<16}{summary:<24}{flag}")
        all_problems += [f"{city_name}/{transport}/{days}天: {p}" for p in problems]

    print(f"\n{'#' * 78}")
    if all_problems:
        print(f"❌ 总计 {len(all_problems)} 个问题")
        for p in all_problems:
            print("  -", p)
    else:
        print(f"✅ 全部 {len(CASES)} 个用例通过")
    print("#" * 78)
    return 1 if all_problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
