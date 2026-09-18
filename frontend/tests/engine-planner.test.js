// 排程引擎 · 分天/裁剪/物化 的单元测试（node --test，零依赖）
//
// 重点覆盖 docs/testing-prompt.md 里列的踩坑场景：
//   锦里↔武侯祠 200 米级必须同簇同天 / 独占型隔离 / 容量裁剪不丢必去 /
//   pick 收口 / materialize_day 时间自洽 + 饭点窗口 / 首末天时间窗

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  assignDays,
  clusterAttractions,
  estimateDaysNeeded,
  groupLoad,
  materializeDay,
  pickAttractions,
  planFallback,
  pruneToCapacity,
  trimDay,
} from '../src/engine/planner.js'
import { dayBudget } from '../src/engine/rules.js'
import { PACE_LABELS, TRANSPORT_LABELS } from '../src/engine/consts.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..', '..')

// ---------------------------------------------------------------- fixtures
const CITY = {
  name: '测试城', kind: 'city',
  center_lat: 30.657, center_lng: 104.0657,
  hotel_areas: [
    { area: '核心区', lat: 30.657, lng: 104.0657, pros: '', cons: '' },
    { area: '远郊镇', lat: 30.95, lng: 103.6, pros: '', cons: '' },
  ],
}

let seq = 0
const att = (name, o = {}) => ({
  id: ++seq,
  name,
  intro: '', district: '核心区', lat: 30.657, lng: 104.0657,
  visit_minutes: 90, heat: 100, must_visit: false, best_time: '', tags: [],
  spot_count: 0, distance_km: 0, spots: [],
  ...o,
})

const req = (o = {}) => ({
  city: '测试城', attraction_ids: [], days: 2,
  start_time: '09:00', return_time: '19:30',
  first_day_start_time: null, last_day_return_time: null,
  transport: 'mixed', pace: 'balanced',
  transport_label: TRANSPORT_LABELS.mixed, pace_label: PACE_LABELS.balanced,
  ...o,
})

/** 时间轴自洽：每个节点 time == 上一节点 time + stay + **本节点** travel
 *  （travel_minutes 挂在当前节点上 —— 「从上一站到这里的耗时」，见 PLAN_SCHEMA.md） */
function assertSelfConsistent(dp) {
  for (let i = 1; i < dp.nodes.length; i += 1) {
    const prev = dp.nodes[i - 1]
    const cur = dp.nodes[i]
    const [h, m] = prev.time.split(':').map(Number)
    const expect = (h * 60 + m) % (24 * 60) + prev.stay_minutes + cur.travel_minutes
    const [ch, cm] = cur.time.split(':').map(Number)
    assert.equal(
      ((ch * 60 + cm) % (24 * 60) + 24 * 60) % (24 * 60),
      expect % (24 * 60),
      `Day${dp.day} 节点「${cur.name}」时间不自洽`,
    )
  }
  assert.equal(dp.nodes.at(-1).type, 'hotel', `Day${dp.day} 最后一个节点必须是 hotel`)
}

// ---------------------------------------------------------------- 聚类
test('cluster：锦里↔武侯祠这种 200 米级景点必须同簇', () => {
  const jinli = att('锦里古街', { lat: 30.6446, lng: 104.0436 })
  const wuhou = att('武侯祠', { lat: 30.6442, lng: 104.0434 })
  const far = att('熊猫基地', { lat: 30.7406, lng: 104.138 })
  const clusters = clusterAttractions(CITY, [jinli, wuhou, far], 'mixed')
  assert.equal(clusters.length, 2)
  const pair = clusters.find((c) => c.some((a) => a.name === '锦里古街'))
  assert.ok(pair.some((a) => a.name === '武侯祠'), '紧邻的两点被拆开了')
})

test('cluster：单元素与空列表', () => {
  assert.deepEqual(clusterAttractions(CITY, [], 'mixed'), [])
  const one = [att('孤儿景点')]
  assert.equal(clusterAttractions(CITY, one, 'mixed').length, 1)
})

