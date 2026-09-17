// AI 旅行搭子 本地状态：V1 不做账号体系，「我的」页历史全放 localStorage。
//
// 小程序端有一份独立实现：miniprogram/utils/store.js（wx.storage + 手写订阅）。
// 两端刻意不共享代码 —— 业务规则（上限、去重、投影）是重复的，改动要两边一起改。
//
// 未来并入 my-website 后可把 history 换成后端 /api/trip/plans，本文件对外接口保持不变。

import { reactive, computed } from 'vue'

/** storage 键。**不要改**——改了用户已有的历史行程会全部丢失 */
const HISTORY_KEY = 'triptide.history.v1'
const MAX_HISTORY = 30

function readHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch {
    // 解析失败 / localStorage 本身不可用（隐私模式等）都当空数组，不阻断页面
    return []
  }
}

function writeHistory(list) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_HISTORY)))
  } catch {
    // 配额满：砍掉一半再试一次，仍失败就放弃（不阻断主流程）
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, Math.floor(MAX_HISTORY / 2))))
    } catch { /* ignore */ }
  }
}

export const store = reactive({
  /** 当前正在展示的方案（整份 PlanResponse 快照） */
  current: null,
  /** 历史规划（最新在前） */
  history: readHistory(),
})

export const hasCurrent = computed(() => !!store.current)

/** 生成成功后落库：更新 current + 历史（同 plan_id 去重，最新在前） */
export function commitPlan(plan) {
  store.current = plan
  const rest = store.history.filter((h) => h?.plan_id !== plan?.plan_id)
  store.history = [plan, ...rest].slice(0, MAX_HISTORY)
  writeHistory(store.history)
}

/** 从历史里打开某一条 */
export function openHistory(planId) {
  const hit = store.history.find((h) => h.plan_id === planId)
  if (hit) store.current = hit
  return hit || null
}

export function removeHistory(planId) {
  store.history = store.history.filter((h) => h?.plan_id !== planId)
  writeHistory(store.history)
  if (store.current?.plan_id === planId) store.current = null
}

export function clearHistory() {
  store.history = []
  store.current = null
  writeHistory([])
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

// 注：原 savePref / loadPref（localStorage 的 triptide.pref.v1）已移除。
// 需求变更：进景点池不再恢复上次的勾选与设置，每次都是干净状态；
// 天数、节奏等在页面内仍然可用，只是不跨会话记住。
