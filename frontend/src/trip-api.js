// AI 旅行搭子 接口封装 —— Web 端。
//
// dev 下由 vite 把 /api 代理到 127.0.0.1:8002（见 vite.config.js）。
// 小程序端有一份独立实现：miniprogram/utils/request.js（wx.request）。
// 两端刻意不共享代码，各自演进，改一边不影响另一边。
// （下面的「冷启动重试」是 Web dev 专属问题，没有同步到小程序端。）

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

// ---------------------------------------------------------------- 后端冷启动重试
/**
 * `npm run dev` 用 concurrently 同时拉起两端：Vite 约 0.6s 就 ready，
 * 后端要 2~4s（起进程 → 连 MySQL → init_db 建表）。浏览器首屏那两个请求
 * （`/api/health`、`/api/trip/cities`）正好落在这个空档里，打到还没监听的端口，
 * Vite 代理回一个 **500 + text/plain + 空 body** —— 首屏就此报错，
 * 必须手动刷新一次才好。这个「刷新一下才有」是用户实测报上来的。
 *
 * 所以在冷启动窗口内对**读类请求**做有限重试。三条边界，免得重试变成万能胶水：
 *   · 只在模块加载后的 `BOOT.windowMs` 内生效 —— 窗口之后真实错误立刻抛出，不拖慢排查；
 *   · 只重试 GET —— `POST /plan` 要跑 15~30s LLM，绝不能重放；
 *   · 只认「连不上」与「代理 5xx 且不是 JSON」两种签名，业务 4xx 直接抛。
 *
 * 为什么不在 vite 代理层做：Vite 自己的 error 监听器注册在 `configure` 之后，
 * 同一个 tick 里就把 500 写掉了，代理层抢不过（vite.config.js 里也留了这条注释）。
 */
const BOOT = {
  startedAt: Date.now(),
  windowMs: 12000,
  attempts: 12,
  delayMs: 600,
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** 还在冷启动窗口内吗 */
function inBootWindow(now = Date.now()) {
  return now - BOOT.startedAt <= BOOT.windowMs
}

/**
 * 后端「还没起来」的签名。
 * FastAPI 的真实错误一律带 JSON `{"detail": ...}`，代理的失败没有。
 */
function looksLikeNotUp(status, contentType, bodyText) {
  if (status < 500) return false
  return !bodyText || !String(contentType || '').includes('application/json')
}

/** 给 fetch 的响应体打标：让调用方知道该不该重试 */
class ApiError extends Error {
  constructor(message, { bootRetryable = false } = {}) {
    super(message)
    this.name = 'ApiError'
    this.bootRetryable = bootRetryable
  }
}

/**
 * 只在冷启动窗口内重试的包装。
 *
 * @param fn       实际发请求的函数
 * @param canRetry 该方法是否可重放（GET/HEAD 才是 true）
 */
async function withBootRetry(fn, canRetry) {
  if (!canRetry) return fn()
  for (let attempt = 1; ; attempt += 1) {
    let err
    try {
      return await fn()
    } catch (e) {
      err = e
    }
    if (!err || !err.bootRetryable || !inBootWindow()) throw err
    if (attempt > BOOT.attempts) {
      // 窗口内一路连不上 —— 「HTTP 500」对排查毫无帮助，换成能照着做的文案
      throw new ApiError(NOT_UP_MESSAGE)
    }
    await sleep(BOOT.delayMs)
  }
}

// ---------------------------------------------------------------- 请求底层
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

/** 冷启动重试耗尽后的文案 —— 比「HTTP 500」有用得多 */
const NOT_UP_MESSAGE = `连不上后端服务（${TARGET_HINT}），已等待约 ${Math.round((BOOT.attempts * BOOT.delayMs) / 1000)}s。请确认后端已启动：cd backend && venv/Scripts/python run.py`

/**
 * 包一层 fetch：网络层直接失败（连不上 / 端口还没监听）时，
 * `fetch` 抛的是裸 `TypeError`，没有任何标记 —— 不归一的话 `withBootRetry` 认不出来，
 * 冷启动重试就会**静默失效**（这个坑是端到端验证跑出来的，单测喂 Response 喂不出来）。
 */
async function rawFetch(url, init) {
  try {
    return await fetch(url, init)
  } catch (e) {
    if (e && e.name === 'AbortError') throw e // 超时是另一回事，交给调用方判
    if (e instanceof TypeError) {
      throw new ApiError(`连不上后端服务（${TARGET_HINT}），请确认已启动`, { bootRetryable: true })
    }
    throw e
  }
}

/**
 * 发一次请求并解析。`fullPath` 是从站点根算起的完整路径。
 * 失败时抛 `ApiError`，并按签名标好 `bootRetryable`。
 */
async function sendOnce(fullPath, { method = 'GET', body, timeout = TIMEOUT.default } = {}) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  try {
    const r = await rawFetch(fullPath, {
      method,
      signal: ctrl.signal,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    const text = await r.text()
    let data = null
    try { data = text ? JSON.parse(text) : null } catch { data = null }

    if (!r.ok) {
      throw new ApiError(normalizeErrorDetail(r.status, data?.detail), {
        bootRetryable: looksLikeNotUp(r.status, r.headers?.get?.('content-type'), text),
      })
    }
    return data
  } catch (e) {
    if (e instanceof ApiError) throw e
    if (e.name === 'AbortError') throw new ApiError(timeoutMessage(timeout))
    throw e
  } finally {
    clearTimeout(timer)
  }
}

async function request(path, { method = 'GET', body, timeout = TIMEOUT.default } = {}) {
  return withBootRetry(
    () => sendOnce(API_BASE + path, { method, body, timeout }),
    method === 'GET' || method === 'HEAD',
  )
}

// ---------------------------------------------------------------- 业务接口
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

/**
 * 健康检查：用于首页提示「AI 引擎 / 高德 Key 是否就绪」。
 *
 * ⚠️ 它不在 `/api/trip` 前缀下、也不走 `request()`，但**同样要吃冷启动重试**——
 * 首屏报错的正是它和 `getCities()` 这两个。
 */
export async function health() {
  return withBootRetry(async () => {
    const r = await rawFetch(HEALTH_PATH)
    if (!r.ok) {
      throw new ApiError('HTTP ' + r.status, {
        bootRetryable: looksLikeNotUp(r.status, r.headers?.get?.('content-type'), ''),
      })
    }
    return r.json()
  }, true)
}

// 供单元测试观察内部行为（不是业务 API，视图层不要 import 这些）
export const __test__ = { BOOT, inBootWindow, looksLikeNotUp, withBootRetry }