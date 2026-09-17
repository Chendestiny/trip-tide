// AI 旅行搭子 · **离线版**接口实现（与 `trip-api.js` 同名的导出，可整块替换）
//
// 面向「单文件 HTML」发布形态：没有后端、没有数据库、没有模型调用，
// 数据是构建期从种子 JSON 内联进来的（见 `scripts/build_offline_data.py`），
// 排程由 `src/engine/*` 在浏览器里算 —— 因此**打开就能用，不看任何外部服务**。
//
// ⚠️ 与联机版的契约必须逐字一致：视图层（TripHome / TripPick / TripPlan）不认识这份文件，
// 换实现靠 `vite.config.js` 的 `@api` 别名。字段形状以 `backend/app/trip/schemas.py` 为准。
//
// 与联机版的**固定差异**（都是刻意的，不是 bug）：
//   · `source` 恒为 `fallback` —— 界面会显示「规则引擎生成」而不是「AI 生成」，不撒谎；
//   · `review` 恒为 `null` —— 没有第二遍模型复核，方案说明里不会出现「AI 复核意见」；
//   · 方案存在 localStorage，`getPlan(id)` 找不到就报错（没有服务端兜底）。

import data from './engine/offline-data.json'
import { PACE_LABELS, TRANSPORT_LABELS } from './engine/consts.js'
import { pickAttractions, planFallback } from './engine/planner.js'
import { adjustPlanResult, previewPlan as previewPlanResult } from './engine/preview.js'

const CITIES = data.cities
const ATTRACTIONS = data.attractions

/** city_id → 该城市的景点（已按 heat 降序，与 list_attractions 同序） */
const BY_CITY = new Map()
for (const a of ATTRACTIONS) {
  if (!BY_CITY.has(a.city_id)) BY_CITY.set(a.city_id, [])
  BY_CITY.get(a.city_id).push(a)
}
const BY_ID = new Map(ATTRACTIONS.map((a) => [a.id, a]))

const PLAN_STORE_KEY = 'triptide.offline.plans.v1'
const PLAN_STORE_MAX = 20
let planSeq = 0
const planMemory = new Map()

function readStore() {
  if (planMemory.size) return planMemory
  try {
    const raw = JSON.parse(localStorage.getItem(PLAN_STORE_KEY) || '[]')
    for (const p of raw) {
      planMemory.set(p.plan_id, p)
      planSeq = Math.max(planSeq, Number(p.plan_id) || 0)
    }
  } catch {
    /* localStorage 不可用（隐私模式）时退化成纯内存，不阻断主流程 */
  }
  return planMemory
}

function writeStore(plan) {
  planMemory.set(plan.plan_id, plan)
  try {
    const all = [...planMemory.values()].sort((a, b) => b.plan_id - a.plan_id).slice(0, PLAN_STORE_MAX)
    localStorage.setItem(PLAN_STORE_KEY, JSON.stringify(all))
  } catch {
    /* 配额满：保留内存里的即可 */
  }
}

const nextPlanId = () => {
  readStore()
  planSeq += 1
  return planSeq
}

const stamp = (d = new Date()) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
  `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`

/** 与 `service.resolve_city` 一致：先按 name，再按 pinyin.toLowerCase() */
function resolveCity(key) {
  const k = String(key || '').trim()
  const hit = CITIES.find((c) => c.name === k) || CITIES.find((c) => (c.pinyin || '').toLowerCase() === k.toLowerCase())
  if (!hit) throw new Error(`目的地「${key}」暂未开放`)
  return hit
}

const normHHMM = (v, fallback) => {
  const s = String(v ?? '').trim().replace('：', ':')
  const m = /^(\d{1,2}):(\d{1,2})$/.exec(s)
  if (!m) return fallback
  const h = Number(m[1])
  const mi = Number(m[2])
  if (h > 23 || mi > 59) return fallback
  return `${String(h).padStart(2, '0')}:${String(mi).padStart(2, '0')}`
}

/**
 * 归一化入参 —— 对应后端 Pydantic 的 validators：
 * `taxi` → `mixed`、id 去重、时间格式规范化、天数夹到 1~7。
 * 另外补上 `transport_label` / `pace_label`（planner 内部要用，后端是 Pydantic property）。
 */
function normalizeRequest(payload) {
  const transport = payload.transport === 'taxi' ? 'mixed' : payload.transport || 'mixed'
  const pace = payload.pace || 'balanced'
  const days = Math.min(7, Math.max(1, Math.trunc(Number(payload.days) || 1)))
  const ids = []
  for (const raw of payload.attraction_ids || []) {
    const id = Number(raw)
    if (Number.isFinite(id) && !ids.includes(id)) ids.push(id)
  }
  return {
    city: String(payload.city || ''),
    attraction_ids: ids,
    days,
    start_time: normHHMM(payload.start_time, '09:00'),
    return_time: normHHMM(payload.return_time, '19:30'),
    first_day_start_time: payload.first_day_start_time ? normHHMM(payload.first_day_start_time, null) : null,
    last_day_return_time: payload.last_day_return_time ? normHHMM(payload.last_day_return_time, null) : null,
    transport,
    pace,
    transport_label: TRANSPORT_LABELS[transport] || transport,
    pace_label: PACE_LABELS[pace] || pace,
  }
}

