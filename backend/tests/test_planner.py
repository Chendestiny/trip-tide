"""纯函数单元测试 —— 不连数据库、不调 LLM、不起服务。

覆盖 planner.py / llm.py / seed.py 里的纯函数：
分级 / 用餐时长 / 时间预算 / 时段规则 / 地理与矩阵 / 聚类 / 一键挑景点 /
独占判定 / 分天硬校验 / 提示词渲染 /
**子景点攻略拼串（`spot_advice`）/ advice 取值优先级 / `trim_day` 的超载例外 /
机动日两餐 / `chat_json` 的空返回重试 / seed 的地理上下文提示词与坐标校验**。

后 6 项是 2026-09-17 加的 —— 每个都对应一个**实际踩过的坑**（见函数里的 docstring）。

跑法（在 backend 目录下）：
    venv/Scripts/python tests/test_planner.py
    venv/Scripts/python -m unittest tests.test_planner -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.trip import llm, planner, seed, amap  # noqa: E402
from app.core.config import settings  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402
from app.trip.schemas import PlanRequest  # noqa: E402


# ---------------------------------------------------------------- 构造器
def make_city(lat=30.657, lng=104.0657):
    return SimpleNamespace(name="成都", center_lat=lat, center_lng=lng, hotel_areas=[])


def make_att(aid, name, minutes=90, heat=80, lat=30.66, lng=104.06,
             must=False, district="", best_time="", tags=None):
    return SimpleNamespace(
        id=aid, name=name, intro=f"{name}简介", district=district,
        lat=lat, lng=lng, visit_minutes=minutes, heat=heat,
        must_visit=must, tags=tags or [], best_time=best_time,
    )


def make_req(**kw):
    base = dict(
        city="成都", attraction_ids=[1], days=3,
        start_time="09:00", return_time="19:30", transport="mixed", pace="balanced",
    )
    base.update(kw)
    return PlanRequest(**base)


# ---------------------------------------------------------------- 景点分级
class TestGrade(unittest.TestCase):
    def test_boundaries(self):
        """90/91、180/181 是硬边界，必须精确落在两侧。"""
        cases = [(1, "小"), (45, "小"), (90, "小"), (91, "中"), (120, "中"),
                 (180, "中"), (181, "大"), (240, "大"), (600, "大")]
        for minutes, want in cases:
            with self.subTest(minutes=minutes):
                self.assertEqual(planner.grade_attraction(make_att(1, "x", minutes)), want)

    def test_missing_field_defaults_to_small(self):
        self.assertEqual(planner.grade_attraction(SimpleNamespace(name="x")), "小")

    def test_falsy_minutes_treated_as_default(self):
        """`visit_minutes=0` 会被 `or 90` 归到默认值 90 → 小（当前实现的约定）。"""
        self.assertEqual(planner.grade_attraction(
            SimpleNamespace(name="x", visit_minutes=0)), "小")

    def test_none_minutes_defaults_to_small(self):
        self.assertEqual(planner.grade_attraction(
            SimpleNamespace(name="x", visit_minutes=None)), "小")


class TestIsMust(unittest.TestCase):
    def test_must_visit_or_high_heat(self):
        # MUST_HEAT=500：全国 T 级 ≥500（T0/T1）视为必去
        self.assertTrue(planner._is_must(make_att(1, "a", must=True, heat=10)))
        self.assertTrue(planner._is_must(make_att(2, "b", must=False, heat=560)))
        self.assertFalse(planner._is_must(make_att(3, "c", must=False, heat=499)))

    def test_missing_and_falsy_fields(self):
        self.assertFalse(planner._is_must(SimpleNamespace(name="x", must_visit=False, heat=None)))
        self.assertFalse(planner._is_must(SimpleNamespace(name="y", heat=0)))


# ---------------------------------------------------------------- 用餐与预算
class TestMealMinutes(unittest.TestCase):
    def test_late_return_keeps_dinner(self):
        # 19:30 返回：18:05 前吃完还能赶上 → 两餐
        self.assertEqual(planner.meal_minutes("19:30"), 135)
        self.assertEqual(planner.meal_minutes("21:00"), 135)

    def test_early_return_drops_dinner(self):
        # 16:00 返回（赶返程）：吃完晚饭就超时了 → 只留午餐
        self.assertEqual(planner.meal_minutes("16:00"), 60)
        # 18:00 返回：18:00 - 85 = 16:35 < 17:00，也不算晚饭
        self.assertEqual(planner.meal_minutes("18:00"), 60)

    def test_dinner_boundary_is_1825(self):
        """判据 end - (75+10) >= 17:00 → end >= 18:25。18:24/18:25 分属两侧。"""
        self.assertEqual(planner.meal_minutes("18:24"), 60)
        self.assertEqual(planner.meal_minutes("18:25"), 135)
        self.assertEqual(planner.meal_minutes("17:00"), 60)
        self.assertEqual(planner.meal_minutes("18:24"), 60)


class TestDayBudget(unittest.TestCase):
    def test_plain_day(self):
        # 09:00→19:30 = 630 分，扣两餐 135 → 495
        self.assertEqual(planner.day_budget(make_req(), 1, 3), 495)

    def test_last_day_early_return(self):
        # 09:00→16:00 = 420 分，只扣午餐 60 → 360
        req = make_req(last_day_return_time="16:00")
        self.assertEqual(planner.day_budget(req, 3, 3), 360)

    def test_pace_does_not_inflate_budget(self):
        """重点回归①：倾向系数不能再放大时间预算（曾把 420 算成 483，导致最后一天超时）。"""
        for pace in ("relaxed", "balanced", "packed"):
            with self.subTest(pace=pace):
                req = make_req(last_day_return_time="16:00", pace=pace)
                self.assertEqual(planner.day_budget(req, 3, 3), 360)

    def test_budget_floor_180(self):
        """极短时间窗：18:00→18:30 只有 30 分，扣餐后为负，必须被 180 的下限托住。"""
        req = make_req(start_time="18:00", return_time="18:30")
        self.assertEqual(planner.day_budget(req, 1, 1), 180)

    def test_cross_midnight_window(self):
        """跨零点时间窗：09:00 → 01:00 = 960 分。01:00 返回吃不上晚饭 → 只扣午餐 60。"""
        req = make_req(start_time="09:00", return_time="01:00")
        self.assertEqual(planner.day_budget(req, 1, 1), 900)

    def test_last_day_cross_midnight(self):
        req = make_req(start_time="09:00", return_time="19:30",
                       last_day_return_time="01:00")
        # last_day_return_time 只作用于最后一天
        self.assertEqual(planner.day_budget(req, 2, 3), 495)
        self.assertEqual(planner.day_budget(req, 3, 3), 900)

    def test_first_day_start_time_only_day1(self):
        req = make_req(days=2, first_day_start_time="12:00", last_day_return_time="16:00")
        # Day1：12:00→19:30 = 450 分，扣两餐 135 → 315
        self.assertEqual(planner.day_budget(req, 1, 2), 315)
        # Day2：09:00→16:00 = 420 分，扣午餐 60 → 360
        self.assertEqual(planner.day_budget(req, 2, 2), 360)


class TestFmtHhmm(unittest.TestCase):
    def test_wraps_past_24h(self):
        self.assertEqual(planner.fmt_hhmm(1510), "01:10")   # 25:10 → 01:10
        self.assertEqual(planner.fmt_hhmm(540), "09:00")
        self.assertEqual(planner.fmt_hhmm(1439), "23:59")


# ---------------------------------------------------------------- 时段规则
class TestTimeRule(unittest.TestCase):
    def test_field_takes_priority(self):
        for kind, latest, earliest in (
            ("morning", "12:00", ""), ("night", "", "15:00"), ("museum", "15:30", ""),
        ):
            with self.subTest(kind=kind):
                rule = planner.time_rule(make_att(1, "普通街", best_time=kind))
                self.assertIsNotNone(rule)
                self.assertEqual(rule.kind, kind)
                self.assertEqual(rule.latest_start, latest)
                self.assertEqual(rule.earliest_start, earliest)

    def test_field_overrides_keyword(self):
        # 名字带「博物馆」但字段说是早场 → 以字段为准
        self.assertEqual(planner.time_rule(make_att(1, "某博物馆", best_time="morning")).kind,
                         "morning")

    def test_keyword_fallback_when_field_empty(self):
        self.assertEqual(planner.time_rule(make_att(1, "大熊猫繁育研究基地")).kind, "morning")
        self.assertEqual(planner.time_rule(make_att(2, "成都博物馆")).kind, "museum")
        self.assertEqual(planner.time_rule(make_att(3, "九眼桥酒吧街")).kind, "night")

    def test_keyword_matches_tags_too(self):
        att = make_att(1, "某园区", tags=["夜景"])
        self.assertEqual(planner.time_rule(att).kind, "night")

    def test_no_rule(self):
        self.assertIsNone(planner.time_rule(make_att(1, "普通商业街")))

    def test_invalid_best_time_falls_back_to_keyword(self):
        """非法 best_time 忽略 → 回退到名称关键词；无关键词则返回 None。"""
        self.assertEqual(planner.time_rule(make_att(1, "大熊猫基地", best_time="whatever")).kind,
                         "morning")
        self.assertIsNone(planner.time_rule(make_att(2, "普通街", best_time="afternoon")))

    def test_field_is_case_and_space_insensitive(self):
        self.assertEqual(planner.time_rule(make_att(1, "x", best_time=" Night ")).kind, "night")

    def test_missing_best_time_attr(self):
        self.assertEqual(planner.time_rule(SimpleNamespace(name="成都博物馆")).kind, "museum")

    def test_rule_sort_key_and_stable_sort(self):
        a = make_att(1, "夜景街", best_time="night")
        b = make_att(2, "普通街")
        c = make_att(3, "熊猫基地", best_time="morning")
        out = planner.apply_time_rules([b, a, c])
        self.assertEqual([x.id for x in out], [3, 2, 1])  # 早场 → 普通 → 夜景
        self.assertEqual(planner.rule_sort_key(b), 1)


# ---------------------------------------------------------------- 地理
class TestHaversine(unittest.TestCase):
    def test_same_point_zero(self):
        self.assertEqual(planner.haversine_m(30.0, 104.0, 30.0, 104.0), 0.0)

    def test_one_degree_latitude(self):
        # 赤道圈 1° 纬度 ≈ 111.195 km（R=6371km 的球面值）
        d = planner.haversine_m(30.0, 104.0, 31.0, 104.0)
        self.assertAlmostEqual(d, 111194.9, delta=1.0)

    def test_symmetric(self):
        self.assertEqual(
            planner.haversine_m(30.0, 104.0, 31.0, 103.0),
            planner.haversine_m(31.0, 103.0, 30.0, 104.0),
        )


class TestTravelMatrix(unittest.TestCase):
    def test_symmetric_and_key_ordered(self):
        city = make_city()
        a = make_att(5, "A", lat=30.660, lng=104.060)
        b = make_att(2, "B", lat=30.700, lng=104.100)
        m = planner.travel_matrix(city, [a, b], "mixed")
        self.assertIn((2, 5), m)          # 键一律 (小 id, 大 id)
        self.assertNotIn((5, 2), m)       # 不允许反序键
        self.assertGreater(m[(2, 5)], 0)

    def test_three_items_key_count(self):
        city = make_city()
        items = [make_att(3, "A"), make_att(1, "B"), make_att(2, "C")]
        m = planner.travel_matrix(city, items, "mixed")
        self.assertEqual(len(m), 3)       # C(3,1)<... 两两共 3 对
        self.assertEqual(set(m), {(1, 2), (1, 3), (2, 3)})

    def test_single_item_empty_matrix(self):
        self.assertEqual(planner.travel_matrix(make_city(), [make_att(1, "A")], "mixed"), {})

    def test_empty_items(self):
        self.assertEqual(planner.travel_matrix(make_city(), [], "mixed"), {})


class TestCluster(unittest.TestCase):
    def test_close_pair_joins(self):
        """锦里↔武侯祠 那种「步行可达」必须同簇（重点场景④）。"""
        city = make_city()
        a = make_att(1, "武侯祠", lat=30.6450, lng=104.0430)
        b = make_att(2, "锦里古街", lat=30.6440, lng=104.0450)   # 约 220m
        clusters = planner.cluster_attractions(city, [a, b], "mixed")
        self.assertEqual(len(clusters), 1)
        self.assertEqual({x.id for x in clusters[0]}, {1, 2})

    def test_far_pair_splits(self):
        city = make_city()
        a = make_att(1, "市区A", lat=30.660, lng=104.060)
        b = make_att(2, "远郊B", lat=30.900, lng=104.300)         # 约 35km
        clusters = planner.cluster_attractions(city, [a, b], "mixed")
        self.assertEqual(len(clusters), 2)

    def test_empty_list(self):
        self.assertEqual(planner.cluster_attractions(make_city(), [], "mixed"), [])

    def test_single_item(self):
        a = make_att(1, "A")
        clusters = planner.cluster_attractions(make_city(), [a], "mixed")
        self.assertEqual(clusters, [[a]])

    def test_chain_breaks_at_true_gap(self):
        """紧邻成链后按间隔断开：A-B 666m 同簇，B-C 1332m 断开。"""
        city = make_city()
        a = make_att(1, "A", heat=99, lat=30.660, lng=104.060)   # 锚点
        b = make_att(2, "B", heat=50, lat=30.666, lng=104.060)   # ~666m
        c = make_att(3, "C", heat=50, lat=30.678, lng=104.060)   # B-C ~1332m
        clusters = planner.cluster_attractions(city, [a, b, c], "mixed")
        self.assertEqual(len(clusters), 2)
        self.assertEqual({x.id for x in clusters[0]}, {1, 2})
        self.assertEqual([x.id for x in clusters[1]], [3])

    def test_whole_chain_within_threshold_is_one_cluster(self):
        """A-B-C 间隔都 ≤1200m（ Bulgari 状），即使首尾相距 >1200m 也应同簇。"""
        city = make_city()
        a = make_att(1, "A", heat=99, lat=30.660, lng=104.060)
        b = make_att(2, "B", lat=30.668, lng=104.060)   # A-B ~890m
        c = make_att(3, "C", lat=30.662, lng=104.060)   # B-C ~666m（回折，仍同簇）
        clusters = planner.cluster_attractions(city, [a, b, c], "mixed")
        self.assertEqual(len(clusters), 1)

    def test__split_cluster_refuses_tight_cluster(self):
        """全簇都在阈值内串成一片 → 不可拆（返回 None）。"""
        city = make_city()
        a = make_att(1, "A", heat=99, lat=30.660, lng=104.060)
        b = make_att(2, "B", lat=30.665, lng=104.060)
        c = make_att(3, "C", lat=30.670, lng=104.060)
        self.assertIsNone(planner._split_cluster(city, [a, b, c], "mixed"))


class TestEstimateDaysNeeded(unittest.TestCase):
    def test_close_pair_is_one_day(self):
        city = make_city()
        a = make_att(1, "武侯祠", lat=30.6450, lng=104.0430)
        b = make_att(2, "锦里古街", lat=30.6440, lng=104.0450)
        self.assertEqual(planner.estimate_days_needed(city, [a, b], "mixed"), 1)

    def test_remote_standalone_takes_own_day(self):
        """重点场景⑤：都江堰这种远郊/大景点独占一天。"""
        city = make_city()
        a = make_att(1, "都江堰", minutes=240, lat=31.002, lng=103.617)   # 远郊大景点
        b = make_att(2, "武侯祠", lat=30.6450, lng=104.0430)
        self.assertEqual(planner.estimate_days_needed(city, [a, b], "mixed"), 2)

    def test_empty(self):
        self.assertEqual(planner.estimate_days_needed(make_city(), [], "mixed"), 0)

    def test_single_normal_item(self):
        self.assertEqual(planner.estimate_days_needed(
            make_city(), [make_att(1, "x", minutes=60)], "mixed"), 1)

    def test_two_separate_districts(self):
        city = make_city()
        a = make_att(1, "A", lat=30.660, lng=104.060)
        b = make_att(2, "B", lat=30.900, lng=104.300)
        self.assertEqual(planner.estimate_days_needed(city, [a, b], "mixed"), 2)


class TestGroupLoad(unittest.TestCase):
    def test_includes_travel(self):
        """一天的占用量必须含路程 —— 只算游览会严重低估远郊日。"""
        city = make_city()
        a = make_att(1, "市区景点", minutes=90, lat=30.660, lng=104.060)
        load = planner._group_load(city, [a], "mixed")
        self.assertGreater(load, 90)      # 游览 90 + 往返路程

    def test_load_at_city_center_exact(self):
        """景点正落在市中心：路程是两段 5 分钟的短腿 → load = 游览 + 10。"""
        city = make_city()
        a = make_att(1, "x", minutes=90, lat=city.center_lat, lng=city.center_lng)
        self.assertEqual(planner._group_load(city, [a], "mixed"), 90 + 5 + 5)

    def test_empty_group_zero(self):
        self.assertEqual(planner._group_load(make_city(), [], "mixed"), 0)

    def test_multi_item_counts_legs_not_round_trips(self):
        """顺序链：市中心→A→B→市中心，共 3 段路程，不是把 A、B 各自往返。"""
        city = make_city()
        a = make_att(1, "A", minutes=60, lat=30.660, lng=104.060)
        b = make_att(2, "B", minutes=60, lat=30.661, lng=104.061)
        load = planner._group_load(city, [a, b], "mixed")
        # 3 段腿分别取整为 15 / 0(A→B 很近) / 10 分钟 → 120 + 25
        self.assertEqual(load, 145)


# ---------------------------------------------------------------- 独占判定
class TestStandalone(unittest.TestCase):
    def test_by_visit_minutes(self):
        city = make_city()
        self.assertTrue(planner._is_standalone(city, make_att(1, "x", minutes=240), "mixed"))
        self.assertFalse(planner._is_standalone(
            city, make_att(2, "x", minutes=239, lat=city.center_lat, lng=city.center_lng),
            "mixed"))
        self.assertTrue(planner._is_standalone(city, make_att(3, "x", minutes=300), "mixed"))

    def test_by_travel_time(self):
        """游览很短、但单程 ≥45min（超远郊区）也是独占型。"""
        city = make_city()
        far = make_att(1, "远郊", minutes=60, lat=31.30, lng=104.06)  # ~71km
        self.assertTrue(planner._is_standalone(city, far, "mixed"))
        self.assertTrue(planner.is_remote(city, far, "mixed"))


# ---------------------------------------------------------------- 一键 AI
class TestPickAttractions(unittest.TestCase):
    def test_must_visit_first(self):
        city = make_city()
        items = [
            make_att(1, "低热度", minutes=90, heat=50),
            make_att(2, "必去", minutes=90, heat=60, must=True),
            make_att(3, "高热度", minutes=90, heat=99),
        ]
        # 2 天轻松（目标 480 分）能装下 3 个，才轮得到比较先后顺序
        picked = planner.pick_attractions(city, items, days=2, pace="relaxed")
        ids = [a.id for a in picked]
        self.assertIn(2, ids)                          # 必去必须在
        self.assertLess(ids.index(2), ids.index(1))    # 且排在普通景点之前

    def test_target_caps_selection(self):
        """景点多时不能全选 —— 要按「天数 × 节奏目标」收口。"""
        city = make_city()
        items = [make_att(i, f"景点{i}", minutes=90, heat=90 - i) for i in range(1, 12)]
        picked = planner.pick_attractions(city, items, days=1, pace="relaxed")
        self.assertLess(len(picked), len(items))

    def test_respects_target_minutes(self):
        city = make_city()
        items = [make_att(i, f"景点{i}", minutes=120, heat=90 - i) for i in range(1, 20)]
        picked = planner.pick_attractions(city, items, days=2, pace="relaxed")
        total = sum(a.visit_minutes for a in picked)
        # 轻松 2 天目标 480 分，允许超 10%
        self.assertLessEqual(total, 480 * 1.1 + 1)
        self.assertGreater(len(picked), 0)

    def test_exact_cap_boundary(self):
        """收口边界精确值：relaxed 1 天目标 240，超 10% = 264。
        132 分的景点：第 1 个后 acc=132，第 2 个 264 ≤ 264 通过，第 3 个 396 > 264 拦下。"""
        city = make_city()
        items = [make_att(i, f"x{i}", minutes=132, heat=95 - i) for i in range(1, 6)]
        picked = planner.pick_attractions(city, items, days=1, pace="relaxed")
        self.assertEqual(len(picked), 2)
        self.assertEqual([a.id for a in picked], [1, 2])
        self.assertEqual(sum(a.visit_minutes for a in picked), 264)

    def test_pace_changes_how_many(self):
        """同一批 90 分钟景点：packed 一天能到 5 个，relaxed 只有 2 个。"""
        city = make_city()
        items = [make_att(i, f"x{i}", minutes=90, heat=95 - i) for i in range(1, 12)]
        packed = planner.pick_attractions(city, items, days=1, pace="packed")
        relaxed = planner.pick_attractions(city, items, days=1, pace="relaxed")
        self.assertEqual(len(packed), 5)     # 450 目标：5×90=450，第 6 个 540>495
        self.assertEqual(len(relaxed), 2)    # 240 目标：2×90=180，第 3 个 270>264
        self.assertEqual([a.id for a in packed], [1, 2, 3, 4, 5])

    def test_single_oversized_item_still_picked(self):
        """只有一个超目标的景点时也要返回它（不能空手而归）。"""
        city = make_city()
        big = make_att(1, "大景点", minutes=600, heat=99,
                       lat=city.center_lat, lng=city.center_lng)
        picked = planner.pick_attractions(city, [big], days=1, pace="relaxed")
        self.assertEqual([a.id for a in picked], [1])
        self.assertEqual(sum(a.visit_minutes for a in picked), 600)

    def test_unknown_pace_uses_balanced_target(self):
        city = make_city()
        items = [make_att(i, f"x{i}", minutes=90, heat=95 - i) for i in range(1, 12)]
        picked = planner.pick_attractions(city, items, days=1, pace="chaotic")
        self.assertEqual(len(picked), 4)     # balanced 目标 360：第 4 个后 acc=360 ≤ 396，第 5 个 450 > 396

    def test_empty_pool(self):
        self.assertEqual(planner.pick_attractions(make_city(), [], 3), [])

    def test_non_positive_days(self):
        self.assertEqual(planner.pick_attractions(make_city(), [make_att(1, "x")], 0), [])
        self.assertEqual(planner.pick_attractions(make_city(), [make_att(1, "x")], -1), [])


# ---------------------------------------------------------------- 分天硬校验（llm.py）
class TestValidateDays(unittest.TestCase):
    """LLM 分天审阅的把关逻辑 —— 不通过就打回，所以它自己必须准。五条规则各自一例。"""

    def setUp(self):
        self.city = make_city()
        self.near_a = make_att(1, "武侯祠", lat=30.6450, lng=104.0430)
        self.near_b = make_att(2, "锦里古街", lat=30.6440, lng=104.0450)
        self.far_c = make_att(3, "杜甫草堂", lat=30.6600, lng=104.0300)
        self.items = [self.near_a, self.near_b, self.far_c]
        self.req = make_req(days=2, attraction_ids=[1, 2, 3])

    def test_valid(self):
        self.assertIsNone(llm._validate_days(self.city, self.req, self.items, [[1, 2], [3]]))

    def test_rule1_unknown_id_rejected(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2], [3, 99]])
        self.assertIsNotNone(err)
        self.assertIn("不重不漏", err or "")

    def test_rule1_missing_id_rejected(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2], []])
        self.assertIsNotNone(err)

    def test_rule1_duplicate_id_rejected(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2], [2, 3]])
        self.assertIsNotNone(err)

    def test_rule2_wrong_day_count(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2, 3]])
        self.assertIn("天数", err or "")
        err3 = llm._validate_days(self.city, self.req, self.items, [[1], [2], [3]])
        self.assertIn("天数", err3 or "")

    def test_rule3_empty_day_rejected(self):
        """3 天只排 1 天内容 → 两天空天，文案含「空天」。"""
        req = make_req(days=3, attraction_ids=[1, 2, 3])
        err = llm._validate_days(self.city, req, self.items, [[1, 2, 3], [], []])
        self.assertIn("空天", err or "")

    def test_rule4_close_pair_split_rejected(self):
        """重点场景⑥：紧邻景点被拆到不同天必须打回，文案含「同一天」。"""
        err = llm._validate_days(self.city, self.req, self.items, [[1, 3], [2]])
        self.assertIsNotNone(err)
        self.assertIn("同一天", err or "")

    def test_rule4_boundary_1200m(self):
        """阈值边界：~1190m 拆开也违规，~1.3km 拆开放行。"""
        city = make_city()
        p = make_att(1, "P", lat=30.660, lng=104.060)
        q1190 = make_att(2, "Q1", lat=30.6707, lng=104.060)   # ~1190m
        r1300 = make_att(3, "Q2", lat=30.6718, lng=104.060)   # ~1308m
        req = make_req(days=2, attraction_ids=[1, 2, 3])
        self.assertIsNotNone(llm._validate_days(city, req, [p, q1190], [[p.id], [q1190.id]]))
        self.assertIsNone(llm._validate_days(city, req, [p, r1300], [[p.id], [r1300.id]]))

    def test_rule5_standalone_not_alone_rejected(self):
        city = make_city()
        remote = make_att(9, "都江堰", minutes=240, lat=31.002, lng=103.617)
        other = make_att(10, "青羊宫", lat=30.6600, lng=104.0300)
        items = [remote, other]
        req = make_req(days=1, attraction_ids=[9, 10])
        err = llm._validate_days(city, req, items, [[9, 10]])
        self.assertIsNotNone(err)
        self.assertIn("独占", err or "")

    def test_rule5_standalone_alone_ok(self):
        city = make_city()
        remote = make_att(9, "都江堰", minutes=240, lat=31.002, lng=103.617)
        req = make_req(days=1, attraction_ids=[9])
        self.assertIsNone(llm._validate_days(city, req, [remote], [[9]]))

    def test_rule5_over_budget_rejected(self):
        """游览 510 + 路程 10 ≈ 520 > int(495 × 1.05) → 超预算。注意别用 ≥240 分的
        景点来造超载——那会先触发「独占」规则而不是预算规则（两例各自单独覆盖）。"""
        city = make_city()
        items = [make_att(i, f"大景点{i}", minutes=m, heat=80,
                          lat=city.center_lat, lng=city.center_lng)
                 for i, m in enumerate((200, 200, 110), start=1)]
        req = make_req(days=1, attraction_ids=[1, 2, 3])
        err = llm._validate_days(city, req, items, [[1, 2, 3]])
        self.assertIsNotNone(err)
        self.assertIn("预算", err or "")

    def test_rule5_budget_tolerates_5pct(self):
        """预算上限是 budget × 1.05：load 落在 (budget, 1.05×budget] 应放行，再高才打回。"""
        city = make_city()
        req = make_req(days=1, attraction_ids=[1, 2, 3], start_time="09:00", return_time="19:30")
        # 09:00→19:30 budget = 630 - 135 = 495；×1.05 = 519.75 → 阈值 519
        items_ok = [make_att(1, "A", minutes=200, lat=city.center_lat, lng=city.center_lng),
                    make_att(2, "B", minutes=200, lat=city.center_lat, lng=city.center_lng),
                    make_att(3, "C", minutes=95, lat=city.center_lat, lng=city.center_lng)]
        # load = 495 游览 + 10 路程 = 505 ≤ 519 → 放行
        self.assertIsNone(llm._validate_days(city, req, items_ok, [[1, 2, 3]]))
        items_bad = items_ok + [make_att(4, "D", minutes=15, lat=city.center_lat, lng=city.center_lng)]
        # load = 510 + 10 = 520 > 519 → 打回
        err = llm._validate_days(city, req, items_bad, [[1, 2, 3, 4]])
        self.assertIsNotNone(err)
        self.assertIn("预算", err or "")


# ---------------------------------------------------------------- 提示词渲染（llm.py）
class TestPromptRendering(unittest.TestCase):
    def test_matrix_has_all_ids_in_header(self):
        city = make_city()
        items = [make_att(1, "A"), make_att(2, "B"), make_att(3, "C")]
        req = make_req(days=2, attraction_ids=[1, 2, 3])
        text = llm._matrix_text(city, items, req)
        header = text.splitlines()[0]
        for aid in (1, 2, 3):
            self.assertIn(str(aid), header)
        # 每个 id 也应各有一行
        self.assertEqual(len(text.splitlines()), 1 + len(items))

    def test_matrix_single_item(self):
        city = make_city()
        text = llm._matrix_text(city, [make_att(7, "A")], make_req(attraction_ids=[7]))
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("7", lines[0])
        self.assertIn("-", lines[1])

    def test_items_text_marks_flags(self):
        city = make_city()
        items = [
            make_att(1, "必去景点", must=True, minutes=60),
            make_att(2, "远郊景点", minutes=120, lat=31.002, lng=103.617),
            make_att(3, "普通景点", minutes=120),
        ]
        req = make_req(days=1, attraction_ids=[1, 2, 3])
        text = llm._items_text(city, items, req)
        self.assertIn("必去", text)
        self.assertIn("远郊", text)
        self.assertIn("小景点", text)      # 必去景点 60 分 → 小
        self.assertIn("中景点", text)      # 120 分 → 中
        self.assertIn("普通", text)        # 无标记的兜底字样


# ---------------------------------------------------------------- 子景点攻略（2026-09-17 新增）
def make_spot(name, guide, order=1, stay=30):
    return SimpleNamespace(name=name, guide=guide, order_index=order,
                           stay_minutes=stay, id=order)


class TestSpotAdvice(unittest.TestCase):
    """`spot_advice` —— 用预存的子景点攻略硬编码拼 advice（不花 token）。

    825 个景点里 603 个（73%）有子景点，所以这条路径是 advice 的主来源。
    """

    def test_joins_guides_in_order(self):
        a = make_att(1, "大熊猫基地", minutes=180)
        a.spots = [
            make_spot("月亮产房", "开园直奔这里", order=1),
            make_spot("太阳产房", "与月亮产房同片", order=2),
        ]
        text = planner.spot_advice(a)
        self.assertIn("月亮产房：开园直奔这里", text)
        self.assertIn("太阳产房：与月亮产房同片", text)
        self.assertLess(text.index("月亮产房"), text.index("太阳产房"))

    def test_sorts_by_order_index_not_insertion(self):
        a = make_att(1, "乱序景点", minutes=90)
        a.spots = [
            make_spot("第三个", "三", order=3),
            make_spot("第一个", "一", order=1),
            make_spot("第二个", "二", order=2),
        ]
        text = planner.spot_advice(a)
        self.assertLess(text.index("第一个"), text.index("第二个"))
        self.assertLess(text.index("第二个"), text.index("第三个"))

    def test_empty_when_no_spots(self):
        self.assertEqual(planner.spot_advice(make_att(1, "单点小景点")), "")

    def test_missing_spots_attr_is_safe(self):
        """`make_att` 没有 spots 属性 —— 不能抛 AttributeError。"""
        a = make_att(1, "没有 spots 字段")
        self.assertFalse(hasattr(a, "spots"))
        self.assertEqual(planner.spot_advice(a), "")

    def test_skips_blank_guides(self):
        a = make_att(1, "有空攻略的景点")
        a.spots = [make_spot("空点位", "   ", order=1), make_spot("有点位", "有攻略", order=2)]
        text = planner.spot_advice(a)
        self.assertNotIn("空点位", text)
        self.assertIn("有点位", text)

    def test_truncates_at_limit_without_cutting_a_piece(self):
        """超长时截断到上限，且**不拼出半句** —— 超时停在完整点位边界。"""
        a = make_att(1, "子景点很多的景区")
        a.spots = [make_spot(f"点位{i}", "很长的一段攻略" * 6, order=i) for i in range(1, 10)]
        text = planner.spot_advice(a)
        self.assertLessEqual(len(text), planner.SPOT_ADVICE_MAX)
        # 截断处不能是半个点位名（每个拼接块都是「名字：攻略」）
        self.assertTrue(text.endswith("。") or "：" in text)

    def test_strips_trailing_period_of_each_guide(self):
        a = make_att(1, "景点")
        a.spots = [make_spot("一号", "从东门进人最少。", order=1)]
        self.assertNotIn("。。", planner.spot_advice(a))


class TestMaterializeAdvicePriority(unittest.TestCase):
    """advice 的取值优先级：**子景点拼串 > LLM 写的 > 库里的 intro**。"""

    def _attraction_node(self, dp):
        return next(n for n in dp.nodes if n.type == "attraction")

    def test_spot_advice_wins_over_llm(self):
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        a = make_att(1, "有子景点的景点", minutes=120)
        a.spots = [make_spot("一号点位", "从东门进人最少", order=1)]
        dp = planner.materialize_day(city, 1, [a], req, total_days=1,
                                     advice={1: "LLM 写的建议"})
        node = self._attraction_node(dp)
        self.assertIn("一号点位", node.advice)
        self.assertNotIn("LLM 写的建议", node.advice)

    def test_falls_back_to_llm_advice(self):
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        a = make_att(1, "无子景点的景点", minutes=120)   # 没有 spots
        dp = planner.materialize_day(city, 1, [a], req, total_days=1,
                                     advice={1: "LLM 写的建议"})
        self.assertEqual(self._attraction_node(dp).advice, "LLM 写的建议")

    def test_falls_back_to_intro(self):
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        a = make_att(1, "无子景点的景点", minutes=120)
        dp = planner.materialize_day(city, 1, [a], req, total_days=1)
        self.assertEqual(self._attraction_node(dp).advice, a.intro)

    def test_empty_llm_advice_does_not_win(self):
        """模型偶尔返回空字符串 —— 不能因此把 advice 清空。"""
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        a = make_att(1, "景点", minutes=120)
        dp = planner.materialize_day(city, 1, [a], req, total_days=1, advice={1: "   "})
        self.assertEqual(self._attraction_node(dp).advice, a.intro)


class TestTrimDayOverpack(unittest.TestCase):
    """`trim_day` 的 OVERPACK_RATIO 例外。

    「第一个景点无条件保留」是为了保证每天不空，但**转场日**要开例外 ——
    踩过：南疆「塔什库尔干 → 库车」628km 的转场日硬塞了一个 240 分钟的大峡谷，
    结果午餐 16:30、晚餐 00:00、凌晨 1:25 才入住。
    """

    def _round_trip_cost(self, city, item, transport="mixed"):
        one_way = planner.leg(city, city.center_lat, city.center_lng,
                              item.lat, item.lng, transport).minutes
        return one_way + item.visit_minutes + one_way

    def test_single_item_far_over_budget_is_dropped(self):
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        far = make_att(1, "很远的大峡谷", minutes=240, lat=33.0, lng=104.0)
        cost = self._round_trip_cost(city, far)
        budget = int(cost / planner.OVERPACK_RATIO) - 10      # 确保 cost > budget × 1.5
        kept, overflow = planner.trim_day(city, [far], req, budget)
        self.assertEqual(kept, [])
        self.assertEqual(overflow, [far])

    def test_single_item_slightly_over_budget_is_kept(self):
        """只超一点时仍然保留 —— 不能让一整天空着。"""
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        far = make_att(1, "稍远的景点", minutes=240, lat=33.0, lng=104.0)
        cost = self._round_trip_cost(city, far)
        budget = int(cost / 1.2)                              # cost < budget × 1.5
        kept, overflow = planner.trim_day(city, [far], req, budget)
        self.assertEqual(kept, [far])
        self.assertEqual(overflow, [])

    def test_second_item_overflows_as_before(self):
        """原有行为不变：已排了东西之后超预算 → 溢出。"""
        city, req = make_city(), make_req(days=1, attraction_ids=[1, 2])
        a = make_att(1, "第一个", minutes=60, lat=30.66, lng=104.06)
        b = make_att(2, "第二个", minutes=300, lat=33.0, lng=104.0)
        budget = self._round_trip_cost(city, a) + 10          # 只装得下第一个
        kept, overflow = planner.trim_day(city, [a, b], req, budget)
        self.assertEqual([x.name for x in kept], ["第一个"])
        self.assertEqual([x.name for x in overflow], ["第二个"])


class TestEmptyDayHasBothMeals(unittest.TestCase):
    """机动日必须有**午 + 晚**两餐。

    踩过：`materialize_day([])` 只产晚餐，而 `plan_fallback` 的机动日走的是它 ——
    两条路径不一致，破坏了「每天恰好 1 午 1 晚」这条不变量。
    机动日必须走 `empty_day()`。
    """

    def test_empty_day_has_both_meals(self):
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        hotel = planner.pick_hotel(city, [])
        dp = planner.empty_day(city, req, 1, hotel)
        meals = [n for n in dp.nodes if n.type == "meal"]
        self.assertEqual(len([n for n in meals if "午餐" in n.name]), 1)
        self.assertEqual(len([n for n in meals if "晚餐" in n.name]), 1)

    def test_empty_day_timeline_is_consistent(self):
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        dp = planner.empty_day(city, req, 1, planner.pick_hotel(city, []))
        for prev, cur in zip(dp.nodes, dp.nodes[1:]):
            h, m = map(int, prev.time.split(":"))
            got = (h * 60 + m + prev.stay_minutes + cur.travel_minutes) % (24 * 60)
            self.assertEqual(f"{got // 60:02d}:{got % 60:02d}", cur.time)

    def test_materialize_empty_has_no_lunch_hence_the_rule(self):
        """把「为什么机动日不能走 materialize_day([])」钉成测试。"""
        city, req = make_city(), make_req(days=1, attraction_ids=[1])
        dp = planner.materialize_day(city, 1, [], req, total_days=1)
        meals = [n for n in dp.nodes if n.type == "meal"]
        self.assertEqual(len([n for n in meals if "午餐" in n.name]), 0)


class TestChatJsonRetry(unittest.TestCase):
    """`chat_json` 拿到空 content 时**加倍预算重试一次**。

    deepseek-flash 是混合思考模型，思考内容也计入 max_tokens；预算被吃光时返回的是
    **空 content**（`finish_reason=length`），报错却是「模型返回内容为空」。
    """

    @staticmethod
    def _resp(content, reasoning="", finish="stop"):
        return SimpleNamespace(content=content, reasoning=reasoning, finish_reason=finish)

    def test_no_retry_when_content_present(self):
        calls = []
        def fake(messages, **kw):
            calls.append(kw["max_tokens"])
            return self._resp('{"ok": 1}')
        with patch.object(llm.llm, "chat", side_effect=fake):
            self.assertEqual(llm.chat_json([{"role": "user", "content": "json"}]), '{"ok": 1}')
        self.assertEqual(calls, [8192])

    def test_retries_with_doubled_budget(self):
        calls = []
        def fake(messages, **kw):
            calls.append(kw["max_tokens"])
            if len(calls) == 1:
                return self._resp("", reasoning="想" * 100, finish="length")
            return self._resp('{"ok": 1}')
        with patch.object(llm.llm, "chat", side_effect=fake):
            self.assertEqual(llm.chat_json([{"role": "user", "content": "json"}]), '{"ok": 1}')
        self.assertEqual(calls, [8192, 16384])

    def test_budget_is_capped_at_ceiling(self):
        calls = []
        def fake(messages, **kw):
            calls.append(kw["max_tokens"])
            return self._resp("")
        with patch.object(llm.llm, "chat", side_effect=fake):
            with self.assertRaises(llm.LLMError):
                llm.chat_json([{"role": "user", "content": "json"}], max_tokens=16000)
        self.assertEqual(calls, [16000, llm.MAX_TOKENS_CEILING])

    def test_error_message_points_at_the_real_cause(self):
        def fake(messages, **kw):
            return self._resp("", reasoning="想" * 50, finish="length")
        with patch.object(llm.llm, "chat", side_effect=fake):
            with self.assertRaises(llm.LLMError) as ctx:
                llm.chat_json([{"role": "user", "content": "json"}])
        self.assertIn("finish_reason=length", str(ctx.exception))
        self.assertIn("调大 max_tokens", str(ctx.exception))


class TestSeedHints(unittest.TestCase):
    """seed 的两段地理上下文提示词 + 坐标合理性校验。

    ⚠️ 这两条都是踩过坑才加的：
      · `_bases_hint`   —— 不说清过夜基地，「西疆」会被当成整个新疆（名单与北疆整份重复）
      · `_too_far`      —— region 的名字不是高德认得的城市名，citylimit 失效 → 全国匹配
                           （「五彩滩」搜到了广西涠洲岛那个，3500km 外）
    """

    def test_bases_hint_lists_bases(self):
        text = seed._bases_hint("西疆", [{"area": "伊宁"}, {"area": "赛里木湖"}])
        self.assertIn("过夜基地", text)
        self.assertIn("伊宁", text)
        self.assertIn("赛里木湖", text)

    def test_bases_hint_empty_for_city(self):
        self.assertEqual(seed._bases_hint("成都", None), "")
        self.assertEqual(seed._bases_hint("成都", []), "")
        self.assertEqual(seed._bases_hint("成都", [{"lat": 1}]), "")   # 没有 area

    def test_too_far_rejects_nationwide_match(self):
        beijiang = SimpleNamespace(
            name="北疆", center_lat=47.84, center_lng=88.14,
            hotel_areas=[{"area": "布尔津", "lat": 47.70, "lng": 86.87}],
        )
        # 广西涠洲岛那个「五彩滩」—— 必须拒绝
        self.assertTrue(seed._too_far(beijiang, "五彩滩", 21.03, 109.10))
        # 布尔津附近那个 —— 正常保留
        self.assertFalse(seed._too_far(beijiang, "五彩滩", 47.75, 86.90))

    def test_too_far_falls_back_to_center_when_no_bases(self):
        chengdu = SimpleNamespace(name="成都", center_lat=30.657, center_lng=104.0657,
                                  hotel_areas=[])
        self.assertFalse(seed._too_far(chengdu, "人民公园", 30.66, 104.06))
        # 北京也有个「人民公园」—— 400km 外，拒绝
        self.assertTrue(seed._too_far(chengdu, "人民公园", 39.90, 116.40))

    def test_too_far_uses_nearest_base_not_the_first(self):
        """按「最近的那个落脚点」判，不是第一个 —— region 的基地可以相距上千公里。"""
        heilongjiang = SimpleNamespace(
            name="黑龙江雪乡·漠河", center_lat=44.55, center_lng=129.63,
            hotel_areas=[
                {"area": "牡丹江", "lat": 44.55, "lng": 129.63},
                {"area": "漠河", "lat": 52.97, "lng": 122.54},     # 离牡丹江 1125km
            ],
        )
        # 漠河附近 → 虽然离第一个基地很远，但离漠河基地很近 → 不该拒
        self.assertFalse(seed._too_far(heilongjiang, "北极村", 53.47, 122.36))



# ---------------------------------------------------------------- amap 双熔断 + 地理编码主路径（2026-09-17 新增）
def _amap_resp(body: dict, status_code: int = 200):
    """构造一个 mock 的 httpx 响应：status_code + json() + raise_for_status。"""
    r = SimpleNamespace(
        status_code=status_code,
        json=lambda: body,
    )
    def raise_for_status():
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")
    r.raise_for_status = raise_for_status
    return r


class TestAmapQuotaClassifier(unittest.TestCase):
    """`is_quota_error` / `is_qps_error` —— 错误码分类的纯函数。"""

    def test_quota_infocodes(self):
        for code in ("10003", "10044", "40000"):
            self.assertTrue(amap.is_quota_error(infocode=code), code)
            self.assertFalse(amap.is_qps_error(infocode=code), code)

    def test_qps_infocodes(self):
        for code in ("10004", "10014", "10015", "10019", "10020", "10021"):
            self.assertTrue(amap.is_qps_error(infocode=code), code)
            self.assertFalse(amap.is_quota_error(infocode=code), code)

    def test_quota_hints_via_info(self):
        self.assertTrue(amap.is_quota_error(info="DAILY_QUERY_OVER_LIMIT"))
        self.assertTrue(amap.is_quota_error(info="USER_DAILY_QUERY_OVER_LIMIT"))
        self.assertTrue(amap.is_quota_error(info="QUOTA_PLAN_RUN_OUT"))
        self.assertTrue(amap.is_quota_error(info="CUQPS_HAS_EXCEEDED_THE_LIMIT"))
        self.assertTrue(amap.is_quota_error(info="访问已超出"))
        self.assertTrue(amap.is_quota_error(info="余额耗尽"))

    def test_qps_hints_via_info(self):
        self.assertTrue(amap.is_qps_error(info="CUQPS_HAS_EXCEEDED_THE_LIMIT"))
        self.assertTrue(amap.is_qps_error(info="访问过于频繁"))

    def test_unrelated_codes_are_neither(self):
        self.assertFalse(amap.is_quota_error(status="1", infocode="10000", info="OK"))
        self.assertFalse(amap.is_qps_error(status="1", infocode="10000", info="OK"))
        self.assertFalse(amap.is_quota_error(status="0", infocode="20000", info="INVALID_PARAMS"))
        self.assertFalse(amap.is_quota_error(status="0", infocode="10001", info="INVALID_USER_KEY"))

    def test_quota_hint_overlaps_with_qps_keyword(self):
        """"CUQPS" 同时含 "CUQPS" 和 "QPS" —— 两边都会识别为 True。

        ⚠️ 不归为"bug"：调 QPS 的重试逻辑发现 info 关键字包含额度字样，
        会进一步升级成熔断；调额度的反过来不会触发 QPS 重试。
        两者不互斥。测试钉住这个行为，免得以后被误改成互斥。
        """
        self.assertTrue(amap.is_quota_error(info="CUQPS_HAS_EXCEEDED_THE_LIMIT"))
        self.assertTrue(amap.is_qps_error(info="CUQPS_HAS_EXCEEDED_THE_LIMIT"))


class TestAmapDoubleBreaker(unittest.TestCase):
    """两个熔断（geo / poi）必须独立 —— 一边断了另一边还能用。"""

    def setUp(self):
        amap.reset_quota_breakers()

    def tearDown(self):
        amap.reset_quota_breakers()

    def test_tripping_poi_does_not_block_geo(self):
        """POI 熔断 → 地理编码仍可用。踩过：原版单熔断，POI 用完会把地理编码也停了。"""
        amap._set_quota_breaker("poi")
        self.assertTrue(amap.quota_gone())
        self.assertTrue(amap.quota_gone_poi())
        self.assertFalse(amap.quota_gone_geo())

    def test_tripping_geo_does_not_block_poi(self):
        """地理编码熔断 → POI 仍可用（虽然只剩 5000/月）。"""
        amap._set_quota_breaker("geo")
        self.assertTrue(amap.quota_gone())
        self.assertTrue(amap.quota_gone_geo())
        self.assertFalse(amap.quota_gone_poi())

    def test_reset_clears_both(self):
        amap._set_quota_breaker("geo")
        amap._set_quota_breaker("poi")
        amap.reset_quota_breakers()
        self.assertFalse(amap.quota_gone())

    def test_quota_gone_returns_true_when_either_is_tripped(self):
        amap._set_quota_breaker("geo")
        self.assertTrue(amap.quota_gone())


class TestAmapSearchAddress(unittest.TestCase):
    """`search_address` —— 地理编码（15 万/月 主路径）。"""

    def setUp(self):
        amap.reset_quota_breakers()
        self._orig_client = amap._client
        amap._client = SimpleNamespace(get=MagicMock())

    def tearDown(self):
        amap._client = self._orig_client
        amap.reset_quota_breakers()

    def test_happy_path_returns_lat_lng_district(self):
        amap._client.get.return_value = _amap_resp({
            "status": "1", "info": "OK", "count": "1",
            "geocodes": [{
                "location": "104.138176,30.740573",
                "district": "成华区",
                "level": "兴趣点",
                "formatted_address": "四川省成都市成华区...",
            }],
        })
        hit = amap.search_address("成都大熊猫繁育研究基地", "成都")
        self.assertEqual(hit, (30.740573, 104.138176, "成华区"))
        # 验证走的是 geocode/geo，不是 place/text
        called_url = amap._client.get.call_args[0][0]
        self.assertIn("/v3/geocode/geo", called_url)
        params = amap._client.get.call_args[1]["params"]
        self.assertEqual(params["address"], "成都大熊猫繁育研究基地")
        self.assertEqual(params["city"], "成都")

    def test_zero_results_returns_none(self):
        """count=0 时高德返回 geocodes=0（数字）—— 必须不崩，认作「没找到」。"""
        amap._client.get.return_value = _amap_resp({
            "status": "1", "info": "OK", "count": "0",
            "geocodes": 0,
        })
        self.assertIsNone(amap.search_address("不存在的景点XYZ", "成都"))

    def test_quota_error_trips_geo_breaker(self):
        amap._client.get.return_value = _amap_resp({
            "status": "0", "info": "USER_DAILY_QUERY_OVER_LIMIT", "infocode": "10044",
            "geocodes": [],
        })
        with self.assertRaises(amap.AmapQuotaExceeded) as ctx:
            amap.search_address("任意", "成都")
        self.assertIn("地理编码", str(ctx.exception))
        self.assertTrue(amap.quota_gone_geo())
        self.assertFalse(amap.quota_gone_poi())

    def test_after_geo_breaker_skips_http(self):
        """熔断后不打网络 —— 否则会一直消耗 fail 重试的额度。"""
        amap._set_quota_breaker("geo")
        with self.assertRaises(amap.AmapQuotaExceeded):
            amap.search_address("任意", "成都")
        amap._client.get.assert_not_called()

    def test_prefers_poi_level_over_other_levels(self):
        """多个结果里优先选 level=兴趣点；只有非 POI 时才回退。"""
        amap._client.get.return_value = _amap_resp({
            "status": "1", "info": "OK", "count": "2",
            "geocodes": [
                {"location": "104.000000,30.000000", "district": "区县匹配的那个", "level": "区县"},
                {"location": "104.138176,30.740573", "district": "成华区", "level": "兴趣点"},
            ],
        })
        hit = amap.search_address("成都大熊猫繁育研究基地", "成都")
        self.assertEqual(hit[2], "成华区")              # POI 级优先
        self.assertAlmostEqual(hit[0], 30.740573)

    def test_returns_none_when_no_key_configured(self):
        # settings.has_amap 是 pydantic property（只读），改 amap_key 字段就够
        with patch.object(settings, "amap_key", ""):
            self.assertIsNone(amap.search_address("任意", "成都"))
        amap._client.get.assert_not_called()


class TestAmapSearchPoioverGeo(unittest.TestCase):
    """`search_poi` 内部必须先打地理编码、失败才回退 POI。"""

    def setUp(self):
        amap.reset_quota_breakers()
        self._orig_client = amap._client
        amap._client = SimpleNamespace(get=MagicMock())

    def tearDown(self):
        amap._client = self._orig_client
        amap.reset_quota_breakers()

    def test_default_path_uses_geocoding(self):
        amap._client.get.return_value = _amap_resp({
            "status": "1", "info": "OK", "count": "1",
            "geocodes": [{
                "location": "104.138176,30.740573",
                "district": "成华区", "level": "兴趣点",
            }],
        })
        hit = amap.search_poi("成都大熊猫繁育研究基地", "成都")
        self.assertEqual(hit, (30.740573, 104.138176, "成华区"))
        called_url = amap._client.get.call_args[0][0]
        self.assertIn("/v3/geocode/geo", called_url)
        # 只能打了一次（geo 直接命中，没走 POI）
        self.assertEqual(amap._client.get.call_count, 1)

    def test_falls_back_to_poi_when_geo_misses(self):
        """地理编码没命中 → 兜底走 POI 搜索。"""
        # 第一次：地理编码返回空；第二次：POI 搜索返回结果
        responses = [
            _amap_resp({"status": "1", "info": "OK", "count": "0", "geocodes": 0}),
            _amap_resp({
                "status": "1", "info": "OK", "count": "1",
                "pois": [{"name": "成都大熊猫基地", "location": "104.138,30.740",
                          "adname": "成华区"}],
            }),
        ]
        amap._client.get.side_effect = responses
        hit = amap.search_poi("成都大熊猫繁育研究基地", "成都")
        self.assertEqual(hit[:2], (30.740, 104.138))
        self.assertEqual(amap._client.get.call_count, 2)
        urls = [c[0][0] for c in amap._client.get.call_args_list]
        self.assertTrue(any("/v3/geocode/geo" in u for u in urls))
        self.assertTrue(any("/v3/place/text" in u for u in urls))

    def test_geo_quota_exhausted_falls_back_to_poi(self):
        """地理编码熔断了 → search_poi 还能走 POI（虽然只剩 5000/月）。"""
        amap._set_quota_breaker("geo")
        amap._client.get.return_value = _amap_resp({
            "status": "1", "info": "OK", "count": "1",
            "pois": [{"name": "成都大熊猫基地", "location": "104.138,30.740",
                      "adname": "成华区"}],
        })
        hit = amap.search_poi("成都大熊猫繁育研究基地", "成都")
        self.assertEqual(hit[:2], (30.740, 104.138))
        called_url = amap._client.get.call_args[0][0]
        self.assertIn("/v3/place/text", called_url)

    def test_poi_quota_exhausted_does_not_fall_back_anywhere(self):
        """两边都熔断 → search_poi 直接返回 None（不会再发请求）。"""
        amap._set_quota_breaker("geo")
        amap._set_quota_breaker("poi")
        self.assertIsNone(amap.search_poi("成都大熊猫繁育研究基地", "成都"))
        amap._client.get.assert_not_called()

    def test_returns_none_when_no_key_configured(self):
        with patch.object(settings, "amap_key", ""):
            self.assertIsNone(amap.search_poi("任意", "成都"))
        amap._client.get.assert_not_called()



if __name__ == "__main__":
    unittest.main(verbosity=2)
