// AI 旅行搭子 本地状态 —— 小程序端。
//
// 与 Web 版 frontend/src/trip-store.js 是**两份独立实现**：
//   Web   → localStorage + vue 的 reactive/computed
//   小程序 → wx.setStorageSync + 下面手写的订阅（小程序没有 vue）
// 业务规则（上限、去重、投影）是重复的，改动要两边一起改。
//
// 小程序的 storage 与浏览器的 localStorage 是**两套独立命名空间**，
// 所以这里继续用同一个 key 不会和 Web 版冲突，也不会互通。

/** storage 键。**不要改**——改了用户已有的历史行程会全部丢失 */
const HISTORY_KEY = 'triptide.history.v1'
const MAX_HISTORY = 30

function readHistory() {
  try {
    const raw = wx.getStorageSync(HISTORY_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch {
    // 解析失败 / storage 不可用都当空数组，不阻断页面
    return []
  }
}

function writeHistory(list) {
  try {
    wx.setStorageSync(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY)))
  } catch {
    // 存储写满：砍掉一半再试一次，仍失败就放弃（不阻断主流程）
    try {
      wx.setStorageSync(HISTORY_KEY, JSON.stringify(list.slice(0, Math.floor(MAX_HISTORY / 2))))
    } catch { /* ignore */ }
  }
}

/** 订阅变更。页面在 onLoad 里 subscribe，onUnload 里调返回的取消函数 */
const listeners = new Set()

export function subscribe(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function emit() {
  listeners.forEach((fn) => {
    try { fn() } catch (e) { console.error('[store] 订阅回调出错', e) }
  })
}

export const store = {
  /** 当前正在展示的方案（整份 PlanResponse 快照） */
  current: null,
  /** 历史规划（最新在前） */
  history: readHistory(),
}

/** 生成成功后落库：更新 current + 历史（同 plan_id 去重，最新在前） */
export function commitPlan(plan) {
  store.current = plan
  const rest = store.history.filter((h) => h?.plan_id !== plan?.plan_id)
  store.history = [plan, ...rest].slice(0, MAX_HISTORY)
  writeHistory(store.history)
  emit()
}

/** 从历史里打开某一条 */
export function openHistory(planId) {
  const hit = store.history.find((h) => h.plan_id === planId)
  if (hit) store.current = hit
  emit()
  return hit || null
}

export function removeHistory(planId) {
  store.history = store.history.filter((h) => h?.plan_id !== planId)
  writeHistory(store.history)
  if (store.current && store.current.plan_id === planId) store.current = null
  emit()
}

export function clearHistory() {
  store.history = []
  store.current = null
  writeHistory([])
  emit()
}

/**
 * 供「我的」页展示的轻量条目。
 * 注意 `result` / `request` 都可能缺（老记录、降级记录），所以每个字段都要有兜底。
 */
export function historyBriefs() {
  return store.history.map((p) => ({
    plan_id: p?.plan_id,
    city: p?.result?.city || p?.request?.city || '—',
    days: p?.result?.days || p?.request?.days || 0,
    title: p?.title || '',
    source: p?.source,
    created_at: p?.created_at,
    attraction_count: p?.attractions?.length || 0,
    summary: p?.result?.summary || '',
  }))
}
