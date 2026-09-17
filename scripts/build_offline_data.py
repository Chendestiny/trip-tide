"""把离线种子导出成前端引擎用的数据模块（**构建期脚本，不参与运行时**）。

    cd backend && venv/Scripts/python ../scripts/build_offline_data.py

产物：`frontend/src/engine/offline-data.json`

为什么要单独生成，而不是前端直接读 `backend/app/trip/data/seed_attractions.json`：

  1. **顺序**。首页宫格是按 `rank_score DESC, heat DESC, id ASC` 排的，而种子 JSON 里
     没有 `rank_score` —— 这里用与 `service.rank_score()` **完全相同的公式**算一遍，
     否则离线版首页的顺序会和联机版不一样。
  2. **id**。种子里没有 id，得给一个稳定编号，否则 `getSpots(id)` / 勾选 / 微调都没法引用。
  3. **投影**。前端只要 `AttractionOut` 那十几个字段，种子里还有 `coord_source` 之类
     用不到的键，剔除后体积小一截。

⚠️ **顺序与口径必须与后端一致**（`list_cities` / `list_attractions` / `service.rank_score`），
   改了后端那三处，这里要跟着改，否则离线版和联机版会给出不同的首页与景点池。
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------- 与 service.py 的 RANK_* 常量**逐字对应** ----------
RANK_TOP = 8
RANK_DECAY = 0.9
RANK_FIRST_BOOST = 1.5
RANK_TOP3_BOOST = 1.2

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "backend" / "app" / "trip" / "data" / "seed_attractions.json"
OUT = ROOT / "frontend" / "src" / "engine" / "offline-data.json"


def rank_score(heats: list[int]) -> int:
    """首页排序分：前 8 个高热度景点的指数加权和（第 1 名 ×1.5、第 2/3 名 ×1.2）。

    与 `backend/app/trip/service.py::rank_score` 是同一套公式，**必须保持一致**。
    """
    total = 0.0
    for i, heat in enumerate(sorted(heats, reverse=True)[:RANK_TOP]):
        weight = RANK_DECAY ** i
        if i == 0:
            weight *= RANK_FIRST_BOOST
        elif i < 3:
            weight *= RANK_TOP3_BOOST
        total += heat * weight
    return round(total)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a)) / 1000.0


def main() -> None:
    raw = json.loads(SEED.read_text(encoding="utf-8"))
    cities_raw = raw["cities"]

    cities: list[dict] = []
    attractions: list[dict] = []
    city_id = 0
    attr_id = 0
    spot_id = 0

    for c in cities_raw:
        city_id += 1
        heats = [int(a.get("heat") or 0) for a in c.get("attractions") or []]
        cities.append({
            "id": city_id,
            "name": c["name"],
            "pinyin": c.get("pinyin") or "",
            "emoji": c.get("emoji") or "",
            "tagline": c.get("tagline") or "",
            "heat": int(c.get("heat") or 0),
            "kind": c.get("kind") or "city",
            "region": c.get("region") or "",
            "center_lat": float(c.get("center_lat") or 0),
            "center_lng": float(c.get("center_lng") or 0),
            "hotel_areas": [
                {
                    "area": h.get("area") or "",
                    "lat": float(h.get("lat") or 0),
                    "lng": float(h.get("lng") or 0),
                    "pros": h.get("pros") or "",
                    "cons": h.get("cons") or "",
                }
                for h in c.get("hotel_areas") or []
            ],
            "rank_score": rank_score(heats),
            "attraction_count": len(c.get("attractions") or []),
        })

        # 与 service.list_attractions 一致：heat 降序、visit_minutes 降序
        ordered = sorted(
            c.get("attractions") or [],
            key=lambda a: (-int(a.get("heat") or 0), -int(a.get("visit_minutes") or 0)),
        )
        for a in ordered:
            attr_id += 1
            spots = []
            for i, s in enumerate(a.get("spots") or [], start=1):
                spot_id += 1
                spots.append({
                    "id": spot_id,
                    "name": s.get("name") or "",
                    "lat": s.get("lat"),
                    "lng": s.get("lng"),
                    "guide": s.get("guide") or "",
                    "order_index": int(s.get("order_index") or i),
                    "stay_minutes": int(s.get("stay_minutes") or 0),
                })
            attractions.append({
                "id": attr_id,
                "city_id": city_id,
                "name": a.get("name") or "",
                "intro": a.get("intro") or "",
                "district": a.get("district") or "",
                "lat": float(a.get("lat") or 0),
                "lng": float(a.get("lng") or 0),
                "visit_minutes": int(a.get("visit_minutes") or 90),
                "heat": int(a.get("heat") or 50),
                "must_visit": bool(a.get("must_visit")),
                "best_time": a.get("best_time") or "",
                "tags": list(a.get("tags") or []),
                "spot_count": len(spots),
                "distance_km": round(
                    haversine_km(
                        float(c.get("center_lat") or 0), float(c.get("center_lng") or 0),
                        float(a.get("lat") or 0), float(a.get("lng") or 0),
                    ),
                    1,
                ),
                "spots": spots,
            })

    # 与 list_cities 的 ORDER BY 一致
    cities.sort(key=lambda x: (-x["rank_score"], -x["heat"], x["id"]))

    OUT.write_text(
        json.dumps(
            {
                "generated_from": "backend/app/trip/data/seed_attractions.json",
                "rank_formula": "top8 exp-weighted, w1=1.5, w2/3=1.2, decay=0.9",
                "cities": cities,
                "attractions": attractions,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    n_spot = sum(len(a["spots"]) for a in attractions)
    size_kb = OUT.stat().st_size / 1024
    print(f"{OUT.relative_to(ROOT)}  {size_kb:.0f} KB")
    print(f"目的地 {len(cities)} · 景点 {len(attractions)} · 子景点 {n_spot}")
    print("首页前 8：" + "、".join(c["name"] for c in cities[:8]))
    for name in ("泰安", "北疆"):
        hit = next((i for i, c in enumerate(cities, 1) if c["name"] == name), None)
        if hit:
            print(f"  {name} 排第 {hit}（对照 scripts/check_rank.py）")


if __name__ == "__main__":
    main()