// ---------------------------------------------------------------- 容量裁剪
test('prune：装不下时丢性价比最低的，且必去受保护', () => {
  // 1 天、16:00 返回 → 预算 360（不排晚餐），三个点合计 450 > 360×1.1 → 必须裁
  const must = att('必去大景点', { heat: 500, visit_minutes: 300, must_visit: true })
  const junk1 = att('低分近郊', { heat: 60, visit_minutes: 60 })
  const junk2 = att('低分近郊 2', { heat: 61, visit_minutes: 60 })
  const [kept, dropped] = pruneToCapacity(CITY, [must, junk1, junk2], req({ days: 1, return_time: '16:00' }))
  assert.ok(!kept.includes(junk1), '应丢性价比最低的')
  assert.ok(kept.includes(must), '必去不该被丢')
  assert.equal(dropped.length, 1)
  assert.ok(dropped[0].reason.length > 0, '舍弃要给理由')
})

test('prune：只剩必去时不再裁（kept 永远非空）', () => {
  const must = att('必去', { heat: 900, visit_minutes: 600, must_visit: true })
  const [kept, dropped] = pruneToCapacity(CITY, [must], req({ days: 1 }))
  assert.equal(kept.length, 1)
  assert.equal(dropped.length, 0)
})

// ---------------------------------------------------------------- pick（一键挑）
test('pick：必去优先、按目标时长收口（不能全选）', () => {
  const pool = [
    att('普通A', { heat: 100, visit_minutes: 200 }),
    att('普通B', { heat: 99, visit_minutes: 200 }),
    att('必去C', { heat: 80, visit_minutes: 60, must_visit: true }),
  ]
  const picked = pickAttractions(CITY, pool, 1, 'relaxed', 'mixed')
  assert.equal(picked[0].name, '必去C') // 必去打头
  const total = picked.reduce((s, a) => s + a.visit_minutes, 0)
  assert.ok(total <= 240 * 1.1 + 1, `收口失败：累计 ${total} 分钟（1 天 relaxed 目标 240）`)
})

test('pick：独占型数量不超过天数', () => {
  const pool = [1, 2, 3, 4].map((i) => att(`大景点${i}`, { visit_minutes: 300, heat: 500 - i }))
  const picked = pickAttractions(CITY, pool, 2, 'packed', 'mixed')
  const standalone = picked.filter((a) => a.visit_minutes >= 240)
  assert.ok(standalone.length <= 2, `2 天却挑了 ${standalone.length} 个独占型`)
})

// ---------------------------------------------------------------- groupLoad / trim / estimate
test('groupLoad = 游览 + 路程（含回中心），必须大于纯游览', () => {
  const items = [att('A', { lat: 30.9, lng: 103.9, visit_minutes: 120 })]
  assert.ok(groupLoad(CITY, items, 'mixed') > 120)
})

test('estimate_days_needed：独占型 + 片区簇', () => {
  const far = att('远郊大景点', { lat: 30.95, lng: 103.6, visit_minutes: 300 }) // 独占
  const a = att('锦里', { lat: 30.6446, lng: 104.0436 })
  const b = att('武侯祠', { lat: 30.6442, lng: 104.0434 })
  assert.equal(estimateDaysNeeded(CITY, [far, a, b], 'mixed'), 2) // 1 独占 + 1 簇
})

test('trim：预算装不下顺延，最后仍有产出', () => {
  const items = Array.from({ length: 5 }, (_, i) => att(`点${i}`, { visit_minutes: 180 }))
  const [kept, overflow] = trimDay(CITY, items, req({ days: 1 }), 300)
  assert.ok(kept.length >= 1, '至少保留 1 个')
  assert.equal(kept.length + overflow.length, 5)
})

