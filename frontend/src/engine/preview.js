// 排程引擎 · 紧凑度预估 + 倾向微调（`service.preview_plan` / `service.adjust_plan` 的镜像）
//
// 两个都是**纯硬编码、毫秒级**的路径，不碰任何模型调用 —— 所以离线版原样能跑，
// 而且和联机版结果一致（差异只可能在浮点末位）。

import { PACE_TARGET_MINUTES } from './consts.js'
import { pyFixed, pyRound } from './pyfmt.js'
import {
  assignDays,
  estimateDaysNeeded,
  groupLoad,
  hotelOrigin,
  planHotels,
  pruneToCapacity,
  trimDay,
  buildDay,
  emptyDay,
} from './planner.js'
import { dayBudget, dayWindow } from './rules.js'

/**
 * 这份清单排这个天数，是偏松、刚好，还是装不下。
 *
 * **判定不看「时间占用率」**——那个指标对天数不敏感（分子分母同比例增长），
 * 永远算不出「6 天只排了 4 天的量」。现在直接比较两个天数：
 *     最少需要几天（地理下限 = 大景点/远郊各一天 + 每个片区簇一天） vs 用户设的天数
 */
export function previewPlan(city, attractions, req) {
  const days = req.days

  const [kept, willDrop] = pruneToCapacity(city, attractions, req)
  const groups = assignDays(city, kept, days, req.transport, req.pace)
  const daysNeeded = estimateDaysNeeded(city, kept, req.transport)

  let visitTotal = 0
  let travelTotal = 0
  const perDay = []
  // 逐天裁剪必须**与真实生成同口径**：trim 的往返要按「当天住宿片区」算，
  // 不能退回市中心 —— region（环线）目的地的中心到景点动辄几百公里，
  // 不传 origin 会把莫高窟这种走廊两端的景点误判成「装不下」（实测踩过）。
  const hotels = planHotels(city, groups)
  for (let idx = 1; idx <= groups.length; idx += 1) {
    const group = groups[idx - 1]
    const budget = dayBudget(req, idx, days)
    const origin = hotelOrigin(city, hotels[idx - 1])
    const [dayKept, spilled] = trimDay(city, group, req, budget, origin)
    for (const a of spilled) willDrop.push({ name: a.name, reason: '当天时间装不下', attraction_id: a.id })

    const used = dayKept.reduce((s, a) => s + (a.visit_minutes || 0), 0)
    const dayLoad = dayKept.length ? groupLoad(city, dayKept, req.transport) : 0
    visitTotal += used
    travelTotal += Math.max(0, dayLoad - used)

    const [startS] = dayWindow(req, idx, days)
    perDay.push({
      day: idx,
      count: dayKept.length,
      visit_minutes: used,
      budget_minutes: budget,
      start_time: startS,
      names: dayKept.map((a) => a.name),
    })
  }

  let capacity = 0
  for (let i = 1; i <= days; i += 1) capacity += dayBudget(req, i, days)
  const load = (visitTotal + travelTotal) / Math.max(1, capacity)

  // 判定：**勾选总量摊到每天，再和节奏目标比**。用「勾选总量」而不是「排入总量」，
  // 后者会随天数增加而增加，导致加天数时日均几乎不降，看不出松紧变化。
  const wantedVisit = attractions.reduce((s, a) => s + (a.visit_minutes || 0), 0)
  const avgVisit = wantedVisit / Math.max(1, days)
  const target = PACE_TARGET_MINUTES[req.pace] || 360
  const ratio = avgVisit / Math.max(1, target)

  let tightness = ratio >= 1.5 ? '超载' : ratio >= 1.0 ? '紧凑' : ratio >= 0.65 ? '适中' : '轻松'

  // 有景点被舍弃 → 至少算「紧凑」；丢得多了直接「超载」
  const dropRatio = willDrop.length / Math.max(1, attractions.length)
  if (dropRatio > 0.25) tightness = '超载'
  else if (dropRatio > 0 && (tightness === '轻松' || tightness === '适中')) tightness = '紧凑'

  const names = willDrop.map((d) => d.name)
  const short = names.slice(0, 3).join('、') + (names.length > 3 ? '…' : '')
  const nSel = attractions.length
  const paceLabel = req.pace_label
  const nextHint = days >= 7 ? '已经是 7 天上限，建议减少景点' : `加到 ${days + 1} 天`
  const avg = pyFixed(avgVisit, 0)

  let suggestion
  if (names.length && ratio < 1.0) {
    // 有舍弃但日均并不超标 —— 原因是「路程太远 / 单点太长」，
    // 别再说「远超目标」（日均 240 分钟却喊远超 450，自相矛盾，实测踩过）。
    const tail = `，会舍弃 ${names.length} 个（${short}）`
    suggestion =
      `${nSel} 个景点摊到 ${days} 天，日均游览约 ${avg} 分钟，本不算多${tail}。` +
      `装不下的原因是路程太远或单点耗时太长，加天数也未必解决，` +
      `建议把远郊点换成近郊的，或单独安排一天。`
  } else if (tightness === '轻松') {
    suggestion =
      `${nSel} 个景点摊到 ${days} 天，日均游览约 ${avg} 分钟，低于「${paceLabel}」的 ${target} 分钟目标 —— 安排偏松。` +
      `可以再多勾几个景点，或者把天数减少一些。`
  } else if (tightness === '适中') {
    suggestion = `${nSel} 个景点摊到 ${days} 天，日均游览约 ${avg} 分钟，接近「${paceLabel}」的 ${target} 分钟目标，节奏正常。`
  } else if (tightness === '紧凑') {
    const tail = names.length ? `，并且会舍弃 ${names.length} 个（${short}）` : ''
    suggestion =
      `${nSel} 个景点摊到 ${days} 天，日均游览约 ${avg} 分钟，高于「${paceLabel}」的 ${target} 分钟目标${tail}。` +
      `${nextHint}会舒服些。`
  } else {
    const tail = names.length ? `，会舍弃 ${names.length} 个（${short}）` : ''
    suggestion = `${nSel} 个景点摊到 ${days} 天，日均游览约 ${avg} 分钟，远超「${paceLabel}」的 ${target} 分钟目标${tail}。${nextHint}。`
  }

  return {
    city: city.name,
    days,
    pace: req.pace,
    transport: req.transport,
    selected_count: attractions.length,
    scheduled_count: kept.length,
    total_visit_minutes: visitTotal,
    total_travel_minutes: travelTotal,
    capacity_minutes: capacity,
    load_ratio: pyRound(load * 100) / 100,
    tightness,
    days_needed: daysNeeded,
    per_day: perDay,
    will_drop: names,
    suggestion,
  }
}

