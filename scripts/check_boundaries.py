"""跨目的地边界检查 —— 同一个景点不能被两个目的地同时收。

背景：用户提过「区域的景点和城市的景点是同一个对象吧？我怕重复了」。
结构上它们共用 `trip_attraction` 一张表（靠 `city_id` 归属），**是同一个对象**；
但唯一约束是 `UniqueConstraint(city_id, name)` —— **每目的地唯一，不是全局唯一**，
所以同一个景点完全可能被两个目的地各收一份。

踩过一次：先灌 20 个 region、后灌 30 个 city，而城市那边的提示词不知道 region 的存在，
**一次冒出 19 个跨目的地重名**（西宁↔青海湖环线收了同一个塔尔寺、太原↔山西收了同一个晋祠…）。

判据是「**同名 且 同位置**」，缺一不可：

  · 相距 < 30km  → 高德返回的是同一个 POI，**真重复**（留着会让方案给同一个地方算两次停留时间）
  · 相距 ≥ 30km  → 只是同名。西安鼓楼 / 宁波鼓楼 / 银川鼓楼 是**三座不同的建筑**，正确保留

> ⚠️ 反过来也要小心：第一版消重脚本只按「名字相同」删，把西安鼓楼（heat 310）、
> 宁波鼓楼、贵阳文昌阁都误删了。**只看名字必错。**

用法：
    cd backend && venv/Scripts/python ../scripts/check_boundaries.py
    cd backend && venv/Scripts/python ../scripts/check_boundaries.py -v   # 连「可疑归属」一起列

退出码：有真重复 → 1；只有提示 → 0。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.trip import planner  # noqa: E402
from app.trip.models import Attraction, City  # noqa: E402

SAME_PLACE_KM = 30.0     # 小于这个距离 → 同一个 POI
SUSPICIOUS_RATIO = 0.6   # 离别人比自己近这么多倍 → 报「可疑归属」


def main() -> int:
    ap = argparse.ArgumentParser(description="跨目的地边界检查")
    ap.add_argument("-v", "--verbose", action="store_true", help="连「可疑归属」一起列出来")
    args = ap.parse_args()

    db = SessionLocal()
    cities = db.scalars(select(City).order_by(City.heat.desc())).all()
    atts = db.scalars(select(Attraction)).all()
    by_city: dict[int, list[Attraction]] = defaultdict(list)
    for a in atts:
        by_city[a.city_id].append(a)
    name_to = defaultdict(list)
    for a in atts:
        name_to[a.name].append(a)

    problems: list[str] = []

    # ---------- ① 真重复：同名 且 同位置 ----------
    print("=" * 78)
    print(f"① 跨目的地重名（判据：同名 **且** 相距 < {SAME_PLACE_KM:.0f}km 才算重复）")
    print("=" * 78)
    dup_real = dup_homonym = 0
    for name, rows in sorted(name_to.items()):
        owners = {a.city_id for a in rows}
        if len(owners) < 2:
            continue
        # 两两比距离，把「同一个 POI」的挑出来
        same = []
        for i, x in enumerate(rows):
            for y in rows[i + 1:]:
                if x.city_id == y.city_id:
                    continue
                gap = planner.haversine_m(x.lat, x.lng, y.lat, y.lng) / 1000
                if gap < SAME_PLACE_KM:
                    same.append((x, y, gap))
        if same:
            dup_real += 1
            for x, y, gap in same:
                cx = db.get(City, x.city_id)
                cy = db.get(City, y.city_id)
                problems.append(f"「{name}」在「{cx.name}」与「{cy.name}」各有一份（相距 {gap:.1f}km）")
                print(f"  ✗ 「{name}」{cx.name}(heat {x.heat}) ↔ {cy.name}(heat {y.heat})  相距 {gap:.1f}km")
        else:
            dup_homonym += 1
            if args.verbose:
                names = [db.get(City, cid).name for cid in owners]
                print(f"  · 「{name}」在 {'、'.join(names)} —— 同名不同地点，正确保留")
    if not dup_real:
        print(f"  ✅ 没有真重复（另有 {dup_homonym} 组同名不同地点，那是合理的）")

    # ---------- ② 可疑归属 ----------
    print()
    print("=" * 78)
    print("② 可疑归属（离另一个目的地明显更近）—— 多数是合理的边界判断，只提示")
    print("=" * 78)
    centers = [(c.id, c.name, c.center_lat, c.center_lng) for c in cities]
    suspicious = []
    for c in cities:
        for a in by_city.get(c.id, []):
            own = planner.haversine_m(c.center_lat, c.center_lng, a.lat, a.lng) / 1000
            best = min(
                ((planner.haversine_m(lat, lng, a.lat, a.lng) / 1000, name)
                 for cid, name, lat, lng in centers if cid != c.id),
                default=(9e9, "—"),
            )
            if best[0] < own * SUSPICIOUS_RATIO and best[0] < own - 20:
                suspicious.append((c.name, a.name, own, best[1], best[0]))
    if suspicious:
        print(f"  共 {len(suspicious)} 个。常见的合理解释：")
        print("    · 行政区划 vs 几何距离（漠河归黑龙江、库车归南疆）")
        print("    · 同一个景点的两侧（壶口瀑布山西侧 ↔ 陕西侧，各留一份是对的）")
        if args.verbose:
            print()
            for own, name, own_km, near, near_km in sorted(suspicious, key=lambda x: -(x[2] - x[4])):
                print(f"    {name:<22} 现属 {own:<12}{own_km:>6.0f}km → {near:<12}{near_km:>6.0f}km")
        else:
            print("  （加 -v 看完整清单）")
    else:
        print("  ✅ 没有可疑归属")

    # ---------- ③ 空目的地 ----------
    empty = [c.name for c in cities if not by_city.get(c.id)]
    print()
    print("=" * 78)
    print("③ 没有景点的目的地")
    print("=" * 78)
    if empty:
        print(f"  ⚠️ {len(empty)} 个：{'、'.join(empty)}")
        print("     （用 `seed --only-empty --per 8` 补）")
    else:
        print("  ✅ 68 个目的地都有景点")

    print()
    print("=" * 78)
    if problems:
        print(f"❌ 发现 {len(problems)} 处真重复：")
        for p in problems[:10]:
            print(f"    {p}")
        print("\n   修法：按边界规则消重 —— 80km 内归 city，区域让出来。")
        print("   注意**不能只按名字删**，要「同名且同位置」才算重复。")
    else:
        print("✅ 边界检查通过")
    print("=" * 78)

    db.close()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