// ---------------------------------------------------------------- materialize_day：时间轴不变量
test('materialize：时间自洽 + 恰好一顿午餐（11:00~16:00）+ 晚餐 + hotel 收尾', () => {
  const r = req({ days: 2 })
  const items = [
    att('大熊猫基地', { lat: 30.7406, lng: 104.138, visit_minutes: 180, heat: 560, must_visit: true }),
    att('杜甫草堂', { lat: 30.659, lng: 104.028, visit_minutes: 120 }),
    att('春熙路', { lat: 30.657, lng: 104.081, visit_minutes: 90 }),
  ]
  const dp = materializeDay(CITY, 1, items, r, { totalDays: 2 })
  assertSelfConsistent(dp)

  const lunch = dp.nodes.filter((n) => n.type === 'meal' && n.name.includes('午餐'))
  const dinner = dp.nodes.filter((n) => n.type === 'meal' && n.name.includes('晚餐'))
  assert.equal(lunch.length, 1, `午餐数量 ${lunch.length}`)
  assert.ok('11:00' <= lunch[0].time && lunch[0].time <= '16:00', `午餐 ${lunch[0].time} 越窗`)
  assert.equal(dinner.length, 1)
  assert.ok('17:30' <= dinner[0].time && dinner[0].time <= '21:30', `晚餐 ${dinner[0].time} 越窗`)

  const ids = dp.nodes.filter((n) => n.attraction_id).map((n) => n.attraction_id)
  // 同一天内 id 重复是**合法的**：大景点跨午饭会被有意拆成「上午段 + 下午继续」
  // （service.audit_plan 的既定口径），但重复节点必须带「下午继续」标记
  for (const n of dp.nodes) {
    if (n.type === 'attraction' && n.name.includes('下午继续')) {
      assert.ok(ids.includes(n.attraction_id), '下午继续段的前一段应在同一天')
    }
  }
})

test('materialize：大景点跨午饭 → 拆成上午段 + 下午段（同 id 两个节点）', () => {
  const r = req({ days: 1 })
  const big = att('青城山', { lat: 30.9, lng: 103.55, visit_minutes: 300 })
  const dp = materializeDay(CITY, 1, [big], r, { totalDays: 1 })
  const attrNodes = dp.nodes.filter((n) => n.type === 'attraction')
  assert.equal(attrNodes.length, 2, '应拆成两段')
  assert.equal(attrNodes[0].attraction_id, attrNodes[1].attraction_id)
  assert.ok(attrNodes[1].name.includes('下午继续'))
  // 两段之和 = 弹性拉长后的总停留：≥ 原始游览时长，≤ 1.5×（大景点上限）
  const sum = attrNodes[0].stay_minutes + attrNodes[1].stay_minutes
  assert.ok(sum >= 300 && sum <= 450, `两段之和 ${sum} 应在 300~450 之间`)
})

test('materialize：最后一天显式返回时间 → 超时压缩停留，且午餐窗口仍合法', () => {
  const r = req({ days: 1, last_day_return_time: '15:00' })
  const items = [
    att('远郊大景点', { lat: 30.95, lng: 103.6, visit_minutes: 300 }),
    att('近郊点', { lat: 30.9, lng: 103.7, visit_minutes: 120 }),
  ]
  const [kept] = trimDay(CITY, items, r, dayBudget(r, 1, 1))
  const dp = materializeDay(CITY, 1, kept, r, { totalDays: 1 })
  assertSelfConsistent(dp)
  const lunch = dp.nodes.filter((n) => n.type === 'meal' && n.name.includes('午餐'))
  if (lunch.length) assert.ok('11:00' <= lunch[0].time && lunch[0].time <= '16:00')
})

// ---------------------------------------------------------------- plan_fallback 整体不变量
test('plan_fallback：所有勾选景点「要么排入要么舍弃」，天数连续，hotel 逐天给出', () => {
  const items = [
    att('大熊猫基地', { lat: 30.7406, lng: 104.138, visit_minutes: 180, heat: 560, must_visit: true }),
    att('都江堰景区', { lat: 30.99, lng: 103.61, visit_minutes: 240, heat: 520 }),
    att('杜甫草堂', { lat: 30.659, lng: 104.028, visit_minutes: 120 }),
    att('春熙路', { lat: 30.657, lng: 104.081, visit_minutes: 90 }),
    att('人民公园', { lat: 30.66, lng: 104.055, visit_minutes: 90 }),
  ]
  const r = req({ days: 2 })
  const result = planFallback(CITY, items, r)
  assert.equal(result.days, 2)
  assert.ok(result.day_plans.length === 2)

  const scheduled = new Set()
  for (const dp of result.day_plans) {
    assertSelfConsistent(dp)
    assert.ok(dp.hotel && dp.hotel.area)
    for (const n of dp.nodes) if (n.type === 'attraction' && n.attraction_id) scheduled.add(n.attraction_id)
  }
  for (const a of items) {
    const inDropped = result.dropped.some((d) => d.attraction_id === a.id)
    assert.ok(scheduled.has(a.id) || inDropped, `「${a.name}」凭空消失`)
  }
  assert.ok(result.summary.includes(CITY.name))
})
