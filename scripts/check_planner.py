"""规则引擎回归检查（不启服务，直接调 planner）。

用法：
    cd backend && venv/Scripts/python ../scripts/check_planner.py
    （也可以先 cd backend，把本文件内容当脚本跑）

校验项：
  1. 时间自洽：每个节点 time == 上一站 time + 上一站 stay_minutes + 本节点 travel_minutes
  2. 午餐恰好 1 个，且落在 11:00~14:30
  3. 晚餐恰好 1 个，且落在 17:30~21:30
  4. 出发时间 == 用户设定；hotel 是当天最后一个节点
  5. 主题里提到的景点必须真的在当天时间轴里
  6. 每个勾选的景点要么出现在时间轴，要么出现在 dropped（不能凭空消失）
"""

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


def check(city_name, ids, days, transport, start="09:00", ret="19:30", verbose=True):
    db = SessionLocal()
    try:
        city = db.scalar(select(City).where(City.name == city_name))
        rows = db.scalars(select(Attraction).where(Attraction.id.in_(ids))).all()
        atts = sorted(rows, key=lambda a: -a.heat)
        req = PlanRequest(
            city=city_name, attraction_ids=ids, days=days,
            start_time=start, return_time=ret, transport=transport,
        )
        plan = plan_fallback(city, atts, req)
    finally:
        db.close()

    if verbose:
        print(f"\n{'='*78}")
        print(f"{city_name} | {len(atts)} 景 | {days} 天 | {transport} | {start}→{ret}")
        print(f"{'='*78}")
        print(plan.summary)

    problems = []
    seen_ids = set()

    for dp in plan.day_plans:
        meals = [n for n in dp.nodes if n.type == "meal"]
        lunch = [n for n in meals if "午餐" in n.name]
        dinner = [n for n in meals if "晚餐" in n.name]

        if len(lunch) != 1:
            problems.append(f"Day{dp.day} 午餐数量={len(lunch)}")
        elif not ("11:00" <= lunch[0].time <= "16:00"):
            problems.append(f"Day{dp.day} 午餐时间离谱 {lunch[0].time}")
        if len(dinner) != 1:
            problems.append(f"Day{dp.day} 晚餐数量={len(dinner)}")
        elif not ("17:30" <= dinner[0].time <= "21:30"):
            problems.append(f"Day{dp.day} 晚餐时间离谱 {dinner[0].time}")

        if dp.nodes[0].time != start:
            problems.append(f"Day{dp.day} 出发 {dp.nodes[0].time} != {start}")
        if dp.nodes[-1].type != "hotel":
            problems.append(f"Day{dp.day} 最后一个节点不是 hotel")

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

        # 主题不能提到当天没去的景点
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
    for i in ids:
        if i not in seen_ids and i not in dropped_ids:
            problems.append(f"景点 id={i} 既没排入也没进 dropped（凭空消失）")

    if verbose and plan.dropped:
        print("\n  🗑️  舍弃：")
        for d in plan.dropped:
            print(f"     · {d.name} —— {d.reason}")

    if verbose:
        print(f"\n  {'✅ 通过' if not problems else '❌ ' + str(len(problems)) + ' 个问题：' + '；'.join(problems)}")
    return problems


ALL = []
db = SessionLocal()
cd = db.scalars(select(Attraction).where(Attraction.city_id == 1).order_by(Attraction.heat.desc())).all()
bj = db.scalars(select(Attraction).where(Attraction.city_id == 2).order_by(Attraction.heat.desc())).all()
db.close()

CASES = [
    ("成都", [a.id for a in cd[:8]], 3, "taxi"),      # 市区线 + 两处远郊，压力最大
    ("成都", [a.id for a in cd[:8]], 3, "transit"),   # 公交地铁，速度更慢
    ("成都", [a.id for a in cd[:5]], 1, "taxi"),      # 一天塞 5 个
    ("成都", [a.id for a in cd[:3]], 2, "taxi"),      # 景点比天数少
    ("成都", [a.id for a in cd[:14]], 7, "taxi"),     # 长行程
    ("成都", [a.id for a in cd[:20]], 4, "taxi"),     # 全选 20 个
    ("北京", [a.id for a in bj[:12]], 4, "transit"),  # 含八达岭/十三陵远郊
    ("北京", [a.id for a in bj[:6]], 2, "taxi"),
]

for city_name, ids, days, transport in CASES:
    ALL += check(city_name, ids, days, transport)

print(f"\n\n{'#'*78}")
if ALL:
    print(f"❌ 总计 {len(ALL)} 个问题")
    for p in ALL:
        print("  -", p)
else:
    print("✅ 全部用例通过")
