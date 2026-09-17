"""首页目的地排序体检（**只读**，不写库、不起服务）。

用法：
    cd backend && venv/Scripts/python ../scripts/check_rank.py
    cd backend && venv/Scripts/python ../scripts/check_rank.py --top 20
    cd backend && venv/Scripts/python ../scripts/check_rank.py --quiet   # 只看结论
    cd backend && venv/Scripts/python ../scripts/check_rank.py --drill   # 补打口径对照

它回答两件事：
  ① 首页为什么是这个顺序？
  ② **库里存的排序分是不是过期了？**（改了景点忘记重算，接口不报错、只会静默给旧顺序）

口径（**唯一实现在 `service.rank_score()`**，本脚本不复制公式）：

    score = Σ_{i=1..min(8,n)}  heat_i × 0.9^(i-1) × B_i
    其中 B_1 = 1.5、B_2 = B_3 = 1.2、其余 = 1

即「前 8 个高热度景点」的指数加权和，**第 1 名（城市名片）抬 1.5 倍、第 2/3 名 1.2 倍**；
并列回落 `City.heat`、再回落 `City.id`。
值**预存在 `trip_city.rank_score`**：`service.recompute_rank_scores()` 写，seed 会自动跑，
也可单独 `scripts/rank_cities.py`。

两道自检（任一不过 exit 1）：
  ① `trip_city.rank_score` == 按当前景点数据现算的值
  ② `GET /cities` 的真实顺序 == 按 (rank_score, City.heat, City.id) 重排的顺序

另附诊断 ρ(名次, 景点数量)：**越接近 0 = 越不依赖「录了几个景点」**。
旧口径「前 10 之和」实测 ρ=**+0.858**（一半在排「seed 给了几个名单」），现口径 **+0.714**。
哪天改完 ρ 又冲回 0.85 以上，就说明「基数」问题回来了。

⚠️ ρ **必须用平均秩处理并列**：库里大量目的地挤在同一个景数档
（早先 30 个都是 8 景，`--topup` 补齐后 26 个都是 10 景），若按「序号」当秩，
这批相同值会被排成一条假斜坡，把 ρ 抬到 0.95（这个坑我踩过一次）。见 `avg_ranks()`。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# 本文件在 <repo>/scripts/ 下，backend 才是可导入的包根
BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.core import SessionLocal  # noqa: E402
from app.trip import service  # noqa: E402
from app.trip.models import Attraction, City  # noqa: E402

MUST_HEAT = 500   # 与 planner.MUST_HEAT 保持一致


def load_attractions(db) -> dict[int, list[tuple[int, bool]]]:  # noqa: ANN001
    """一条 SELECT 取全部景点的 (heat, must_visit)，按目的地分组。"""
    by_city: dict[int, list[tuple[int, bool]]] = {}
    for city_id, heat, must in db.execute(
        select(Attraction.city_id, Attraction.heat, Attraction.must_visit)
    ).all():
        by_city.setdefault(int(city_id), []).append((int(heat), bool(must)))
    return by_city


def avg_ranks(vals: dict[str, float]) -> dict[str, float]:
    """平均秩（1-based）：并列的一组取组内平均秩。

    ⚠️ 不处理并列的话，同一个景数档的那批目的地（早先 30 个都是 8 景）
    会被排成 1..N 的假斜坡，和名次的相关系数被抬高到 0.95 这种假数字上。
    """
    items = sorted(vals.items(), key=lambda kv: kv[1])
    out: dict[str, float] = {}
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1][1] == items[i][1]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[items[k][0]] = avg
        i = j + 1
    return out


def pearson(a: dict[str, float], b: dict[str, float]) -> float:
    names = list(a)
    m = len(names)
    ma, mb = sum(a[x] for x in names) / m, sum(b[x] for x in names) / m
    cov = sum((a[x] - ma) * (b[x] - mb) for x in names)
    va = sum((a[x] - ma) ** 2 for x in names) ** 0.5
    vb = sum((b[x] - mb) ** 2 for x in names) ** 0.5
    return cov / (va * vb)


def main() -> int:
    ap = argparse.ArgumentParser(description="首页目的地排序体检（只读）")
    ap.add_argument("--top", type=int, default=68, help="打几行（默认全部）")
    ap.add_argument("--quiet", action="store_true", help="不打明细表，只打结论")
    ap.add_argument("--drill", action="store_true", help="补打「前N均分 / 最高热度」两种口径的名次")
    args = ap.parse_args()

    db = SessionLocal()

    atts = load_attractions(db)
    cities = db.scalars(select(City)).all()
    by_name = {c.name: c for c in cities}
    heats = {cid: [h for h, _ in items] for cid, items in atts.items()}

    # ---------- 自检 ①：存的值 vs 现算的值 ----------
    live = {c.id: service.rank_score(heats.get(c.id, [])) for c in cities}
    stale = [(c, c.rank_score, live[c.id]) for c in cities if c.rank_score != live[c.id]]
    ok_stored = not stale

    # ---------- 自检 ②：接口顺序 vs 按存值重排 ----------
    service.clear_cities_cache()
    api_order = [c.name for c in service.list_cities(db)]
    expect_order = [
        c.name for c in sorted(cities, key=lambda c: (c.rank_score, c.heat, -c.id), reverse=True)
    ]
    ok_order = api_order == expect_order
    rank = {n: i + 1 for i, n in enumerate(api_order)}

    def rank_by(value_of) -> dict[str, int]:  # noqa: ANN001
        return {
            n: i + 1
            for i, n in enumerate(
                sorted(by_name, key=lambda n: (value_of(n), by_name[n].heat, -by_name[n].id),
                       reverse=True)
            )
        }

    # 口径对照：无条件先算好（--quiet --drill 时也要用，别放在打印分支里 —— 踩过 UnboundLocalError）
    avg_of = {
        n: sum(sorted(heats.get(by_name[n].id, []), reverse=True)[: service.RANK_TOP])
        / max(1, min(len(heats.get(by_name[n].id, [])), service.RANK_TOP))
        for n in by_name
    }
    max_of = {n: max(heats.get(by_name[n].id, []) or [0]) for n in by_name}
    rank_avg = rank_by(lambda n: avg_of[n])
    rank_max = rank_by(lambda n: max_of[n])

    # ---------- 明细表 ----------
    if not args.quiet:
        print(f"目的地 {len(cities)} 个   口径：前 {service.RANK_TOP} 个景点的指数加权和"
              f"（λ={service.RANK_DECAY}，第 1 名 ×{service.RANK_FIRST_BOOST}、"
              f"第 2/3 名 ×{service.RANK_TOP3_BOOST}）")
        head = (f"{'#':>3} {'目的地':<14}{'kind':<7}{'景数':>4}{'排序分':>8}{'现算':>8}"
                f"{'前N均':>7}{'最高':>6}{'必去':>4}")
        suffix = f"{'均分名次':>9}{'最高名次':>9}" if args.drill else ""
        print("-" * (96 + len(suffix)))
        print(head + suffix)
        print("-" * (96 + len(suffix)))
        for name in api_order[: args.top]:
            c = by_name[name]
            hs = sorted(heats.get(c.id, []), reverse=True)
            must_n = sum(1 for h, m in atts.get(c.id, []) if m or h >= MUST_HEAT)
            line = (f"{rank[name]:>3} {name:<14}{c.kind:<7}{len(hs):>4}"
                    f"{c.rank_score:>8}{live[c.id]:>8}{round(avg_of[name], 1):>7}"
                    f"{max_of[name]:>6}{must_n:>4}")
            print(line + (f"{rank_avg[name]:>9}{rank_max[name]:>9}" if args.drill else ""))
        print("-" * (96 + len(suffix)))
        print()

    # ---------- 结论 ----------
    print(f"{'✅' if ok_stored else '❌'} 自检① 库里存的排序分 == 现算的值  →  "
          f"{'一致' if ok_stored else f'{len(stale)} 个过期！跑 scripts/rank_cities.py 修'}")
    for c, old, new in stale[:10]:
        print(f"       {c.name:<14} 存 {old} / 现算 {new}")
    print(f"{'✅' if ok_order else '❌'} 自检② 接口顺序 == 按 (排序分, heat, id) 重排  →  "
          f"{'一致' if ok_order else '不一致！'}")
    if not ok_order:
        for i, (a, b) in enumerate(zip(api_order, expect_order)):
            if a != b:
                print(f"       第 {i + 1} 位：接口 {a} / 重排 {b}")
                break

    # ---------- 诊断 ----------
    n_of = {c.name: len(heats.get(c.id, [])) for c in cities}
    # 两个都取负号：avg_ranks 把「最小的值」记成秩 1，
    # 所以 -分数（分数高 → 负得少 → 值小）与 -景数（景数多 → 值小）都变成「好 = 秩小」→ ρ>0 表示越靠前景点越多
    rho = pearson(
        avg_ranks({n: -live[by_name[n].id] for n in n_of}),
        avg_ranks({n: -v for n, v in n_of.items()}),
    )
    dist = Counter(n_of.values())
    under = sum(1 for v in n_of.values() if v < service.RANK_TOP)

    print()
    print(f"📊 ρ(名次, 景点数量) = {rho:+.3f}   ← 越接近 0 越好（旧口径「前10之和」是 +0.858）")
    print(f"   不足 {service.RANK_TOP} 景的目的地：{under}/{len(cities)} 个"
          f"（旧口径 TOP=10 时是 35/68）")
    print("   景数分布：" + "  ".join(f"{k}景:{v}个" for k, v in sorted(dist.items())))

    if args.drill:
        print()
        print("   口径敏感度（前 20 名单重合度，基准 = 当前接口顺序）：")
        top20 = set(api_order[:20])
        for label, rk in (("前N均分", rank_avg), ("最高热度", rank_max)):
            other = {n for n, _ in sorted(rk.items(), key=lambda kv: kv[1])[:20]}
            print(f"     {label:<8} {len(top20 & other):>2}/20"
                  + (f"   新进：{'、'.join(sorted(other - top20))}" if other - top20 else ""))

    db.close()
    return 0 if (ok_stored and ok_order) else 1


if __name__ == "__main__":
    raise SystemExit(main())
