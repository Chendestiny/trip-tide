// 排程引擎 · 补充覆盖：时段排序 / 子景点拼串 / 住宿取舍 / 机动日 / 裁剪例外 / 真数据回归
//
// 与另几个测试文件的分工：
//   engine-basics.test.js    数值语义、地理交通、单点判定
//   engine-planner.test.js   聚类、裁剪、物化、plan_fallback 主链路
//   engine-preview.test.js   预估与微调、region 误判回归
//   本文件                   上面没覆盖的函数 + 跨目的地的真数据不变量
//   api-boot-retry.test.js   接口层（不在引擎里）
//
// 姊妹篇：backend/tests/test_planner.py。改 planner.py 或 engine/*.js 后两边都要跑。

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { CLUSTER_MAX_METERS, OVERNIGHT_KM, PACE_LABELS, SPOT_ADVICE_MAX, TRANSPORT_LABELS } from '../src/engine/consts.js'
import { haversineM } from '../src/engine/geo.js'
import { pyRound } from '../src/engine/pyfmt.js'
import {
  emptyDay,
  materializeDay,
  pickHotel,
  planHotels,
  planFallback,
  trimDay,
  travelMinutes,
} from '../src/engine/planner.js'
import { previewPlan } from '../src/engine/preview.js'
import {
  applyTimeRules,
  dayBudget,
  isRemote,
  isStandalone,
  mealMinutes,
  ruleSortKey,
  spotAdvice,
} from '../src/engine/rules.js'

const HERE = dirname(fileURLToPath(import.meta.url))

// ---------------------------------------------------------------- fixtures
const CITY = {
  name: '测试城', kind: 'city',
  center_lat: 30.657, center_lng: 104.0657,
  hotel_areas: [
    { area: '核心区', lat: 30.657, lng: 104.0657, pros: '', cons: '' },
    { area: '远郊镇', lat: 30.95, lng: 103.6, pros: '离景区近', cons: '配套少' },
    { area: '南边新区', lat: 30.4, lng: 104.1, pros: '', cons: '' },
  ],
}

/** 环线：两个基地相距 ~517km（自驾 375 分钟），用来构造「抵达已过 15:00」的转场日 */
const CORRIDOR = {
  name: '测试走廊', kind: 'region',
  center_lat: 39.0, center_lng: 97.0,
  hotel_areas: [
    { area: '东基地', lat: 39.0, lng: 100.0 },
    { area: '西基地', lat: 39.0, lng: 94.0 },
    { area: '中基地', lat: 39.0, lng: 97.0 },
  ],
}

