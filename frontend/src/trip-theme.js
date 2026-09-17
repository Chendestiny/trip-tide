// AI 旅行搭子 视觉常量与节点语义：地图、时间轴、图例共用一套，避免两边各写一份跑偏。
//
// 小程序端有一份独立副本：miniprogram/utils/theme.js。
// 两端刻意不共享代码 —— **改配色 / 图标时记得两边一起改**。

/** 按天配色（地图 marker / 时间轴圆点 / 图例 共用） */
export const DAY_COLORS = ['#0E7C86', '#FF7A45', '#7C5CE0', '#2E9E5B', '#D9541F', '#3B7DD8', '#B57B13']

export const dayColor = (day) => DAY_COLORS[(Math.max(1, day) - 1) % DAY_COLORS.length]

/** 时间轴节点类型 → 图标 + 中文名 */
export function nodeVisual(type, transport = 'mixed') {
  switch (type) {
    case 'depart':
      return {
        icon: { drive: '🚗', transit: '🚇' }[transport] || '🚕',
        label: '出发',
      }
    case 'attraction':
      return { icon: '📍', label: '景点' }
    case 'meal':
      return { icon: '🍜', label: '餐饮' }
    case 'hotel':
      return { icon: '🏨', label: '住宿' }
    case 'transit':
      return { icon: '🚶', label: '活动' }
    default:
      return { icon: '•', label: '' }
  }
}

/** 丰富度标签配色 */
export const paceStyle = (pace) => ({
  轻松: { bg: '#e8f5ec', fg: '#2e7d4f' },
  适中: { bg: '#e6f0f7', fg: '#2a6a8c' },
  紧凑: { bg: '#fdece4', fg: '#c05a24' },
}[pace] || { bg: '#f0f3f4', fg: '#5b6b74' })

/** 出行方式（三选，与后端 PlanRequest.transport 对齐） */
export const TRANSPORT_LABEL = {
  drive: '自驾',
  mixed: '打车+公共',
  transit: '公共交通',
  taxi: '打车+公共',   // 历史值
}

/** 节奏倾向 */
export const PACE_LABEL = {
  relaxed: '宽松',
  balanced: '平衡',
  packed: '紧凑',
}
