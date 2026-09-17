// AI 旅行搭子 接口封装 —— Web 端。
//
// dev 下由 vite 把 /api 代理到 127.0.0.1:8002（见 vite.config.js）。
// 小程序端有一份独立实现：miniprogram/utils/request.js（wx.request）。
// 两端刻意不共享代码，各自演进，改一边不影响另一边。

/** 业务接口前缀。后端 routers.py 挂载在 /api/trip */
const API_BASE = '/api/trip'

/** 健康检查不在业务前缀下（main.py 直接挂在 /api） */
const HEALTH_PATH = '/api/health'

/**
 * 超时（毫秒）。
 * 规划要走 LLM 并行流水线，实测 15~30s 属正常，所以给到 150s——
 * 别按普通接口的十几秒设，否则长行程必然超时。
 */
const TIMEOUT = {
  default: 15000,
  preview: 12000,   // 纯硬编码，毫秒级，给 12s 已是极大冗余
  plan: 150000,
  adjust: 15000,    // 微调纯硬编码，秒级
}

/** 连不上时把这个贴进错误文案，排查时最有用 */
const TARGET_HINT = '/api → 127.0.0.1:8002（vite 代理）'

/**
 * 把后端各种形态的 error detail 归一成一句中文。
 * 后端有三种：字符串、FastAPI 校验错误数组（元素带 msg）、以及裸 HTTP 状态。
 */
function normalizeErrorDetail(status, detail) {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const msg = detail
      .map((d) => (d && typeof d === 'object' ? d.msg || JSON.stringify(d) : String(d)))
      .join('；')
    if (msg) return msg
  }
  return `请求失败（HTTP ${status}）`
}

/** 超时文案：规划超时要提示「减少景点」，普通超时要提示「检查后端」，两者别混用 */
function timeoutMessage(timeout) {
  return timeout === TIMEOUT.plan
    ? 'AI 规划超时了（150s），请减少景点数量或稍后重试'
    : '请求超时，请检查后端是否已启动'
}

async function request(path, { method = 'GET', body, timeout = TIMEOUT.default } = {}) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  try {
    const r = await fetch(API_BASE + path, {
      method,
      signal: ctrl.signal,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    const text = await r.text()
    let data = null
    try { data = text ? JSON.parse(text) : null } catch { data = null }

    if (!r.ok) throw new Error(normalizeErrorDetail(r.status, data?.detail))
    return data
  } catch (e) {
    if (e.name === 'AbortError') throw new Error(timeoutMessage(timeout))
    if (e instanceof TypeError) {
      throw new Error(`连不上后端服务（${TARGET_HINT}），请确认已启动`)
    }
    throw e
  } finally {
    clearTimeout(timer)
  }
}

/** 热门城市宫格（含 region 类型目的地） */
export const getCities = () => request('/cities')

/** 某目的地的景点池（后端已按 heat 降序返回） */
export const getAttractions = (city) => request(`/attractions?city=${encodeURIComponent(city)}`)

/** 某景点的内部子景点（名称/顺序/停留/攻略，纯静态数据，不调 LLM） */
export const getSpots = (id) => request(`/attractions/${id}/spots`)

/** 紧凑度预估：纯硬编码、毫秒级、不调 LLM。选景点时实时调用 */
export const previewPlan = (payload) =>
  request('/preview', { method: 'POST', body: payload, timeout: TIMEOUT.preview })

/** 生成行程（核心，LLM 并行流水线，15~30s） */
export const createPlan = (payload) =>
  request('/plan', { method: 'POST', body: payload, timeout: TIMEOUT.plan })

/** 一键 AI：只给城市 + 天数 + 节奏，景点由服务端自动挑，再走同一条生成链路 */
export const autoPlan = (payload) =>
  request('/auto-plan', { method: 'POST', body: payload, timeout: TIMEOUT.plan })

/** 按倾向微调已有方案：纯硬编码、毫秒级，不重新走 LLM */
export const adjustPlan = (planId, payload) =>
  request(`/plan/${planId}/adjust`, { method: 'POST', body: payload, timeout: TIMEOUT.adjust })

/** 按 id 读取历史行程 */
export const getPlan = (id) => request(`/plan/${id}`)

/** 后端侧的历史列表（本地 storage 之外的兜底） */
export const listPlans = (limit = 30) => request(`/plans?limit=${limit}`)

/** 健康检查：用于首页提示「AI 引擎 / 高德 Key 是否就绪」 */
export async function health() {
  const r = await fetch(HEALTH_PATH)
  if (!r.ok) throw new Error('HTTP ' + r.status)
  return r.json()
}