/**
 * 按倾向微调已有方案：**复用原来的分天，只重排时间轴**。
 *
 * 和「重新生成」的区别：
 *   · 微调是纯硬编码（毫秒级）：按新的天数/时间/倾向重排、增删景点；
 *   · 重新生成会重新算一份方案。
 * 倾向的作用：宽松把塞不下的剔掉让每天早点收工，紧凑尽量把舍弃的加回来。
 */
export function adjustPlanResult(city, allMap, oldResult, patch) {
  // 沿用原来的分天（景点 id 顺序）
  const groups = []
  for (const dp of oldResult.day_plans.slice(0, patch.days)) {
    const ids = dp.nodes.filter((n) => n.type === 'attraction' && allMap.has(n.attraction_id)).map((n) => n.attraction_id)
    groups.push(ids.map((i) => allMap.get(i)))
  }
  while (groups.length < patch.days) groups.push([])

  // 紧凑：把原先舍弃的加回来，塞进还有余量的天
  const restored = []
  if (patch.pace === 'packed') {
    for (const d of oldResult.dropped || []) {
      const aid = d.attraction_id
      if (!allMap.has(aid) || groups.some((g) => g.some((x) => x.id === aid))) continue
      const item = allMap.get(aid)
      let bestIdx = null
      let bestSlack = 0
      for (let idx = 0; idx < groups.length; idx += 1) {
        const trial = [...groups[idx], item]
        const [kept] = trimDay(city, trial, patch, dayBudget(patch, idx + 1, patch.days))
        if (kept.length === trial.length) {
          const slack =
            dayBudget(patch, idx + 1, patch.days) - kept.reduce((s, a) => s + (a.visit_minutes || 0), 0)
          if (slack > bestSlack) {
            bestIdx = idx
            bestSlack = slack
          }
        }
      }
      if (bestIdx !== null) {
        groups[bestIdx].push(item)
        restored.push(item.name)
      }
    }
  }

  // 沿用原有的主题与景点建议（微调不重写文案）
  const oldAdvice = {}
  for (const dp of oldResult.day_plans) {
    for (const n of dp.nodes) {
      if (n.type === 'attraction' && n.attraction_id && n.advice) oldAdvice[n.attraction_id] = n.advice
    }
  }

  const hotels = planHotels(city, groups)
  const dayPlans = []
  const dropped = []
  for (let idx = 1; idx <= patch.days; idx += 1) {
    const items = groups[idx - 1]
    if (!items.length) {
      dayPlans.push(emptyDay(city, patch, idx, hotels[idx - 1]))
      continue
    }
    const origin = hotelOrigin(city, hotels[idx - 1])
    const [kept, spilled] = trimDay(city, items, patch, dayBudget(patch, idx, patch.days), origin)
    for (const a of spilled) {
      dropped.push({
        name: a.name,
        reason: `按「${patch.pace_label}」倾向重排后 Day ${idx} 装不下，建议下次单独安排。`,
        attraction_id: a.id,
      })
    }
    const keptIds = new Set(kept.map((a) => a.id))
    const theme =
      oldResult.day_plans.find((dp) =>
        dp.nodes.some((n) => n.type === 'attraction' && keptIds.has(n.attraction_id)),
      )?.theme || ''
    dayPlans.push(
      buildDay(city, idx, kept, patch, null, {
        departFrom: idx >= 2 ? hotels[idx - 2] : null,
        totalDays: patch.days,
        hotel: hotels[idx - 1],
        theme,
        advice: oldAdvice,
      }),
    )
  }

  let note = ''
  if (restored.length) note = `（按「紧凑」倾向把 ${restored.slice(0, 3).join('、')} 重新排了进来）`
  else if (patch.pace === 'relaxed') note = '（按「宽松」倾向精简了每天的量，收工更早）'
  const summary = note ? `${oldResult.summary.slice(0, 280).replace(/。$/, '')}。${note}`.slice(0, 300) : oldResult.summary.slice(0, 300)

  return {
    city: city.name,
    days: patch.days,
    summary,
    day_plans: dayPlans,
    must_visit_reasons: oldResult.must_visit_reasons || [],
    dropped,
  }
}
