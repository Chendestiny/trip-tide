"""重算首页排序分并落库（`trip_city.rank_score`）。

用法：
    cd backend && venv/Scripts/python ../scripts/rank_cities.py              # 重算并写库
    cd backend && venv/Scripts/python ../scripts/rank_cities.py --dry-run    # 只看会变成什么，不写库
    cd backend && venv/Scripts/python ../scripts/rank_cities.py --top 25     # 只看前 25 名

**什么时候要跑**：
  · 改过 `trip_attraction.heat`、或增删过景点之后（手工改库、或跑了别的脚本）
  · `seed` 会自动跑，**不用手动**（`seed.main` 末尾已调 `recompute_rank_scores()`）
  · 调排序口径之后（改 `service.RANK_TOP / RANK_DECAY / RANK_TOP3_BOOST`）

漏跑**不会报错**，只是首页顺序和实际数据对不上。用 `scripts/check_rank.py` 可以查出这种不一致。

口径（**唯一实现在 `service.rank_score()`**，这里不复制公式）：

    score = Σ_{i=1..min(8, n)}  heat_i × 0.9^(i-1) × B_i
    其中 B_1 = 1.5、B_2 = B_3 = 1.2、其余 = 1

即「前 8 个高热度景点」的指数加权和，**第 1 名（城市名片）再抬 1.5 倍、第 2/3 名 1.2 倍**。
为什么这样、以及 TOP=8 / λ=0.9 的实测依据，见 `service.py` 里 RANK_* 常量上方的注释。
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
from app.trip import service  # noqa: E402
from app.trip.models import Attraction, City  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="重算首页排序分（trip_city.rank_score）")
    ap.add_argument("--dry-run", action="store_true", help="只打印会变成什么，不写库")
    ap.add_argument("--top", type=int, default=20, help="打印前几名（默认 20）")
    args = ap.parse_args()

    db = SessionLocal()

    # 先算一遍「新值」，同时拿到「旧值」做差异对比
    by_city: dict[int, list[int]] = {}
    for city_id, heat in db.execute(select(Attraction.city_id, Attraction.heat)).all():
        by_city.setdefault(int(city_id), []).append(int(heat))

    cities = db.scalars(select(City)).all()
    plan: list[tuple[City, int, int]] = []      # (城市, 旧分, 新分)
    for c in cities:
        plan.append((c, c.rank_score, service.rank_score(by_city.get(c.id, []))))

    changed = [p for p in plan if p[1] != p[2]]
    print(f"目的地 {len(plan)} 个，其中 {len(changed)} 个的排序分需要更新"
          + ("（--dry-run，未写库）" if args.dry_run else ""))

    print()
    print(f"口径：前 {service.RANK_TOP} 个景点的指数加权和（λ={service.RANK_DECAY}，"
          f"第 1 名 ×{service.RANK_FIRST_BOOST}、第 2/3 名 ×{service.RANK_TOP3_BOOST}）")
    print("-" * 92)
    print(f"{'#':>3} {'目的地':<14}{'kind':<7}{'景数':>4}{'旧分':>8}{'新分':>8}{'变化':>8}")
    print("-" * 92)
    for i, (c, old, new) in enumerate(sorted(plan, key=lambda p: p[2], reverse=True)[: args.top], 1):
        n = len(by_city.get(c.id, []))
        delta = new - old
        print(f"{i:>3} {c.name:<14}{c.kind:<7}{n:>4}{old:>8}{new:>8}{delta:>+8}")
    print("-" * 92)

    if args.dry_run:
        print()
        print("（--dry-run：没有写库。去掉该参数即落库。）")
        db.close()
        return 0

    n_changed = service.recompute_rank_scores(db)
    print()
    print(f"✅ 已写库：{n_changed} 个目的地更新，宫格缓存已作废")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
