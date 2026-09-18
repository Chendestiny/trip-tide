// 排程引擎 · 数值语义 + 地理/交通 + 判定规则 的单元测试（node --test，零依赖）
//
// 跑法（任选）：
//   node --test frontend/tests/
//   cd frontend && npm test
//
// 姊妹篇：backend/tests/test_planner.py（Python 侧 122 用例）。
// 两边测的是同一套规则的镜像，踩坑点一一对应（见 docs/testing-prompt.md 的「重点场景」）。

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { pyFixed, pyRound } from '../src/engine/pyfmt.js'
import {
  distanceKm,
  fmtHHMM,
  haversineM,
  leg,
  parseHHMM,
  travelFrom,
} from '../src/engine/geo.js'
import {
  dayBudget,
  dayWindow,
  grade,
  isMust,
  isStandalone,
  mealMinutes,
  timeRule,
} from '../src/engine/rules.js'

// ---------------------------------------------------------------- pyfmt：Python 舍入语义
test('pyRound 是五取偶（banker rounding），不是 JS 的五一进位', () => {
  assert.equal(pyRound(742.5), 742) // JS Math.round 会给 743
  assert.equal(pyRound(743.5), 744)
  assert.equal(pyRound(0.5), 0)
  assert.equal(pyRound(1.5), 2)
  assert.equal(pyRound(2.5), 2)
  assert.equal(pyRound(2.6), 3)
  assert.equal(pyRound(2.4), 2)
})

test('pyFixed 复现 f"{x:.1f}" 的输出（6.25 → "6.2" 而不是 "6.3"）', () => {
  assert.equal(pyFixed(6.25, 1), '6.2')
  assert.equal(pyFixed(742.5, 0), '742')
  assert.equal(pyFixed(6.3, 1), '6.3')
  assert.equal(pyFixed(0, 1), '0.0')
})

// ---------------------------------------------------------------- geo：haversine / 交通
// 参照点：成都天府广场（30.657, 104.0657）
const CD = { name: '成都', kind: 'city', center_lat: 30.657, center_lng: 104.0657, hotel_areas: [] }
const att = (o) => ({ visit_minutes: 90, heat: 100, tags: [], best_time: '', ...o })

test('haversine：成都中心 → 熊猫基地（30.7406,104.138）约 10km 量级', () => {
  const m = haversineM(30.657, 104.0657, 30.7406, 104.138)
  assert.ok(m > 8000 && m < 12000, `实测 ${m}m`)
})

test('distanceKm 保留 1 位小数', () => {
  const km = distanceKm(30.657, 104.0657, 30.7406, 104.138)
  assert.ok(km > 8 && km < 12, `实测 ${km}km`)
  assert.ok(Math.abs(km * 10 - Math.round(km * 10)) < 1e-9, `${km} 不是 1 位小数`)
})

test('fmtHHMM 跨零点折回 24 小时制', () => {
  assert.equal(fmtHHMM(parseHHMM('09:00') + 630), '19:30')
  assert.equal(fmtHHMM(parseHHMM('23:50') + 80), '01:10') // 25:10 → 01:10
})

test('leg：≤1.5km 步行', () => {
  const go = leg(CD, 30.657, 104.0657, 30.666, 104.075, 'mixed')
  assert.equal(go.mode, 'walk')
  assert.equal(go.label, '步行')
})

test('leg mixed：短途打车、跨区中长途地铁', () => {
  // ~3km（城区内）→ 打车
  const near = leg(CD, 30.657, 104.0657, 30.68, 104.08, 'mixed')
  assert.equal(near.mode, 'taxi')
  // ~15km（仍在城区阈值内）→ 地铁
  const far = leg(CD, 30.657, 104.0657, 30.57, 104.15, 'mixed')
  assert.equal(far.mode, 'metro')
})

test('leg drive：城区 30 速、>80km 高速长途', () => {
  const city = leg(CD, 30.657, 104.0657, 30.75, 104.2, 'drive')
  assert.equal(city.label, '自驾')
  const hwy = leg(CD, 30.657, 104.0657, 29.6, 103.0, 'drive') // ~150km
  assert.equal(hwy.label, '自驾·高速')
  assert.equal(hwy.mode, 'drive')
})

test('leg transit：>80km 走城际铁路；耗时恒为 5 的倍数且 ≥5', () => {
  const r = leg(CD, 30.657, 104.0657, 29.6, 103.0, 'transit')
  assert.equal(r.mode, 'rail')
  for (const go of [leg(CD, 30.657, 104.0657, 30.75, 104.2, 'mixed'), r]) {
    assert.ok(go.minutes >= 5)
    assert.equal(go.minutes % 5, 0, `minutes=${go.minutes} 不是 5 的倍数`)
  }
})

