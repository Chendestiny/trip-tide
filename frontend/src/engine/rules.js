// 排程引擎 · 判定规则（`planner.py` 的 time_rule / grade / _is_must / day_budget 镜像）
//
// 这一层全是「读字段 + 判真假」的纯函数，不碰几何，也不改数组顺序以外的状态。

import {
  DINNER_TO_HOTEL,
  EARLIEST_DINNER,
  GRADE_MEDIUM_MAX,
  GRADE_SMALL_MAX,
  MEAL_DINNER_MINUTES,
  MEAL_LUNCH_MINUTES,
  MORNING_KEYS,
  MUSEUM_KEYS,
  MUST_HEAT,
  NIGHT_KEYS,
  SPOT_ADVICE_MAX,
  STANDALONE_MINUTES,
  STANDALONE_TRAVEL_MIN,
  TIME_RULES,
} from './consts.js'
import { haversineM, leg, parseHHMM, travelFrom } from './geo.js'

/**
 * 景点是否属于「独占型」：自己就要吃掉一整天。
 *
 * ⚠️ region（环线）目的地不能拿「市中心单程」判 —— 环线横跨上千公里，
 * 每个景点离中心都远，会被全部判成独占型 → 1 天只排 1 个点
 * （实测：云南单选丽江古城+玉龙雪山，1 天被排成 1 个、另一个直接消失）。
 * 环线的参照点与 `planner.cost()` 一致：**最近的过夜基地**。
 */
export function isStandalone(city, item, transport) {
  if ((item.visit_minutes || 0) >= STANDALONE_MINUTES) return true
  if ((city.kind || 'city') === 'region' && (city.hotel_areas || []).length) {
    let base = city.hotel_areas[0]
    let best = haversineM(base.lat, base.lng, item.lat, item.lng)
    for (const h of city.hotel_areas.slice(1)) {
      const d = haversineM(h.lat, h.lng, item.lat, item.lng)
      if (d < best) {
        best = d
        base = h
      }
    }
    return leg(city, base.lat, base.lng, item.lat, item.lng, transport).minutes >= STANDALONE_TRAVEL_MIN
  }
  return travelFrom(city, item, transport) >= STANDALONE_TRAVEL_MIN
}

/** 远郊：单程通行 ≥45 分钟。与「级别」是两个独立维度。 */
export function isRemote(city, item, transport) {
  return travelFrom(city, item, transport) >= STANDALONE_TRAVEL_MIN
}

/** 「必去」判定 —— 与 materialize_day 里写 must_visit 的口径保持一致 */
export function isMust(item) {
  return Boolean(item.must_visit) || (item.heat || 0) >= MUST_HEAT
}

/** 按基础游览时长分级：小 / 中 / 大。决定「一天能放几个」。 */
export function grade(item) {
  const minutes = item.visit_minutes || 90
  if (minutes <= GRADE_SMALL_MAX) return '小'
  if (minutes <= GRADE_MEDIUM_MAX) return '中'
  return '大'
}

/**
 * 景点的时段约束，命中不了返回 null。
 * 优先读 `best_time` 字段（seed 时由 LLM 标注，换城市零改动）；
 * 字段为空时回退到名称/标签关键词，兼容早期数据。
 */
export function timeRule(item) {
  const kind = String(item.best_time || '').trim().toLowerCase()
  if (TIME_RULES[kind]) return TIME_RULES[kind]

  const text = `${item.name || ''} ${(item.tags || []).join(' ')}`
  if (MORNING_KEYS.some((k) => text.includes(k))) return TIME_RULES.morning
  if (NIGHT_KEYS.some((k) => text.includes(k))) return TIME_RULES.night
  if (MUSEUM_KEYS.some((k) => text.includes(k))) return TIME_RULES.museum
  return null
}

/** 排日顺序：早场类必须打头，夜景类必须压尾 */
export function ruleSortKey(item) {
  const rule = timeRule(item)
  if (!rule) return 1
  return { morning: 0, night: 2, museum: 1 }[rule.kind]
}

/** 稳定排序：早场类提前、夜景类推后，其余保持原有顺路顺序（JS sort 已是稳定排序） */
export function applyTimeRules(items) {
  return [...items].sort((a, b) => ruleSortKey(a) - ruleSortKey(b))
}

/**
 * 把景点的**子景点攻略**按游览顺序拼成一句 advice（**硬编码拼，不花任何模型调用**）。
 *
 * 子景点是 seed 阶段预存在库里的 —— 「从哪个门进、几点人少、有什么坑」这类知识
 * 2~3 年不变，不该每次规划都现写。取值优先级见 materializeDay：
 * **子景点拼串 > 模型写的 > 库里的 intro**。拼接只做截断，不改写原文。
 */
export function spotAdvice(item) {
  const spots = [...(item.spots || [])].sort(
    (a, b) => (a.order_index || 0) - (b.order_index || 0) || (a.id || 0) - (b.id || 0),
  )
  const parts = []
  let total = 0
  for (const s of spots) {
    const guide = String(s.guide || '').trim().replace(/。$/, '')
    if (!guide) continue
    const piece = `${s.name}：${guide}`
    // 留点余量：超时停在**上一个**完整点位，不要拼出半句
    if (parts.length && total + piece.length + 1 > SPOT_ADVICE_MAX - 10) break
    parts.push(piece)
    total += piece.length + 1
  }
  return parts.join('；').slice(0, SPOT_ADVICE_MAX)
}

/** 取某天的 (出发时间, 目标返回时间)。首末天常常和中间几天不一样。 */
export function dayWindow(req, dayIndex, totalDays) {
  let start = req.start_time
  if (dayIndex === 1 && req.first_day_start_time) start = req.first_day_start_time
  let end = req.return_time
  if (dayIndex === totalDays && req.last_day_return_time) end = req.last_day_return_time
  return [start, end]
}

/**
 * 当天要预留的用餐分钟数。
 *
 * 只有「吃完晚饭还来得及按时返回」才算上晚餐 —— 最后一天 16:00 赶返程这种，
 * 只预留午餐，materializeDay 也不会给它排晚餐。
 */
export function mealMinutes(endHHMM) {
  const latestDinner = parseHHMM(endHHMM) - (MEAL_DINNER_MINUTES + DINNER_TO_HOTEL)
  if (latestDinner >= parseHHMM(EARLIEST_DINNER)) {
    return MEAL_LUNCH_MINUTES + MEAL_DINNER_MINUTES
  }
  return MEAL_LUNCH_MINUTES
}

/**
 * 某天可用于「游览 + 赶路」的分钟数（已扣当天实际要吃的餐）。
 *
 * **不乘倾向系数** —— 出发/返回时间是用户定的硬约束，物理时间不会被「紧凑」抻长 15%。
 * 倾向的差异体现在「每天目标游玩时长」和弹性填充上。
 */
export function dayBudget(req, dayIndex = 1, totalDays = 1) {
  const [startS, endS] = dayWindow(req, dayIndex, totalDays)
  const start = parseHHMM(startS)
  let end = parseHHMM(endS)
  if (end <= start) end += 24 * 60
  return Math.max(180, end - start - mealMinutes(endS))
}