/** 与 `service._load_selected` 一致：按 id 取景点、校验属于该城、按热度降序 */
function loadSelected(city, ids) {
  const pool = BY_CITY.get(city.id) || []
  const picked = pool.filter((a) => ids.includes(a.id))
  if (!picked.length) throw new Error('所选景点不属于该城市，请重新勾选')
  return [...picked].sort((a, b) => b.heat - a.heat)
}

/** 去掉 `spots`（那是 `getSpots` 的事）——对应 AttractionOut 的形状 */
function toAttractionOut(a) {
  const { spots, city_id, ...rest } = a
  return rest
}

// ---------------------------------------------------------------- 对外接口（与 trip-api.js 同名）
export const getCities = () =>
  CITIES.map(({ rank_score, ...rest }) => rest) // rank_score 是排序用的内部键，不进 CityOut

export const getAttractions = async (city) => {
  const hit = resolveCity(city)
  return (BY_CITY.get(hit.id) || []).map(toAttractionOut)
}

export const getSpots = async (id) => (BY_ID.get(Number(id))?.spots || []).map((s) => ({ ...s }))

export const previewPlan = async (payload) => {
  const req = normalizeRequest(payload)
  const city = resolveCity(req.city)
  return previewPlanResult(city, loadSelected(city, req.attraction_ids), req)
}

/** 组装一份 PlanResponse（对应 `service._to_response`） */
function toResponse({ planId, req, city, result, selected, source, title }) {
  return {
    plan_id: planId,
    created_at: stamp(),
    source,
    title,
    request: { ...req, transport_label: undefined, pace_label: undefined },
    result,
    attractions: selected.map(toAttractionOut),
    review: null,
    trace: [],
  }
}

const OFFLINE_NOTE = '（离线版：整条路线由本地规则引擎在你的浏览器里算出）'

export const createPlan = async (payload) => {
  const req = normalizeRequest(payload)
  const city = resolveCity(req.city)
  const selected = loadSelected(city, req.attraction_ids)
  const result = planFallback(city, selected, req)
  result.summary = `${result.summary} ${OFFLINE_NOTE}`.trim().slice(0, 300)
  const plan = toResponse({
    planId: nextPlanId(),
    req,
    city,
    result,
    selected,
    source: 'fallback',
    title: `${city.name} · ${req.days} 天 ${selected.length} 景`,
  })
  writeStore(plan)
  return plan
}

export const autoPlan = async (payload) => {
  // 对应 generate_auto_plan：先按「天数 + 节奏」自动挑景点，再走同一条生成链路
  const req = normalizeRequest({ ...payload, attraction_ids: [] })
  const city = resolveCity(req.city)
  const pool = BY_CITY.get(city.id) || []
  const picked = pickAttractions(city, pool, req.days, req.pace, req.transport)
  return createPlan({ ...payload, city: city.name, attraction_ids: picked.map((a) => a.id) })
}

export const adjustPlan = async (planId, patchPayload) => {
  const store = readStore()
  const old = store.get(Number(planId))
  if (!old) throw new Error('这份方案不在本机记录里，请重新生成')
  const patch = normalizeRequest({ ...old.request, ...patchPayload })
  const city = resolveCity(patch.city)
  const selected = loadSelected(city, old.request.attraction_ids)
  const allMap = new Map(selected.map((a) => [a.id, a]))
  const result = adjustPlanResult(city, allMap, old.result, patch)
  const plan = toResponse({
    planId: nextPlanId(),
    req: patch,
    city,
    result,
    selected,
    source: 'tweak',
    title: `${city.name} · ${patch.days} 天 ${selected.length} 景`,
  })
  writeStore(plan)
  return plan
}

export const getPlan = async (id) => {
  const hit = readStore().get(Number(id))
  if (!hit) throw new Error(`规划 #${id} 不在本机记录里`)
  return hit
}

export const listPlans = async (limit = 30) =>
  [...readStore().values()]
    .sort((a, b) => b.plan_id - a.plan_id)
    .slice(0, Math.min(100, Math.max(1, limit)))
    .map((p) => ({
      plan_id: p.plan_id,
      city: p.result?.city || p.request?.city || '—',
      days: p.result?.days || p.request?.days || 0,
      title: p.title,
      source: p.source,
      created_at: p.created_at,
      attraction_count: p.attractions?.length || 0,
    }))

/** 首页提示条要读它：`db: 'offline'` 让 TripHome 显示「离线演示版」而不是「未配置 Key」 */
export async function health() {
  return {
    ok: true,
    app: 'AI 旅行搭子 · 离线版',
    version: 'offline',
    llm_ready: false,
    amap_ready: false,
    db: 'offline',
    model: '本地规则引擎',
  }
}
