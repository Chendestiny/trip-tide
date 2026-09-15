"""纯函数单元测试 —— 不连数据库、不调 LLM、不起服务。

覆盖的是最容易被改坏、又最好隔离验证的那批函数：
分级 / 用餐时长 / 时间预算 / 时段规则 / 地理聚类 / 一键挑景点 / 分天硬校验。

跑法（在 backend 目录下）：
    venv/Scripts/python tests/test_planner.py
    venv/Scripts/python -m unittest tests.test_planner -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.trip import llm, planner  # noqa: E402
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
        cases = [(45, "小"), (90, "小"), (91, "中"), (120, "中"),
                 (180, "中"), (181, "大"), (240, "大"), (300, "大")]
        for minutes, want in cases:
            with self.subTest(minutes=minutes):
                self.assertEqual(planner.grade_attraction(make_att(1, "x", minutes)), want)

    def test_missing_field_defaults_to_small(self):
        self.assertEqual(planner.grade_attraction(SimpleNamespace(name="x")), "小")


class TestIsMust(unittest.TestCase):
    def test_must_visit_or_high_heat(self):
        self.assertTrue(planner._is_must(make_att(1, "a", must=True, heat=10)))
        self.assertTrue(planner._is_must(make_att(2, "b", must=False, heat=90)))
        self.assertFalse(planner._is_must(make_att(3, "c", must=False, heat=89)))


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


class TestDayBudget(unittest.TestCase):
    def test_plain_day(self):
        # 09:00→19:30 = 630 分，扣两餐 135 → 495
        self.assertEqual(planner.day_budget(make_req(), 1, 3), 495)

    def test_last_day_early_return(self):
        # 09:00→16:00 = 420 分，只扣午餐 60 → 360
        req = make_req(last_day_return_time="16:00")
        self.assertEqual(planner.day_budget(req, 3, 3), 360)

    def test_pace_does_not_inflate_budget(self):
        """回归：倾向系数不能再放大时间预算（曾把 420 算成 483，导致最后一天超时）。"""
        req_r = make_req(last_day_return_time="16:00", pace="relaxed")
        req_p = make_req(last_day_return_time="16:00", pace="packed")
        self.assertEqual(planner.day_budget(req_r, 3, 3), 360)
        self.assertEqual(planner.day_budget(req_p, 3, 3), 360)


# ---------------------------------------------------------------- 时段规则
class TestTimeRule(unittest.TestCase):
    def test_field_takes_priority(self):
        rule = planner.time_rule(make_att(1, "某夜景街", best_time="night"))
        self.assertIsNotNone(rule)
        self.assertEqual(rule.kind, "night")
        self.assertEqual(rule.earliest_start, "15:00")

    def test_field_overrides_keyword(self):
        # 名字带「博物馆」但字段说是早场 → 以字段为准
        rule = planner.time_rule(make_att(1, "某博物馆", best_time="morning"))
        self.assertEqual(rule.kind, "morning")

    def test_keyword_fallback_when_field_empty(self):
        self.assertEqual(planner.time_rule(make_att(1, "大熊猫繁育研究基地")).kind, "morning")
        self.assertEqual(planner.time_rule(make_att(2, "成都博物馆")).kind, "museum")
        self.assertEqual(planner.time_rule(make_att(3, "九眼桥酒吧街")).kind, "night")

    def test_no_rule(self):
        self.assertIsNone(planner.time_rule(make_att(1, "普通商业街")))

    def test_invalid_best_time_ignored(self):
        # 非法取值不应被当成规则，回退关键词
        self.assertIsNone(planner.time_rule(make_att(1, "普通街", best_time="whatever")))


# ---------------------------------------------------------------- 地理
class TestTravelMatrix(unittest.TestCase):
    def test_symmetric_and_key_ordered(self):
        city = make_city()
        a = make_att(5, "A", lat=30.660, lng=104.060)
        b = make_att(2, "B", lat=30.700, lng=104.100)
        m = planner.travel_matrix(city, [a, b], "mixed")
        self.assertIn((2, 5), m)          # 键一律 (小 id, 大 id)
        self.assertNotIn((5, 2), m)
        self.assertGreater(m[(2, 5)], 0)


class TestCluster(unittest.TestCase):
    def test_close_pair_joins(self):
        """锦里↔武侯祠 那种「步行可达」必须同簇。"""
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


class TestEstimateDaysNeeded(unittest.TestCase):
    def test_close_pair_is_one_day(self):
        city = make_city()
        a = make_att(1, "武侯祠", lat=30.6450, lng=104.0430)
        b = make_att(2, "锦里古街", lat=30.6440, lng=104.0450)
        self.assertEqual(planner.estimate_days_needed(city, [a, b], "mixed"), 1)

    def test_remote_standalone_takes_own_day(self):
        city = make_city()
        a = make_att(1, "都江堰", minutes=240, lat=31.002, lng=103.617)   # 远郊大景点
        b = make_att(2, "武侯祠", lat=30.6450, lng=104.0430)
        self.assertEqual(planner.estimate_days_needed(city, [a, b], "mixed"), 2)

    def test_empty(self):
        self.assertEqual(planner.estimate_days_needed(make_city(), [], "mixed"), 0)


class TestGroupLoad(unittest.TestCase):
    def test_includes_travel(self):
        """一天的占用量必须含路程 —— 只算游览会严重低估远郊日。"""
        city = make_city()
        a = make_att(1, "市区景点", minutes=90, lat=30.660, lng=104.060)
        load = planner._group_load(city, [a], "mixed")
        self.assertGreater(load, 90)      # 游览 90 + 往返路程


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

    def test_empty_pool(self):
        self.assertEqual(planner.pick_attractions(make_city(), [], 3), [])


# ---------------------------------------------------------------- 分天硬校验
class TestValidateDays(unittest.TestCase):
    """LLM 分天审阅的把关逻辑 —— 不通过就打回，所以它自己必须准。"""

    def setUp(self):
        self.city = make_city()
        self.near_a = make_att(1, "武侯祠", lat=30.6450, lng=104.0430)
        self.near_b = make_att(2, "锦里古街", lat=30.6440, lng=104.0450)
        self.far_c = make_att(3, "杜甫草堂", lat=30.6600, lng=104.0300)
        self.items = [self.near_a, self.near_b, self.far_c]
        self.req = make_req(days=2, attraction_ids=[1, 2, 3])

    def test_valid(self):
        self.assertIsNone(llm._validate_days(self.city, self.req, self.items, [[1, 2], [3]]))

    def test_missing_id(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2], []])
        self.assertIsNotNone(err)

    def test_duplicate_id(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2], [2, 3]])
        self.assertIsNotNone(err)

    def test_wrong_day_count(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2, 3]])
        self.assertIn("天数", err or "")

    def test_empty_day_rejected(self):
        err = llm._validate_days(self.city, self.req, self.items, [[1, 2], []])
        self.assertIsNotNone(err)

    def test_close_pair_split_rejected(self):
        """核心：紧邻景点被拆到不同天必须打回（「地理第一」的硬约束）。"""
        err = llm._validate_days(self.city, self.req, self.items, [[1, 3], [2]])
        self.assertIsNotNone(err)
        self.assertIn("同一天", err or "")

    def test_standalone_not_alone_rejected(self):
        city = make_city()
        remote = make_att(9, "都江堰", minutes=240, lat=31.002, lng=103.617)
        other = make_att(10, "青羊宫", lat=30.6600, lng=104.0300)
        items = [remote, other]
        req = make_req(days=1, attraction_ids=[9, 10])
        err = llm._validate_days(city, req, items, [[9, 10]])
        self.assertIsNotNone(err)
        self.assertIn("独占", err or "")

    def test_over_budget_rejected(self):
        city = make_city()
        # 5 个 300 分钟的大景点塞进 1 天 → 必然超预算
        items = [make_att(i, f"大景点{i}", minutes=300, heat=80,
                          lat=30.65 + i * 0.001, lng=104.06) for i in range(1, 6)]
        req = make_req(days=1, attraction_ids=[1, 2, 3, 4, 5])
        err = llm._validate_days(city, req, items, [[1, 2, 3, 4, 5]])
        self.assertIsNotNone(err)


# ---------------------------------------------------------------- 提示词渲染
class TestPromptRendering(unittest.TestCase):
    def test_matrix_has_all_ids_in_header(self):
        city = make_city()
        items = [make_att(1, "A"), make_att(2, "B"), make_att(3, "C")]
        req = make_req(days=2, attraction_ids=[1, 2, 3])
        text = llm._matrix_text(city, items, req)
        header = text.splitlines()[0]
        for aid in (1, 2, 3):
            self.assertIn(str(aid), header)

    def test_items_text_marks_flags(self):
        city = make_city()
        items = [
            make_att(1, "必去景点", must=True),
            make_att(2, "远郊景点", lat=31.002, lng=103.617),
        ]
        req = make_req(attraction_ids=[1, 2])
        text = llm._items_text(city, items, req)
        self.assertIn("必去", text)
        self.assertIn("远郊", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