let seq = 0
const att = (name, o = {}) => ({
  id: ++seq, name,
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

/** 与 engine-planner.test.js 同一口径：travel_minutes 挂在**当前**节点上 */
function assertSelfConsistent(dp) {
  for (let i = 1; i < dp.nodes.length; i += 1) {
    const prev = dp.nodes[i - 1]
    const cur = dp.nodes[i]
    const [ph, pm] = prev.time.split(':').map(Number)
    const expect = (ph * 60 + pm) % (24 * 60) + prev.stay_minutes + cur.travel_minutes
    const [ch, cm] = cur.time.split(':').map(Number)
    assert.equal(
      ((ch * 60 + cm) % (24 * 60) + 24 * 60) % (24 * 60),
      expect % (24 * 60),
      `Day${dp.day} 节点「${cur.name}」时间不自洽`,
    )
  }
  assert.equal(dp.nodes.at(-1).type, 'hotel', `Day${dp.day} 最后一个节点必须是 hotel`)
}

const lunches = (dp) => dp.nodes.filter((n) => n.type === 'meal' && n.name.includes('午餐'))
const dinners = (dp) => dp.nodes.filter((n) => n.type === 'meal' && n.name.includes('晚餐'))

// ================================================================ 时段排序
test('ruleSortKey：早场 0 / 普通与博物馆 1 / 夜景 2', () => {
  assert.equal(ruleSortKey(att('熊猫基地', { best_time: 'morning' })), 0)
  assert.equal(ruleSortKey(att('春熙路')), 1)
  assert.equal(ruleSortKey(att('四川博物院', { best_time: 'museum' })), 1)
  assert.equal(ruleSortKey(att('洪崖洞', { best_time: 'night' })), 2)
})

test('applyTimeRules：早场打头、夜景压尾，其余保持原有顺路顺序', () => {
  const out = applyTimeRules([
    att('第一站'), att('熊猫基地', { best_time: 'morning' }), att('第二站'),
    att('四川博物院', { best_time: 'museum' }), att('洪崖洞', { best_time: 'night' }), att('第三站'),
  ])
  assert.equal(out[0].name, '熊猫基地', '早场类必须打头')
  assert.equal(out.at(-1).name, '洪崖洞', '夜景类必须压尾')
  // 同 key 的保持原相对顺序（JS sort 自 ES2019 起是稳定排序）
  const rest = out.filter((n) => !['熊猫基地', '洪崖洞'].includes(n.name)).map((n) => n.name)
  assert.deepEqual(rest, ['第一站', '第二站', '四川博物院', '第三站'])
})

test('applyTimeRules 不改动入参（返回新数组）', () => {
  const input = [att('洪崖洞', { best_time: 'night' }), att('春熙路')]
  applyTimeRules(input)
  assert.equal(input[0].name, '洪崖洞', '入参被就地排序了')
})

// ================================================================ 子景点拼串（不花 token 的那条路径）
test('spotAdvice：按 order_index 升序拼接，空 guide 的点位跳过，尾句号被吃掉', () => {
  const item = att('武侯祠', {
    spots: [
      { id: 3, name: '刘华堂', order_index: 3, guide: '两侧碑刻值得看完。' },
      { id: 1, name: '正门', order_index: 1, guide: '从正门进人最少' },
      { id: 2, name: '空点位', order_index: 2, guide: '   ' },
    ],
  })
  assert.equal(spotAdvice(item), '正门：从正门进人最少；刘华堂：两侧碑刻值得看完')
})

test('spotAdvice：order_index 相同按 id 升序', () => {
  const item = att('某景点', {
    spots: [
      { id: 7, name: '后到', order_index: 1, guide: 'B' },
      { id: 3, name: '先到', order_index: 1, guide: 'A' },
    ],
  })
  assert.equal(spotAdvice(item), '先到：A；后到：B')
})

test('spotAdvice：超长时停在**上一个完整点位**，绝不拼出半句', () => {
  // 每片 = 4 字名 + 「：」 + 55 字攻略 = 60 字符；上限 200、留 10 余量 → 只装得下 3 片
  const mk = (i, n) => ({ id: i, name: `点位${n}`, order_index: i, guide: '指'.repeat(55) + '。' })
  const item = att('大景点', { spots: [1, 2, 3, 4, 5].map((i) => mk(i, '一二三四五'[i - 1])) })
  const text = spotAdvice(item)
  assert.ok(text.length <= SPOT_ADVICE_MAX, `拼串 ${text.length} 超过上限 ${SPOT_ADVICE_MAX}`)
  assert.equal(text.split('；').length, 3, '应当只保留前 3 个完整点位')
  assert.ok(!text.includes('点位四'), '第四个点位被拼了半句')
  assert.ok(text.endsWith('指'), '结尾被硬截断，不是完整攻略')
})

test('spotAdvice：没有子景点返回空串（交给模型文案或 intro）', () => {
  assert.equal(spotAdvice(att('没点位的景点')), '')
  assert.equal(spotAdvice(att('空数组', { spots: [] })), '')
})

// ================================================================ 判定维度独立性
test('isRemote 与 isStandalone 是两个独立维度：大而不远 / 远而不大', () => {
  const bigNear = att('市区大景点', { visit_minutes: 300 }) // 游览 ≥240 → 独占，但不远郊
  assert.equal(isStandalone(CITY, bigNear, 'mixed'), true)
  assert.equal(isRemote(CITY, bigNear, 'mixed'), false)

  const smallFar = att('远郊小景点', { lat: 30.95, lng: 103.6, visit_minutes: 60 })
  assert.equal(isRemote(CITY, smallFar, 'mixed'), true)
  assert.equal(isStandalone(CITY, smallFar, 'mixed'), true) // 单程 ≥45min 也算独占
})

test('mealMinutes 边界：18:25 返回算两餐，18:20 返回只算午餐', () => {
  // 晚餐 75 分钟 + 走回酒店 10 分钟，且开饭不得早于 17:00
  assert.equal(mealMinutes('18:25'), 135)
  assert.equal(mealMinutes('18:20'), 60)
  assert.equal(mealMinutes('16:00'), 60)
})

test('dayBudget 有 180 分钟下限：极端时间窗不会算成 0 或负数', () => {
  assert.equal(dayBudget(req({ days: 1, start_time: '09:00', return_time: '09:30' }), 1, 1), 180)
})

// ================================================================ 裁剪的 OVERPACK 例外
test('trimDay：单点耗时未超「预算×1.5」时超预算也要保留（否则这天整个空掉）', () => {
  const only = att('唯一景点', { visit_minutes: 550 }) // 5 + 550 + 5 = 560 ≤ 400×1.5
  const [kept, overflow] = trimDay(CITY, [only], req({ days: 1 }), 400)
  assert.equal(kept.length, 1, '一个都没排上，这天会整天空掉')
  assert.equal(overflow.length, 0)
})

test('trimDay：单点耗时超「预算×1.5」→ 放弃留机动日（628km 转场日排到凌晨 1:25 的回归）', () => {
  const monster = att('巨无霸转场点', { visit_minutes: 700 }) // 5 + 700 + 5 = 710 > 400×1.5
  const [kept, overflow] = trimDay(CITY, [monster], req({ days: 1 }), 400)
  assert.equal(kept.length, 0)
  assert.equal(overflow.length, 1)
})

test('travelMinutes：同点往返也保底 5 分钟（不做 0 分钟这种不真实的路程）', () => {
  const a = att('同一点')
  assert.equal(travelMinutes(CITY, a, a, 'mixed'), 5)
})

// ================================================================ 住宿取舍
test('pickHotel：没配候选片区时回落到「市中心」，备选为空', () => {
  const h = pickHotel({ ...CITY, hotel_areas: [] }, [att('随便')])
  assert.equal(h.area, '测试城市中心')
  assert.deepEqual(h.alternatives, [])
})

test('pickHotel：选离景点平均距离最近的片区，并给出至多 2 个备选', () => {
  const h = pickHotel(CITY, [att('远郊景点', { lat: 30.95, lng: 103.6 })])
  assert.equal(h.area, '远郊镇')
  assert.equal(h.alternatives.length, 2, '三个候选应给出 2 个备选')
  assert.ok(h.reason.includes('离景区近'), '片区 pros 要拼进理由')
})

test('planHotels(city)：默认全程不换住；只有远郊日且本地明显更优才建议就近住一晚', () => {
  const nearDay = [att('市区点A', { lat: 30.66, lng: 104.07 }), att('市区点B', { lat: 30.65, lng: 104.06 })]
  const farDay = [att('远郊点', { lat: 30.95, lng: 103.6 })]
  const hotels = planHotels(CITY, [nearDay, farDay])

  assert.equal(hotels[0].area, '核心区', '第一天应住全程基座')
  assert.ok(!hotels[0].reason.includes('就近住一晚'))
  assert.equal(hotels[1].area, '远郊镇')
  assert.ok(hotels[1].reason.includes('建议就近住一晚'), '远郊日应改推就近过夜')
  assert.equal(hotels[1].alternatives[0].area, '核心区', '要给出「不换」的代价说明')
})

test('planHotels(city)：远郊日若本地没有更优片区，就继续住同一片区并提醒早出发', () => {
  const onlyOne = { ...CITY, hotel_areas: [{ area: '核心区', lat: 30.657, lng: 104.0657 }] }
  const hotels = planHotels(onlyOne, [[att('远郊点', { lat: 30.95, lng: 103.6 })]])
  assert.equal(hotels[0].area, '核心区')
  assert.ok(hotels[0].reason.includes('早出发'), '应换成「继续住 + 早出发」的口径')
})

test('planHotels(region)：今晚住哪=今天走到哪，连住优先、明显更近才搬行李', () => {
  const near = (lng) => [att('点', { lat: 39.0, lng })]
  const hotels = planHotels(CORRIDOR, [near(100.02), near(99.98), near(94.02)])

  assert.equal(hotels[0].area, '东基地', '第一晚住第一天走到哪')
  assert.ok(hotels[1].reason.includes('继续住'), '第二天还在东基地边上，不该搬行李')
  assert.equal(hotels[2].area, '西基地')
  assert.ok(hotels[2].reason.includes('搬过来住'), '第三天走到走廊另一端，应搬')
  assert.ok(hotels[2].reason.includes('东基地'), '搬家的理由要写明从哪过来')
})

test('OVERNIGHT_KM 就是「远郊日就近住」的那条线（前端 45km 标记与引擎行为对齐）', () => {
  assert.equal(OVERNIGHT_KM, 45)
  // 刚好在阈值内的远郊日不搬家
  const inside = [att('近一点的', { lat: 31.05, lng: 104.0657 })] // ≈44km
  assert.ok(!planHotels({ ...CITY, hotel_areas: [CITY.hotel_areas[0], CITY.hotel_areas[1]] }, [inside])[0].reason.includes('就近住一晚'))
})

// ================================================================ 机动日
test('emptyDay（机动日）：时间自洽，且**有午餐** —— materializeDay([]) 只有晚餐', () => {
  const r = req({ days: 1 })
  const dp = emptyDay(CITY, r, 1, pickHotel(CITY, []))
  assertSelfConsistent(dp)
  assert.equal(lunches(dp).length, 1, '机动日必须保留午餐（BACKEND.md 记录的差异）')
  assert.equal(dinners(dp).length, 1)
  assert.ok(dp.theme.includes('机动日'))

  // 对照：空列表直接走 materializeDay 确实不排午餐 —— 所以机动日才必须走 emptyDay
  const viaMaterialize = materializeDay(CITY, 1, [], r, { totalDays: 1 })
  assert.equal(lunches(viaMaterialize).length, 0)
  assert.equal(dinners(viaMaterialize).length, 1)
})

test('planFallback：整天排空时落到机动日，而不是一个只有晚餐的空壳', () => {
  const plan = planFallback(CITY, [att('巨无霸', { visit_minutes: 2000 })], req({ days: 1 }))
  const dp = plan.day_plans[0]
  assert.ok(dp.theme.includes('机动日'), `实际 theme = ${dp.theme}`)
  assert.equal(lunches(dp).length, 1)
})

test('materializeDay：长途转场日首景晚于 15:00 到达时不单列午餐（那顿并入晚餐）', () => {
  const r = req({ days: 2, transport: 'drive' })
  // 东基地(100.0) → 西端景点(94.0) ≈517km，自驾 375 分钟 → 09:00 出发、15:15 才到
  const dp = materializeDay(CORRIDOR, 1, [att('西端景点', { lat: 39.0, lng: 94.0, visit_minutes: 120 })], r, {
    totalDays: 2,
    hotel: { area: '西基地' },
    departFrom: { area: '东基地' },
  })
  assertSelfConsistent(dp)

  const firstAtt = dp.nodes.find((n) => n.type === 'attraction')
  assert.ok(firstAtt, '应排出景点节点')
  assert.ok(firstAtt.time > '15:00', `fixture 没构造成长途转场日：首景 ${firstAtt.time} 到达`)
  assert.equal(lunches(dp).length, 0, '抵达已过午段，不该再单列一顿下午的「午餐」')
  assert.equal(dinners(dp).length, 1, '那顿并进晚餐')
})

// ================================================================ 预估文案的自相矛盾回归
test('previewPlan：有舍弃但日均不超标时，不再喊「远超目标」', () => {
  // 4 个景点各 200 分钟、摊到 2 天 = 日均 400 < packed 的 450 → 本不算多；
  // 但它们离中心 ~130km，路程把当天撑爆 → 仍会有舍弃。
  const items = [
    att('北边远点', { lat: 31.85, lng: 104.06, visit_minutes: 200 }),
    att('南边远点', { lat: 29.45, lng: 104.06, visit_minutes: 200 }),
    att('东边远点', { lat: 30.657, lng: 105.9, visit_minutes: 200 }),
    att('西边远点', { lat: 30.657, lng: 102.2, visit_minutes: 200 }),
  ]
  const p = previewPlan(CITY, items, req({ days: 2, pace: 'packed', pace_label: PACE_LABELS.packed }))
  assert.ok(p.will_drop.length > 0, '这个 fixture 没触发舍弃，测不到目标分支')
  assert.ok(!p.suggestion.includes('远超'), `日均不超标却仍在喊「远超目标」：${p.suggestion}`)
  assert.ok(p.suggestion.includes('本不算多'), `应走「路程太远/单点太长」那条分支：${p.suggestion}`)
})

test('previewPlan：per_day 条数恒等于天数，且排入 + 舍弃 = 勾选总数', () => {
  const items = Array.from({ length: 9 }, (_, i) =>
    att(`点${i}`, { lat: 30.6 + i * 0.05, lng: 104.0 + i * 0.05, visit_minutes: 150, heat: 300 - i }))
  const p = previewPlan(CITY, items, req({ days: 3 }))
  assert.equal(p.per_day.length, 3)
  const scheduled = p.per_day.reduce((s, d) => s + d.count, 0)
  assert.equal(scheduled + p.will_drop.length, items.length, '景点凭空消失或凭空多出')
  for (const d of p.per_day) assert.ok(d.budget_minutes >= 180)
})

// ================================================================ 真数据回归（68 目的地 / 899 景点）
const OFFLINE = JSON.parse(readFileSync(resolve(HERE, '..', 'src', 'engine', 'offline-data.json'), 'utf8'))
const byCity = new Map()
for (const a of OFFLINE.attractions) {
  if (!byCity.has(a.city_id)) byCity.set(a.city_id, [])
  byCity.get(a.city_id).push(a)
}
const REAL_CITIES = OFFLINE.cities.filter((c) => (byCity.get(c.id) || []).length >= 6)

test(`真数据：${REAL_CITIES.length} 个目的地跑 planFallback，三条不变量与景点守恒都成立`, () => {
  assert.ok(REAL_CITIES.length >= 50, `真数据覆盖数异常（只有 ${REAL_CITIES.length}）`)
  let days = 0

  for (const city of REAL_CITIES) {
    const pool = [...byCity.get(city.id)].sort((a, b) => b.heat - a.heat).slice(0, 12)
    const plan = planFallback(city, pool, req({ city: city.name, days: 3 }))

    assert.equal(plan.day_plans.length, 3, `${city.name} 天数不对`)
    const scheduled = new Set()
    for (const dp of plan.day_plans) {
      assertSelfConsistent(dp)
      days += 1
      for (const n of dp.nodes) {
        if (n.type === 'attraction') scheduled.add(n.attraction_id)
        if (n.type === 'meal' && n.name.includes('午餐')) {
          assert.ok('11:00' <= n.time && n.time <= '16:00', `${city.name} Day${dp.day} 午餐 ${n.time} 越窗`)
        }
      }
      if (lunches(dp).length === 0) {
        const first = dp.nodes.find((n) => n.type === 'attraction')
        assert.ok(!first || first.time > '15:00', `${city.name} Day${dp.day} 无午餐但首景 ${first?.time} 到达`)
      }
      assert.ok(dinners(dp).length <= 1, `${city.name} Day${dp.day} 晚餐多于一顿`)
    }

    const dropped = new Set(plan.dropped.map((d) => d.attraction_id))
    for (const a of pool) {
      assert.ok(scheduled.has(a.id) || dropped.has(a.id), `${city.name}「${a.name}」凭空消失`)
    }
    assert.equal(
      scheduled.size + dropped.size, pool.length,
      `${city.name} 排入 ${scheduled.size} + 舍弃 ${dropped.size} ≠ 勾选 ${pool.length}`,
    )
  }
  assert.ok(days >= REAL_CITIES.length * 3)
})

test('真数据：region 目的地不再「1 天只排 1 个」——贴着过夜基地的景点不是独占型', () => {
  const nearestBaseKm = (city, a) => {
    let best = Infinity
    for (const h of city.hotel_areas || []) {
      const d = Math.hypot(
        (h.lat - a.lat) * 111,
        (h.lng - a.lng) * 111 * Math.cos((a.lat * Math.PI) / 180),
      )
      if (d < best) best = d
    }
    return best
  }
  let cases = 0
  for (const city of REAL_CITIES.filter((c) => c.kind === 'region')) {
    for (const a of byCity.get(city.id)) {
      if ((a.visit_minutes || 0) >= 240) continue // 游览本身就 ≥4h，独占是对的
      if (nearestBaseKm(city, a) > 15) continue // 只查「贴着基地」的
      cases += 1
      assert.equal(
        isStandalone(city, a, 'mixed'), false,
        `${city.name}「${a.name}」离过夜基地仅 ${nearestBaseKm(city, a).toFixed(1)}km，不该判独占`,
      )
    }
  }
  assert.ok(cases > 40, `region 近基地样本只有 ${cases} 个，测试可能已失去意义`)
})

test('真数据：region 里挨得近的非独占景点必须排在同一天', () => {
  // 这才是「region 全被判独占型 → 1 天只排 1 个」那个故障的反面断言。
  // 注意不能用「前 8 热门」构造：环线的招牌景点本来就几乎全是 ≥4h 的大景点
  // （贵州前 8 里有 6 个 visit ≥240），一天一个是**正确行为**，不是 bug。
  // 所以只挑「两个都不到 4h、且相距 ≤1.2km」的一对 —— 它们必须落进同一个片区簇。
  const offenders = []
  let checked = 0
  let bothScheduled = 0
  for (const city of REAL_CITIES.filter((c) => c.kind === 'region')) {
    const small = (byCity.get(city.id) || []).filter((a) => (a.visit_minutes || 0) < 240)
    let best = null
    for (let i = 0; i < small.length; i += 1) {
      for (let j = i + 1; j < small.length; j += 1) {
        const m = haversineM(small[i].lat, small[i].lng, small[j].lat, small[j].lng)
        if (!best || m < best.m) best = { m, a: small[i], b: small[j] }
      }
    }
    if (!best || best.m > CLUSTER_MAX_METERS) continue // 这个目的地没有「挨得近的一对」
    checked += 1

    // 只额外带 2 个景点：环线路程巨大，带太多会被 pruneToCapacity 按性价比丢掉一个，
    // 而「被裁剪掉」与「被拆到不同天」是两件事 —— 前者合法，后者才违反簇原子性。
    const others = [...byCity.get(city.id)]
      .sort((a, b) => b.heat - a.heat)
      .filter((a) => a.id !== best.a.id && a.id !== best.b.id)
      .slice(0, 2)
    const plan = planFallback(city, [best.a, best.b, ...others], req({ city: city.name, days: 3, transport: 'drive' }))

    const dayOf = (id) => plan.day_plans.findIndex((dp) => dp.nodes.some((n) => n.attraction_id === id))
    const [da, db] = [dayOf(best.a.id), dayOf(best.b.id)]
    if (da === -1 || db === -1) continue // 有一个被容量裁剪掉了，不构成同日性违例
    bothScheduled += 1
    if (da !== db) {
      offenders.push(`${city.name}「${best.a.name}」/「${best.b.name}」(${(best.m / 1000).toFixed(2)}km) 被拆到 Day${da + 1} 与 Day${db + 1}`)
    }
  }
  assert.ok(checked >= 5, `只有 ${checked} 个 region 目的地有「近距对」，样本不足`)
  assert.ok(bothScheduled >= 3, `两个都排进行程的近距对只有 ${bothScheduled} 组，断言接近空转`)
  assert.deepEqual(offenders, [], `这些紧邻景点没能同日：\n${offenders.join('\n')}`)
})

test('真数据：全部目的地跑 previewPlan 不抛异常，档位与文案口径自洽', () => {
  for (const city of REAL_CITIES) {
    const pool = [...byCity.get(city.id)].sort((a, b) => b.heat - a.heat).slice(0, 8)
    for (const days of [1, 2, 5]) {
      const p = previewPlan(city, pool, req({ city: city.name, days }))
      assert.equal(p.per_day.length, days, `${city.name} ${days} 天 per_day 条数不对`)
      assert.ok(['轻松', '适中', '紧凑', '超载'].includes(p.tightness), `${city.name} 档位异常`)
      assert.ok(
        p.suggestion.startsWith(`${pool.length} 个景点摊到 ${days} 天`),
        `${city.name} 文案开头不对：${p.suggestion.slice(0, 30)}`,
      )
    }
  }
})

test('真数据：子景点拼串全部不超 advice 上限，且确实覆盖了大量景点', () => {
  let withSpots = 0
  for (const a of OFFLINE.attractions) {
    const text = spotAdvice(a)
    assert.ok(text.length <= SPOT_ADVICE_MAX, `「${a.name}」拼串 ${text.length} 超过上限`)
    if (text) withSpots += 1
  }
  assert.ok(OFFLINE.attractions.length >= 800)
  assert.ok(withSpots / OFFLINE.attractions.length > 0.5,
    `有子景点攻略的占比过低（${withSpots}），硬编码拼串这条路径可能已失效`)
})

// ---------------------------------------------------------------- 首页排序分（铁律 22 的 JS 侧镜像）
/** service.py 的 rank_score()：前 8 景指数加权，第 1 名 ×1.5、第 2/3 名 ×1.2 */
const RANK = { top: 8, decay: 0.9, firstBoost: 1.5, top3Boost: 1.2 }
function rankScore(heats) {
  let total = 0
  heats.slice().sort((a, b) => b - a).slice(0, RANK.top).forEach((heat, i) => {
    let w = RANK.decay ** i
    if (i === 0) w *= RANK.firstBoost
    else if (i < 3) w *= RANK.top3Boost
    total += heat * w
  })
  return pyRound(total) // Python 的 round() 是五取偶，不能用 Math.round
}

test('真数据：离线种子里的 rank_score 与现算值完全一致（首页顺序没陈旧）', () => {
  const stale = []
  for (const city of OFFLINE.cities) {
    const heats = (byCity.get(city.id) || []).map((a) => a.heat)
    if (rankScore(heats) !== city.rank_score) {
      stale.push(`${city.name} 存 ${city.rank_score} / 算 ${rankScore(heats)}`)
    }
  }
  assert.deepEqual(stale, [], `这些目的地的排序分已陈旧，要跑 scripts/rank_cities.py：\n${stale.join('\n')}`)
})

test('真数据：首页顺序就是按 rank_score 降序（第 1 名确实是热度担当）', () => {
  const sorted = [...OFFLINE.cities].sort((a, b) => b.rank_score - a.rank_score)
  for (let i = 1; i < sorted.length; i += 1) {
    assert.ok(sorted[i - 1].rank_score >= sorted[i].rank_score, '排序分序列不单调')
  }
  const topHeat = (c) => Math.max(...(byCity.get(c.id) || []).map((a) => a.heat))
  assert.ok(topHeat(sorted[0]) >= 900, `榜首「${sorted[0].name}」最高热度只有 ${topHeat(sorted[0])}`)
})

// ================================================================ 仓库级不变量
test('tests/ 下每个测试文件都挂在 package.json 的 test 脚本里', () => {
  // node --test 不认目录参数（会报 MODULE_NOT_FOUND），只能显式列文件 ——
  // 所以「新增测试文件忘了挂上去」是个真实风险，这里把它变成会失败的测试。
  const pkg = JSON.parse(readFileSync(resolve(HERE, '..', 'package.json'), 'utf8'))
  const listed = new Set(String(pkg.scripts.test).split(/\s+/).filter((t) => t.endsWith('.test.js')))
  const present = new Set(readdirSync(HERE).filter((f) => f.endsWith('.test.js')).map((f) => `tests/${f}`))
  const missing = [...present].filter((f) => !listed.has(f))
  assert.deepEqual(missing, [], `这些测试文件没挂进 npm test，永远不会被跑：${missing.join(', ')}`)
})