// ---------------------------------------------------------------- rules：分级 / 必去 / 时段 / 预算
test('grade 边界：90 小 / 91 中 / 180 中 / 181 大', () => {
  assert.equal(grade(att({ visit_minutes: 90 })), '小')
  assert.equal(grade(att({ visit_minutes: 91 })), '中')
  assert.equal(grade(att({ visit_minutes: 180 })), '中')
  assert.equal(grade(att({ visit_minutes: 181 })), '大')
})

test('isMust：must_visit 标记 或 热度 ≥ 500（不是旧尺度的 90）', () => {
  assert.equal(isMust(att({ must_visit: true, heat: 10 })), true)
  assert.equal(isMust(att({ must_visit: false, heat: 499 })), false)
  assert.equal(isMust(att({ must_visit: false, heat: 500 })), true)
  assert.equal(isMust(att({ must_visit: false, heat: 501 })), true)
})

test('timeRule：best_time 字段优先，非法值回退关键词，都没有为 null', () => {
  assert.equal(timeRule(att({ best_time: 'morning', name: '随便' })).kind, 'morning')
  assert.equal(timeRule(att({ best_time: 'bogus', name: '杜甫草堂' })), null) // 非法 → 回退关键词，没命中
  assert.equal(timeRule(att({ best_time: '', name: '成都大熊猫繁育研究基地' })).kind, 'morning')
  assert.equal(timeRule(att({ best_time: '', name: '洪崖洞' })).kind, 'night')
  assert.equal(timeRule(att({ best_time: '', name: '四川博物院' })).kind, 'museum')
  assert.equal(timeRule(att({ best_time: '', name: '春熙路' })), null)
})

test('dayBudget：绝不随节奏变化（曾乘 ×1.15 导致最后一天超时）', () => {
  for (const pace of ['relaxed', 'balanced', 'packed']) {
    const req = { days: 2, start_time: '09:00', return_time: '19:30', pace, transport: 'mixed',
      transport_label: '', pace_label: '' }
    assert.equal(dayBudget(req, 1, 2), dayBudget({ ...req, pace: 'relaxed' }, 1, 2))
  }
})

test('dayWindow：首末天时间窗覆盖中间天', () => {
  const req = { days: 3, start_time: '09:00', return_time: '19:30',
    first_day_start_time: '13:00', last_day_return_time: '16:00' }
  assert.deepEqual(dayWindow(req, 1, 3), ['13:00', '19:30'])
  assert.deepEqual(dayWindow(req, 2, 3), ['09:00', '19:30'])
  assert.deepEqual(dayWindow(req, 3, 3), ['09:00', '16:00'])
})

test('mealMinutes：赶不上晚饭返回就只预留午餐（最后一天 16:00 返回）', () => {
  assert.equal(mealMinutes('19:30'), 60 + 75)
  assert.equal(mealMinutes('16:00'), 60)
})

// ---------------------------------------------------------------- rules：region 的独占型判定（2026-09-17 修复的回归）
// region 的参照点是「最近过夜基地」，不是中心 —— 中心到景点动辄几百公里，
// 全判独占型会导致 1 天只排 1 个点（河西走廊+莫高窟报超载的根因之一）。
const REGION = {
  name: '测试走廊', kind: 'region',
  center_lat: 39.0, center_lng: 98.0, // 中心在走廊中段
  hotel_areas: [
    { area: '东基地', lat: 39.0, lng: 98.0 },
    { area: '西基地', lat: 39.0, lng: 94.0 }, // 离中心 ~350km
  ],
}

test('region 独占型按最近基地判：基地边上的景点不是独占型', () => {
  const nearWest = att({ name: '西端景点', lat: 39.0, lng: 94.05, visit_minutes: 120 })
  // 市中心(98.0)到它 ~330km → 旧实现判独占；按最近基地(94.0)只有 ~4km → 不是
  assert.equal(isStandalone(REGION, nearWest, 'drive'), false)
})

test('region 独占型按最近基地判：离所有基地都远的仍是独占型', () => {
  const far = att({ name: '无人区景点', lat: 39.0, lng: 96.5, visit_minutes: 120 })
  // 离两个基地都 ~200km
  assert.equal(isStandalone(REGION, far, 'drive'), true)
})

test('city 的独占型判定不受 region 修复影响：市中心单程 ≥45min 即独占', () => {
  const farSuburb = att({ name: '远郊', lat: 29.6, lng: 103.0, visit_minutes: 120 })
  assert.equal(isStandalone(CD, farSuburb, 'mixed'), true)
})

test('travelFrom 对 city 仍以中心为参照', () => {
  const m = travelFrom(CD, att({ lat: 30.7406, lng: 104.138 }), 'mixed')
  assert.ok(m > 0 && m % 5 === 0)
})
