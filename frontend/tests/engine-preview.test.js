// 排程引擎 · 紧凑度预估 + region 回归（2026-09-17 用户实测 bug）+ 倾向微调冒烟
//
// 回归背景：河西走廊（region）+ 莫高窟曾被预估判「超载、舍弃」——
// 根因是裁剪/独占判定用「目的地中心往返」而 region 横跨上千公里。
// 修复口径：region 的几何参照点 = 最近的过夜基地（hotel_areas）；
// preview 的逐天裁剪传「当天住宿片区」做 origin，与真实生成同口径。
// 这几个用例**必须永远绿**，绿了就说明 region 的误判没有回来。

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { previewPlan, adjustPlanResult } from '../src/engine/preview.js'
import { planFallback, pickAttractions } from '../src/engine/planner.js'
import { PACE_LABELS, TRANSPORT_LABELS } from '../src/engine/consts.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..') // frontend/
const data = JSON.parse(readFileSync(resolve(ROOT, 'src/engine/offline-data.json'), 'utf8'))

const byCity = new Map()
for (const a of data.attractions) {
  if (!byCity.has(a.city_id)) byCity.set(a.city_id, [])
  byCity.get(a.city_id).push(a)
}
const cityByName = (n) => data.cities.find((c) => c.name === n)

const mkReq = (city, ids, o = {}) => ({
  city, attraction_ids: ids, days: o.days ?? 1,
  start_time: '09:00', return_time: '19:30',
  first_day_start_time: null, last_day_return_time: null,
  transport: o.transport ?? 'mixed', pace: o.pace ?? 'packed',
  transport_label: TRANSPORT_LABELS[o.transport ?? 'mixed'],
  pace_label: PACE_LABELS[o.pace ?? 'packed'],
})

// ---------------------------------------------------------------- 用户实测回归：河西走廊 + 莫高窟
test('region 回归：莫高窟单选 1 天（紧凑）→ 排入 1/1、无舍弃、轻松', () => {
  const city = cityByName('河西走廊')
  const mogao = (byCity.get(city.id) || []).find((a) => a.name === '莫高窟')
  assert.ok(mogao, '种子数据里应存在莫高窟')

  const pv = previewPlan(city, [mogao], mkReq(city.name, [mogao.id], { days: 1, pace: 'packed' }))
  assert.equal(pv.scheduled_count, 1, `排入 ${pv.scheduled_count}，莫高窟被误舍弃`)
  assert.equal(pv.will_drop.length, 0)
  assert.equal(pv.tightness, '轻松')
  assert.ok(!pv.suggestion.includes('远超'), '日均 240 < 450 却喊「远超」—— 自相矛盾文案回来了')
})

test('region 回归：同一场景加到 3 天依然正常（加天数不再越加越糟）', () => {
  const city = cityByName('河西走廊')
  const mogao = (byCity.get(city.id) || []).find((a) => a.name === '莫高窟')
  const pv = previewPlan(city, [mogao], mkReq(city.name, [mogao.id], { days: 3, pace: 'packed' }))
  assert.equal(pv.scheduled_count, 1)
  assert.equal(pv.will_drop.length, 0)
})

test('region 回归：生成路径（plan_fallback）与预估口径一致 —— 莫高窟真能排进时间轴', () => {
  const city = cityByName('河西走廊')
  const mogao = (byCity.get(city.id) || []).find((a) => a.name === '莫高窟')
  const result = planFallback(city, [mogao], mkReq(city.name, [mogao.id], { days: 1, pace: 'packed' }))
  const ids = result.day_plans.flatMap((d) => d.nodes).filter((n) => n.attraction_id).map((n) => n.attraction_id)
  assert.ok(ids.includes(mogao.id), '预估说能排、生成却没排 —— 两条路径口径又分叉了')
  assert.equal(result.dropped.length, 0)
})

test('region 回归：云南 丽江古城+玉龙雪山 选 1 天，合计 540min 判超载是对的', () => {
  const city = cityByName('云南')
  const sel = (byCity.get(city.id) || [])
    .sort((a, b) => b.heat - a.heat)
    .filter((a) => ['丽江古城', '玉龙雪山'].includes(a.name))
  const visitSum = sel.reduce((s, a) => s + a.visit_minutes, 0)
  const pv = previewPlan(city, sel, mkReq(city.name, sel.map((a) => a.id), { days: 1, pace: 'packed' }))
  if (visitSum > 450) {
    // 量真的超标 → 超载 + 舍弃是对的；但文案不许自相矛盾
    assert.equal(pv.tightness, '超载')
    assert.ok(!pv.suggestion.includes('本不算多'))
  } else {
    assert.ok(pv.will_drop.length === 0 || !pv.suggestion.includes('远超'))
  }
})

// ---------------------------------------------------------------- 城市场景的预估不变量
test('city：成都 2 天塞 10 景 → 超载时建议加天数，且 will_drop 与 per_day 一致', () => {
  const city = cityByName('成都')
  const sel = (byCity.get(city.id) || []).sort((a, b) => b.heat - a.heat).slice(0, 10)
  const pv = previewPlan(city, sel, mkReq(city.name, sel.map((a) => a.id), { days: 2, pace: 'balanced' }))
  assert.equal(pv.selected_count, 10)
  const inDays = pv.per_day.reduce((s, d) => s + d.count, 0)
  assert.equal(inDays, pv.scheduled_count)
  assert.ok(['轻松', '适中', '紧凑', '超载'].includes(pv.tightness))
  assert.ok(pv.suggestion.length > 10)
})

// ---------------------------------------------------------------- adjustPlanResult 冒烟
test('adjust：沿用旧分天重排，宽松会丢、紧凑会找回（不碰 LLM）', () => {
  const city = cityByName('成都')
  const sel = (byCity.get(city.id) || []).sort((a, b) => b.heat - a.heat).slice(0, 10)
  const req = mkReq(city.name, sel.map((a) => a.id), { days: 2, pace: 'packed' })
  const generated = planFallback(city, sel, req)

  const relaxed = adjustPlanResult(
    city,
    new Map(sel.map((a) => [a.id, a])),
    generated, // planFallback 直接返回 PlanResult 形状（没有 .result 包一层）
    { ...req, days: 2, pace: 'relaxed', pace_label: '宽松' },
  )
  assert.equal(relaxed.days, 2)
  assert.ok(relaxed.summary.length > 0)
  // 宽松重排后的总停留不高于 packed 版
  const stayOf = (r) => r.day_plans.reduce((s, d) => s + d.nodes.reduce((x, n) => x + n.stay_minutes, 0), 0)
  assert.ok(stayOf(relaxed) <= stayOf(generated))
})

// ---------------------------------------------------------------- pick 与 preview 的联动
test('一键挑完直接预估：不产生「排入 0」这种自相矛盾', () => {
  const city = cityByName('贵阳')
  const pool = (byCity.get(city.id) || []).sort((a, b) => b.heat - a.heat)
  const picked = pickAttractions(city, pool, 3, 'balanced', 'mixed')
  assert.ok(picked.length > 0)
  const pv = previewPlan(city, picked, mkReq(city.name, picked.map((a) => a.id), { days: 3, pace: 'balanced' }))
  assert.ok(pv.scheduled_count > 0, '一键挑出来的景点居然一个都排不下')
})
