// TripTide 接口封装：4 个后端接口 + 统一错误文案
// dev 下由 vite 把 /api 代理到 127.0.0.1:8000（见 vite.config.js）
// 未来并入 my-website 时本文件原样可用，只需在 main.js 注册路由。

const BASE = '/api/trip'

// 规划接口要等 LLM，10~30s 属正常，超时给足 150s
const PLAN_TIMEOUT = 150000
const DEFAULT_TIMEOUT = 15000

async function request(path, { method = 'GET', body, timeout = DEFAULT_TIMEOUT } = {}) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  try {
    const r = await fetch(BASE + path, {
      method,
      signal: ctrl.signal,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    const text = await r.text()
    let data = null
    try { data = text ? JSON.parse(text) : null } catch { data = null }

    if (!r.ok) {
      const detail = data?.detail
      throw new Error(
        typeof detail === 'string' ? detail
          : Array.isArray(detail) ? detail.map(d => d.msg || JSON.stringify(d)).join('；')
            : `请求失败（HTTP ${r.status}）`
      )
    }
    return data
  } catch (e) {
    if (e.name === 'AbortError') {
      throw new Error(timeout === PLAN_TIMEOUT
        ? 'AI 规划超时了（150s），请减少景点数量或稍后重试'
        : '请求超时，请检查后端是否已启动')
    }
    if (e instanceof TypeError) throw new Error('连不上后端服务，请确认 8002 端口已启动')
    throw e
  } finally {
    clearTimeout(timer)
  }
}

/** 热门城市宫格 */
export const getCities = () => request('/cities')

/** 某城市的景点池（后端已按 heat 降序返回） */
export const getAttractions = (city) => request(`/attractions?city=${encodeURIComponent(city)}`)

/** 紧凑度预估：纯硬编码、毫秒级、不调 LLM。选景点时实时调用 */
export const previewPlan = (payload) =>
  request('/preview', { method: 'POST', body: payload, timeout: 12000 })

/** 生成行程（核心，LLM 并行流水线，15~30s） */
export const createPlan = (payload) =>
  request('/plan', { method: 'POST', body: payload, timeout: PLAN_TIMEOUT })

/** 一键 AI：只给城市 + 天数 + 节奏，景点由服务端自动挑，再走同一条生成链路 */
export const autoPlan = (payload) =>
  request('/auto-plan', { method: 'POST', body: payload, timeout: PLAN_TIMEOUT })

/** 按倾向微调已有方案：纯硬编码、毫秒级，不重新走 LLM */
export const adjustPlan = (planId, payload) =>
  request(`/plan/${planId}/adjust`, { method: 'POST', body: payload, timeout: 15000 })

/** 按 id 读取历史行程 */
export const getPlan = (id) => request(`/plan/${id}`)

/** 后端侧的历史列表（本地 storage 之外的兜底） */
export const listPlans = (limit = 30) => request(`/plans?limit=${limit}`)

/** 健康检查：用于首页提示「AI 引擎 / 高德 Key 是否就绪」 */
export async function health() {
  const r = await fetch('/api/health')
  if (!r.ok) throw new Error('HTTP ' + r.status)
  return r.json()
}
