// AI 旅行搭子 接口封装 —— 小程序端。
//
// 与 Web 版 frontend/src/trip-api.js 是**两份独立实现**（那边走 fetch，这边走 wx.request）。
// 契约（路径、超时、错误文案）是重复的，改一边记得改另一边。
//
// 注意：BASE_URL 是绝对地址（见 config.js），小程序没有 vite 代理那一层。

import { BASE_URL } from '../config.js'

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
  preview: 12000,
  plan: 150000,
  adjust: 15000,
}

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

export function request(path, { method = 'GET', body, timeout = TIMEOUT.default } = {}) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + API_BASE + path,
      method,
      data: body,
      header: body ? { 'content-type': 'application/json' } : {},
      timeout,
      success(res) {
        const { statusCode, data } = res
        if (statusCode >= 200 && statusCode < 300) {
          resolve(data)
          return
        }
        reject(new Error(normalizeErrorDetail(statusCode, data && data.detail)))
      },
      fail(err) {
        const msg = String((err && err.errMsg) || '')
        reject(new Error(
          msg.indexOf('timeout') >= 0
            ? timeoutMessage(timeout)
            : `连不上后端服务（${BASE_URL}），请确认已启动`
        ))
      },
    })
  })
}

/** 热门城市宫格（含 region 类型目的地） */
export const getCities = () => request('/cities')

/** 某目的地的景点池（后端已按 heat 降序返回） */
export const getAttractions = (city) => request(`/attractions?city=${encodeURIComponent(city)}`)

/** 某景点的内部子景点（纯静态数据，不调 LLM） */
export const getSpots = (id) => request(`/attractions/${id}/spots`)

/** 紧凑度预估：纯硬编码、毫秒级 */
export const previewPlan = (payload) =>
  request('/preview', { method: 'POST', body: payload, timeout: TIMEOUT.preview })

/** 生成行程（核心，LLM 并行流水线，15~30s） */
export const createPlan = (payload) =>
  request('/plan', { method: 'POST', body: payload, timeout: TIMEOUT.plan })

/** 一键 AI：只给城市 + 天数 + 节奏 */
export const autoPlan = (payload) =>
  request('/auto-plan', { method: 'POST', body: payload, timeout: TIMEOUT.plan })

/** 按倾向微调已有方案：纯硬编码、秒级 */
export const adjustPlan = (planId, payload) =>
  request(`/plan/${planId}/adjust`, { method: 'POST', body: payload, timeout: TIMEOUT.adjust })

/** 按 id 读取历史行程 */
export const getPlan = (id) => request(`/plan/${id}`)

/** 后端侧的历史列表 */
export const listPlans = (limit = 30) => request(`/plans?limit=${limit}`)

/** 健康检查：不在 /api/trip 前缀下，所以单独发 */
export function health() {
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + HEALTH_PATH,
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data)
        else reject(new Error('HTTP ' + res.statusCode))
      },
      fail: () => reject(new Error(`连不上后端服务（${BASE_URL}），请确认已启动`)),
    })
  })
}
