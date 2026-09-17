// 排程引擎 · 规则规划器（`backend/app/trip/planner.py` 的逐函数镜像）
//
// 设计原则与后端一致：所有时间都是「算出来的」而不是「编出来的」——
//     到达时间 = 上一站离开时间 + 路程耗时
//     离开时间 = 到达时间 + 停留时长
// 因此规则产出的时间轴天然自洽。
//
// 分天策略（四步，顺序不能换）：
//     ① 容量裁剪  先算总时间预算，装不下的按性价比丢掉
//     ② 独占型隔离 把「游览 ≥4h」或「单程 ≥45min」的景点单独占一天
//     ③ 成链均分  剩下的按最近邻成链，再按片区簇分天
//     ④ 逐天裁剪  每天按真实预算再裁一次，溢出的顺延到下一天
//
// ⚠️ 这是**翻译**不是**重写**：任何一处「顺手优化」都会让离线版和联机版
// 对同一份输入给出不同的行程。改之前先看 `docs/OFFLINE.md` 的一致性约束。

import {
  BIG_FLEX_MAX,
  CAPACITY_SLACK,
  CLUSTER_MAX_METERS,
  DINNER_FROM,
  DINNER_TO_HOTEL,
  EARLIEST_DINNER,
  GRADE_MEDIUM_MAX,
  LUNCH_AFTER,
  LUNCH_FROM,
  LUNCH_MAX_WAIT,
  MEAL_DINNER_MINUTES,
  MEAL_LUNCH_MINUTES,
  MIN_SPLIT_MINUTES,
  MIN_STAY_MINUTES,
  OVERNIGHT_KM,
  OVERPACK_RATIO,
  OVERRUN_TOLERANCE,
  PACE_FLEX,
  PACE_TARGET_MINUTES,
  STANDALONE_TRAVEL_MIN,
} from './consts.js'
import { fmtHHMM, haversineM, leg, parseHHMM, travelFrom } from './geo.js'
import { pyFixed, pyRound } from './pyfmt.js'
import {
  dayBudget,
  dayWindow,
  isMust,
  isStandalone,
  mealMinutes,
  spotAdvice,
  applyTimeRules,
} from './rules.js'

// ================================================================ ① 容量裁剪
/**
 * 粗估某个景点要吃掉多少时间：游览 + 往返路程。
 *
 * ⚠️ 往返的参照点**不能无脑用市中心**：region（环线）目的地横跨上千公里，
 * 「河西走廊的中心」到莫高窟往返要 +10 小时 —— 会把走廊两端的必去景点全部误判成
 * 「装不下」（实测：单选莫高窟，1 天被报「超载、会舍弃」，加到 3 天照样舍弃）。
 * 环线游的合理参照是**最近的过夜基地**（hotel_areas 就是为此存在的），
 * 与 planFallback / adjustPlanResult 里「当天从哪个基地出发」的口径一致。
 */
const cost = (city, item, transport) => {
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
    return Math.trunc(
      (item.visit_minutes || 0) + 2 * leg(city, base.lat, base.lng, item.lat, item.lng, transport).minutes,
    )
  }
  return Math.trunc((item.visit_minutes || 0) + 2 * travelFrom(city, item, transport))
}

/** 性价比：单位时间能换来多少「值得去」。必去景点给加成。 */
const value = (city, item, transport) =>
  ((item.heat || 0) + (isMust(item) ? 30 : 0)) / Math.max(1, cost(city, item, transport))

function dropReason(city, item, transport, days) {
  const t = travelFrom(city, item, transport)
  const hours = (item.visit_minutes || 0) / 60
  const h = pyFixed(hours, 1)
  if (isStandalone(city, item, transport)) {
    if (t >= STANDALONE_TRAVEL_MIN) {
      return `单程约 ${t} 分钟、游览 ${h} 小时，基本要占掉一整天；${days} 天预算装不下市区线 + 远郊，按性价比取舍后舍弃。`
    }
    return `游览 ${h} 小时，本身就占掉大半天；${days} 天预算装不下，按性价比取舍后舍弃（建议单独留半天再来）。`
  }
  return `${days} 天装不下全部勾选景点，按「热度 ÷ 耗时」排序后它排在最后，取舍后舍弃。`
}

/**
 * 总时长超容量时，按性价比从低到高丢弃，直到装得下。
 * 先裁再排，是为了避免「排到最后一天才发现塞不下，结果把必去景点丢了」。
 */
export function pruneToCapacity(city, attractions, req, capacityMinutes = null) {
  const kept = [...attractions]
  const dropped = []
  let capacity = capacityMinutes
  if (capacity === null || capacity === undefined) {
    let sum = 0
    for (let i = 1; i <= req.days; i += 1) sum += dayBudget(req, i, req.days)
    capacity = sum
  }
  const cap = Math.trunc(capacity * CAPACITY_SLACK)

  while (kept.length > 1) {
    const total = kept.reduce((s, a) => s + cost(city, a, req.transport), 0)
    if (total <= cap) break
    // 先丢普通景点；普通景点丢完了仍装不下，才轮到必去
    const pool = kept.some((a) => !isMust(a)) ? kept.filter((a) => !isMust(a)) : kept
    let victim = pool[0]
    for (const a of pool) {
      if (value(city, a, req.transport) < value(city, victim, req.transport)) victim = a
    }
    kept.splice(kept.indexOf(victim), 1)
    dropped.push({
      name: victim.name,
      reason: dropReason(city, victim, req.transport, req.days),
      attraction_id: victim.id,
    })
  }
  return [kept, dropped]
}

// ================================================================ ②③ 排序与分天
/** 从 start 出发的最近邻贪心链——「顺路」的最朴素实现 */
function nnChain(items, start) {
  const remaining = items.filter((x) => x !== start)
  const chain = [start]
  let cur = start
  while (remaining.length) {
    let nxt = remaining[0]
    for (const x of remaining) {
      if (haversineM(cur.lat, cur.lng, x.lat, x.lng) < haversineM(cur.lat, cur.lng, nxt.lat, nxt.lng)) {
        nxt = x
      }
    }
    chain.push(nxt)
    remaining.splice(remaining.indexOf(nxt), 1)
    cur = nxt
  }
  return chain
}

/**
 * 取数组中 key 最大的元素，**并列取第一个**（对齐 Python max() 的语义）。
 *
 * ⚠️ key 允许返回**数组**（对应 Python 的元组 key，如 `(a.heat, a.visit_minutes)`）。
 * 千万别直接写 `key(a) > key(b)` —— JS 的数组比较会退化成**字符串**比较，
 * `[560] > [98]` 会被算成 `"560" > "98"` = false，选出的锚点完全是错的。
 */
function argMax(items, key) {
  const asArr = (k) => (Array.isArray(k) ? k : [k])
  const gt = (x, y) => {
    const a = asArr(x)
    const b = asArr(y)
    for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
      const u = a[i] ?? 0
      const v = b[i] ?? 0
      if (u > v) return true
      if (u < v) return false
    }
    return false
  }
  let best = items[0]
  let bestK = key(best)
  for (const x of items.slice(1)) {
    const k = key(x)
    if (gt(k, bestK)) {
      best = x
      bestK = k
    }
  }
  return best
}

/** 两景点之间的通行分钟数——分天矩阵的最小单元 */
export function travelMinutes(city, a, b, transport) {
  return leg(city, a.lat, a.lng, b.lat, b.lng, transport).minutes
}

/**
 * 按**直线距离**把景点聚成「片区簇」——步行可达的归为一簇。
 * 先最近邻成链（保证地理顺路），再在「相邻直线距离 > threshold」处断开。
 *
 * **簇是分天的原子单位，簇内景点不会被拆到不同天** —— 所以像
 * 锦里古街↔武侯祠（实测 0.24km、步行 5 分钟）这种组合必然落在同一天。
 */
export function clusterAttractions(city, attractions, transport, thresholdM = CLUSTER_MAX_METERS) {
  const items = [...attractions]
  if (!items.length) return []
  if (items.length === 1) return [items]

  const anchor = argMax(items, (a) => [a.heat, a.visit_minutes])
  const chain = nnChain(items, anchor)

  const clusters = [[chain[0]]]
  for (let i = 1; i < chain.length; i += 1) {
    const prev = chain[i - 1]
    const item = chain[i]
    if (haversineM(prev.lat, prev.lng, item.lat, item.lng) <= thresholdM) clusters[clusters.length - 1].push(item)
    else clusters.push([item])
  }
  return clusters
}

/**
 * 把簇切成两半：两侧游览时长尽量接近，但**绝不在紧邻对处切**。
 * 切点必须落在「相邻直线距离 > threshold」的真断点上；若整个簇在阈值内串成一片，
 * 返回 null 表示它是一个地理整体、不该拆。
 */
function splitCluster(city, group, transport, thresholdM = CLUSTER_MAX_METERS) {
  if (group.length < 2) return null
  const chain = nnChain(group, argMax(group, (a) => [a.heat, a.visit_minutes]))
  const total = chain.reduce((s, a) => s + (a.visit_minutes || 0), 0)

  let bestCut = null
  let bestScore = null
  let acc = 0
  for (let k = 1; k < chain.length; k += 1) {
    acc += chain[k - 1].visit_minutes || 0
    const gapM = haversineM(chain[k - 1].lat, chain[k - 1].lng, chain[k].lat, chain[k].lng)
    if (gapM <= thresholdM) continue // 紧邻对，不许切
    const score = Math.abs(acc - (total - acc)) // 两边时长越接近越好
    if (bestScore === null || score < bestScore) {
      bestCut = k
      bestScore = score
    }
  }
  if (bestCut === null) return null
  return [chain.slice(0, bestCut), chain.slice(bestCut)]
}

/**
 * 一组景点排成一天要占用多少分钟 = **游览 + 路程**（含从市中心往返）。
 * 只算游览会严重低估：远郊景点游览 1 小时、往返却要 3 小时。
 */
export function groupLoad(city, group, transport) {
  const items = [...group]
  if (!items.length) return 0
  const visit = items.reduce((s, a) => s + (a.visit_minutes || 0), 0)
  let travel = 0
  let lat = city.center_lat
  let lng = city.center_lng
  for (const a of items) {
    travel += leg(city, lat, lng, a.lat, a.lng, transport).minutes
    lat = a.lat
    lng = a.lng
  }
  travel += leg(city, lat, lng, city.center_lat, city.center_lng, transport).minutes
  return visit + travel
}

/**
 * 把片区簇分配到 days 天。簇是原子——只有必要时才合并或拆分。
 * 簇太多 → 合并「地理最近」的两簇；簇太少 → 只拆「内部有真断点」的簇；
 * 全都紧凑时**宁可让某些天空着**，也不违反「地理优先」把紧邻景点拆开。
 */
function allocClustersToDays(city, clusters, days, transport, targetMinutes) {
  const groups = clusters.filter((c) => c && c.length).map((c) => [...c])
  if (!groups.length) return Array.from({ length: days }, () => [])
  const size = (g) => groupLoad(city, g, transport)

  while (groups.length > days) {
    let bestI = 0
    let bestScore = null
    for (let i = 0; i < groups.length - 1; i += 1) {
      const a = groups[i]
      const b = groups[i + 1]
      let gap = Infinity
      for (const x of a) for (const y of b) gap = Math.min(gap, haversineM(x.lat, x.lng, y.lat, y.lng))
      const over = Math.max(0, size(a) + size(b) - targetMinutes)
      const score = [over, gap]
      if (bestScore === null || score[0] < bestScore[0] || (score[0] === bestScore[0] && score[1] < bestScore[1])) {
        bestI = i
        bestScore = score
      }
    }
    groups[bestI] = [...groups[bestI], ...groups[bestI + 1]]
    groups.splice(bestI + 1, 1)
  }

  while (groups.length < days) {
    const cands = []
    for (let i = 0; i < groups.length; i += 1) {
      const split = splitCluster(city, groups[i], transport)
      if (split) cands.push([groups[i].reduce((s, a) => s + (a.visit_minutes || 0), 0), i, split])
    }
    if (!cands.length) break
    const best = argMax(cands, (x) => x[0]) // 优先拆时长最大的
    groups.splice(best[1], 1, best[2][0], best[2][1])
  }

  while (groups.length < days) groups.push([])
  return groups.slice(0, days)
}

/**
 * 把景点分配到每一天。**地理优先**：片区簇是骨架，簇内不可拆。
 * 规则（顺序不能换）：
 *   ① 大景点 / 远郊 → 独占一天
 *   ② 其余按通行时间聚成片区簇
 *   ③ 簇分配到天：簇太多就合并，太少只在「真断点」处拆
 *   ④ 天与天按地理顺路排序，减少跨天折返
 *   ⑤ 每天内部应用时段硬规则（早场打头、夜景压尾）
 */
export function assignDays(city, attractions, days, transport, pace = 'balanced') {
  if (!attractions.length) return Array.from({ length: days }, () => [])

  let standalone = attractions.filter((a) => isStandalone(city, a, transport))
  const standaloneIds = new Set(standalone.map((a) => a.id))
  const normal = attractions.filter((a) => !standaloneIds.has(a.id))

  standalone.sort((a, b) => b.heat - a.heat || b.visit_minutes - a.visit_minutes)
  if (standalone.length > days) {
    // 独占型比天数还多，多出来的回到普通池，交给容量裁剪/逐天裁剪处理
    normal.push(...standalone.slice(days))
    standalone = standalone.slice(0, days)
  }

  const groups = standalone.map((a) => [a])
  const leftDays = days - groups.length

  if (normal.length) {
    if (leftDays > 0) {
      const clusters = clusterAttractions(city, normal, transport)
      const target = PACE_TARGET_MINUTES[pace] || PACE_TARGET_MINUTES.balanced
      groups.push(...allocClustersToDays(city, clusters, leftDays, transport, target))
    } else {
      // 没位置了，塞进最后一个独占日，随后由逐天裁剪决定去留
      groups[groups.length - 1].push(...normal)
    }
  }

  while (groups.length < days) groups.push([])

  const out = groups.slice(0, days)
  // 让第 k 天从「离上一天收尾最近」的那端开始，减少跨天折返
  for (let k = 1; k < out.length; k += 1) {
    const prev = out[k - 1]
    const cur = out[k]
    if (cur.length < 2 || !prev.length) continue
    const tail = prev[prev.length - 1]
    const headGap = haversineM(tail.lat, tail.lng, cur[0].lat, cur[0].lng)
    const revGap = haversineM(tail.lat, tail.lng, cur[cur.length - 1].lat, cur[cur.length - 1].lng)
    if (revGap < headGap) out[k] = [...cur].reverse()
  }
  // 时段硬约束：早场类必须打头，夜景类必须压尾
  return out.map((g) => applyTimeRules(g))
}

/**
 * 一键 AI：按「天数 + 节奏」自动挑选景点，用户不必逐个勾选。
 * 原则与分天一致——**必去优先，其次热度**，累计游览时长逼近
 * 「天数 × 节奏目标游玩时长」，超出 10% 后收口。
 */
export function pickAttractions(city, attractions, days, pace = 'balanced', transport = 'mixed') {
  const items = [...attractions]
  if (!items.length || days <= 0) return []

  const target = days * (PACE_TARGET_MINUTES[pace] || PACE_TARGET_MINUTES.balanced)
  const ordered = [...items].sort(
    (a, b) => (a.must_visit ? 0 : 1) - (b.must_visit ? 0 : 1) || b.heat - a.heat,
  )

  const picked = []
  let acc = 0
  for (const a of ordered) {
    if (picked.length && acc + (a.visit_minutes || 0) > target * 1.1) break
    // 独占型一天只能放一个，超出天数就别再选了
    if (isStandalone(city, a, transport) && picked.length >= days) continue
    picked.push(a)
    acc += a.visit_minutes || 0
  }
  return picked.length ? picked : items.slice(0, 1)
}

/** 「按片区排最舒服需要几天」——用于给用户提示，**不用作硬判定** */
export function estimateDaysNeeded(city, attractions, transport) {
  const items = [...attractions]
  if (!items.length) return 0
  const standalone = items.filter((a) => isStandalone(city, a, transport))
  const sids = new Set(standalone.map((a) => a.id))
  const normal = items.filter((a) => !sids.has(a.id))
  const clusters = normal.length ? clusterAttractions(city, normal, transport) : []
  return standalone.length + clusters.length
}

// ================================================================ ④ 逐天裁剪
/**
 * 按当天真实时间预算裁剪，返回 [保留, 溢出]。
 * 预算里含「最后一站回住宿片区」的返程时间——不算返程的话，
 * 远郊景点会看着能塞进去，实际收尾时间要晚一个多小时。
 * 至少保留 1 个（否则一整天就空了）。
 */
export function trimDay(city, items, req, perDayBudget, origin = null, depart = null) {
  const kept = []
  const overflow = []
  let used = 0
  const [olat, olng] = origin || [city.center_lat, city.center_lng]
  const [dlat, dlng] = depart || [olat, olng]
  let lat = dlat
  let lng = dlng

  for (const item of items) {
    const t = leg(city, lat, lng, item.lat, item.lng, req.transport).minutes
    const back = leg(city, item.lat, item.lng, olat, olng, req.transport).minutes
    const c = t + (item.visit_minutes || 0) + back
    if (used + t + (item.visit_minutes || 0) + back > perDayBudget) {
      // 已经排了东西 → 溢出。一个都还没排时：只有「单个景点就远超预算」才放弃
      if (kept.length || c > perDayBudget * OVERPACK_RATIO) {
        overflow.push(item)
        continue
      }
    }
    used += t + (item.visit_minutes || 0)
    lat = item.lat
    lng = item.lng
    kept.push(item)
  }
  return [kept, overflow]
}

// ================================================================ 住宿取舍
/** 选离这组景点几何中心最近的住宿片区 */
export function pickHotel(city, attractions) {
  const areas = [...(city.hotel_areas || [])]
  if (!areas.length) {
    return {
      area: `${city.name}市中心`,
      reason: '未配置候选住宿片区，默认建议住在市中心，便于向各方向辐射。',
      alternatives: [],
    }
  }
  const avgDistance = (area) =>
    attractions.length
      ? attractions.reduce((s, a) => s + haversineM(area.lat, area.lng, a.lat, a.lng), 0) / attractions.length
      : 0

  const ranked = [...areas].sort((a, b) => avgDistance(a) - avgDistance(b))
  const best = ranked[0]
  const distKm = avgDistance(best) / 1000.0

  let reason = `到本次各景点平均单程约 ${pyFixed(distKm, 1)} km，位置最居中。`
  if (best.pros) reason += String(best.pros)

  return {
    area: String(best.area),
    reason,
    alternatives: ranked.slice(1, 3).map((x) => ({
      area: String(x.area),
      tradeoff: String(x.cons || '位置略偏，但周边环境/价格可能更优。'),
    })),
  }
}

/** 住宿片区的坐标；找不到就退回市中心 */
function hotelOrigin(city, hotel) {
  for (const area of city.hotel_areas || []) {
    if (area.area === hotel.area) return [area.lat, area.lng]
  }
  return [city.center_lat, city.center_lng]
}

/** 某住宿片区到一组景点的平均直线距离（km） */
function avgKmHotel(city, hotel, items) {
  const [hlat, hlng] = hotelOrigin(city, hotel)
  if (!items.length) return 0.0
  return items.reduce((s, a) => s + haversineM(hlat, hlng, a.lat, a.lng), 0) / items.length / 1000.0
}

/**
 * 一次定**全程**住宿。城市目的地与区域游目的地的策略不同：
 *   city   按「全程景点的几何中心」选一个常住片区，只有远郊日才建议就近住一晚
 *   region **今晚住哪 = 今天走到哪**：连住优先（不搬行李），
 *          当天景点离昨晚过夜点太远且本地有明显更优基地时才搬
 */
export function planHotels(city, groups) {
  const allItems = groups.flat()
  const base = pickHotel(city, allItems)
  if ((city.kind || 'city') === 'region') return planHotelsRegion(city, groups, base)

  const [baseLat, baseLng] = hotelOrigin(city, base)
  const out = []
  for (const items of groups) {
    if (!items.length) {
      out.push(base)
      continue
    }
    const avgKm =
      items.reduce((s, a) => s + haversineM(baseLat, baseLng, a.lat, a.lng), 0) / items.length / 1000.0
    if (avgKm <= OVERNIGHT_KM) {
      out.push(base)
      continue
    }
    // 当天离常住片区太远 → 看看本地有没有更合适的片区
    const local = pickHotel(city, items)
    const localKm = avgKmHotel(city, local, items)
    if (local.area !== base.area && localKm < avgKm * 0.6) {
      out.push({
        area: local.area,
        reason: `当天在「${local.area}」一带，离常住片区「${base.area}」平均 ${pyFixed(avgKm, 0)} km，往返太远，建议就近住一晚（本地片区平均单程 ${pyFixed(localKm, 1)} km）。`,
        alternatives: [{ area: base.area, tradeoff: '不用搬行李，但当天来回要多花 2~3 小时车程。' }],
      })
    } else {
      out.push({
        area: base.area,
        reason: `继续住「${base.area}」（全程不换酒店，省去搬行李）。注意当天景点平均单程 ${pyFixed(avgKm, 0)} km，建议早出发、当天行程别再加密。`,
        alternatives: base.alternatives,
      })
    }
  }
  return out
}

/** 区域游的推进式住宿：链式转场的核心（规则见 planHotels 注释） */
function planHotelsRegion(city, groups, base) {
  const out = []
  let cur = null
  for (const items of groups) {
    if (!items.length) {
      out.push(cur || base)
      continue
    }
    const local = pickHotel(city, items)
    if (cur === null) {
      cur = local // ① 第一晚：住第一天景点就近的基地
      out.push(cur)
      continue
    }
    const curKm = avgKmHotel(city, cur, items)
    if (curKm <= OVERNIGHT_KM) {
      out.push({ area: cur.area, reason: `继续住「${cur.area}」，离今天的景点近，不搬行李。`, alternatives: cur.alternatives })
      continue
    }
    const localKm = avgKmHotel(city, local, items)
    if (local.area !== cur.area && localKm < curKm * 0.6) {
      const prevArea = out[out.length - 1].area
      cur = {
        area: local.area,
        reason: `今天玩「${local.area}」一带，从「${prevArea}」过去平均 ${pyFixed(curKm, 0)} km，今晚搬过来住（本地平均单程 ${pyFixed(localKm, 1)} km），明天从这里出发。`,
        alternatives: [{ area: prevArea, tradeoff: '不搬行李，但明天一早要多花 2 小时以上车程折返。' }],
      }
    } else {
      cur = {
        area: cur.area,
        reason: `继续住「${cur.area}」硬跑今天这一线（换住省不了多少路）。当天平均单程 ${pyFixed(curKm, 0)} km，务必早出发，行程别再加密。`,
        alternatives: cur.alternatives,
      }
    }
    out.push(cur)
  }
  return out
}

// ================================================================ 单日构建
/**
 * 补齐节点字段。
 *
 * 后端的 `TimelineNode` 是 Pydantic 模型，**没显式给的字段会自动带上默认值**
 * （`travel_mode=""`、`must_visit=false`、`attraction_id=null`…）。JS 的普通对象不会，
 * 缺键在 JSON 里直接消失，视图层读到的就是 `undefined`。所以这里补一次，
 * 让两边的节点形状**逐键一致**。
 */
const NODE_DEFAULTS = {
  time: '',
  type: 'transit',
  name: '',
  advice: '',
  attraction_id: null,
  stay_minutes: 0,
  travel_minutes: 0,
  travel_mode: '',
  must_visit: false,
  moved_to_day: null,
  moved_from_day: null,
  move_reason: '',
}
const normNode = (o) => ({ ...NODE_DEFAULTS, ...o })

/**
 * 餐饮节点不写具体店名——店铺随时会换，写了反而误导。
 * 改为「区域 + 本地特色」，具体吃什么放在 advice 里。
 */
function mealName(cityName, area, kind) {
  const where = String(area || '').trim() || `${cityName}市区`
  return `${where}·本地特色${kind === 'lunch' ? '午餐' : '晚餐'}`
}

function mealAdvice(kind, hint, area) {
  const base =
    kind === 'lunch'
      ? '就近在当天片区解决，别为了一顿饭跨区，会打乱下午的动线。'
      : '晚餐安排在住宿片区附近，吃完直接回酒店，不用再折腾交通。'
  if (hint) return `推荐本地特色：${hint}。${base}`
  if (area) return `在「${area}」一带找家本地小馆即可。${base}`
  return base
}

function makeTheme(cityName, items) {
  if (!items.length) return `${cityName}市区自由漫步 · 机动日`
  const names = items.map((a) => a.name)
  const districts = items.map((a) => a.district).filter(Boolean)
  let prefix
  if (districts.length && new Set(districts).size === 1) prefix = `${districts[0]}深度线`
  else if (districts.length) prefix = `${districts[0]}—${districts[districts.length - 1]} 顺路线`
  else prefix = `${cityName}市区经典线`
  return `${prefix}｜${names.slice(0, 3).join(' → ')}`.slice(0, 80)
}

/** 按给定顺序预估当天赶路总时长（含最后一站回住宿片区） */
function estimateTravel(city, items, originLat, originLng, transport, endLat = null, endLng = null) {
  const eLat = endLat === null || endLat === undefined ? originLat : endLat
  const eLng = endLng === null || endLng === undefined ? originLng : endLng
  let total = 0
  let lat = originLat
  let lng = originLng
  for (const a of items) {
    total += leg(city, lat, lng, a.lat, a.lng, transport).minutes
    lat = a.lat
    lng = a.lng
  }
  total += leg(city, lat, lng, eLat, eLng, transport).minutes
  return total
}

/**
 * 当天游玩时长的放大倍数——**弹性填充**。
 *
 *     最终游玩 = clamp(可用游玩 × r, 基础游玩, 基础游玩 × m)
 *
 * r / m 按节奏取；**大景点与远郊不受节奏限制**。
 */
function flexScale(city, items, req, baseTotal, available) {
  if (baseTotal <= 0 || available <= 0) return 1.0
  const [ratio, cap0] = PACE_FLEX[req.pace] || PACE_FLEX.balanced
  let cap = cap0
  const hasBig = items.some(
    (a) => a.visit_minutes > GRADE_MEDIUM_MAX || travelFrom(city, a, req.transport) >= STANDALONE_TRAVEL_MIN,
  )
  if (hasBig) cap = Math.max(cap, BIG_FLEX_MAX)
  return Math.max(1.0, Math.min((available * ratio) / baseTotal, cap))
}

/**
 * 把「当天去哪几个景点」物化成完整时间轴。
 *
 * 这是全项目唯一产出时间的地方：所有 time / stay_minutes / travel_minutes /
 * 饭点位置 / pace 都由本函数算，所以不可能算错时间、漏插饭点或把景点排两遍。
 * items 必须已经过 trimDay 裁剪，本函数不再做容量判断。
 */
export function materializeDay(city, dayIndex, items, req, opts = {}) {
  const totalDays = opts.totalDays || dayIndex
  const movedFrom = opts.movedFrom || {}
  const adviceMap = opts.advice || {}
  const { lunchArea = '', lunchHint = '', dinnerArea = '', dinnerHint = '' } = opts

  const [startS, endS] = dayWindow(req, dayIndex, totalDays)
  const startMin = parseHHMM(startS)
  let returnMin = parseHHMM(endS)
  if (returnMin <= startMin) returnMin += 24 * 60 // 允许跨零点

  const hotel = opts.hotel || pickHotel(city, items)
  const [originLat, originLng] = hotelOrigin(city, hotel) // 今晚的住处（收尾点）
  const depHotel = opts.departFrom || hotel
  const [departLat, departLng] = hotelOrigin(city, depHotel)
  const isTransit = depHotel.area !== hotel.area

  const nodes = []
  let curTime = startMin
  let curLat = departLat
  let curLng = departLng

  /** 所有节点统一从这里进——保证「到达 = 上一站离开 + 路程」永远成立 */
  const push = (node) => {
    const full = normNode(node)
    nodes.push(full)
    curTime = parseHHMM(full.time) + full.stay_minutes
  }

  /** 插一个餐饮节点。gap 不传则按 at - curTime 反推，时间轴依然自洽。 */
  const addMeal = (kind, at, district, gap = null) => {
    const stay = kind === 'lunch' ? MEAL_LUNCH_MINUTES : MEAL_DINNER_MINUTES
    const area = kind === 'lunch' ? lunchArea : dinnerArea
    const hint = kind === 'lunch' ? lunchHint : dinnerHint
    // 晚餐回到住宿片区吃；午餐用当天所在的区
    const where = area || (kind === 'dinner' ? hotel.area : district)
    const g = gap === null ? Math.max(0, at - curTime) : gap
    push({
      time: fmtHHMM(at),
      type: 'meal',
      name: mealName(city.name, where, kind),
      advice: mealAdvice(kind, hint, where),
      stay_minutes: stay,
      travel_minutes: g,
    })
  }

  /** 压入一个景点节点。大景点跨午饭窗口时会压两个（上午段 + 下午段）。 */
  const pushAttr = (at, item, stay, travel, mode, moved, resumed = false) => {
    push({
      time: fmtHHMM(at),
      type: 'attraction',
      name: resumed ? `${item.name}（下午继续）` : item.name,
      attraction_id: item.id,
      // 建议的取值优先级：**子景点拼串（硬编码） > 模型写的 > 库里的 intro**
      advice: spotAdvice(item) || String(adviceMap[item.id] || '').trim() || item.intro || '建议留足时间慢慢逛。',
      stay_minutes: stay,
      travel_minutes: travel,
      travel_mode: resumed ? '步行' : mode,
      must_visit: isMust(item),
      moved_to_day: moved ? dayIndex : null,
      moved_from_day: moved ? moved.from_day : null,
      move_reason: moved ? moved.reason : '',
    })
  }

  push({
    time: fmtHHMM(startMin),
    type: 'depart',
    name: isTransit ? `从「${depHotel.area}」退房出发` : `从「${depHotel.area}」出发`,
    advice: isTransit
      ? `转场日：昨晚住在「${depHotel.area}」，今天先赶路去「${hotel.area}」一带，行李带上车，下午游玩后直接入住新住处。`
      : { drive: '自驾建议提前确认停车场位置，景区周边车位紧张。', transit: '地铁早高峰较挤，避开 8:00-9:00 换乘大站。' }[
          req.transport
        ] || '早高峰建议提前 10 分钟叫车，先到最远的一站再往回走。',
    stay_minutes: 0,
    travel_minutes: 0,
  })

  // ---------- 弹性游玩时长 ----------
  const baseTotal = items.reduce((s, a) => s + (a.visit_minutes || 0), 0)
  const travelEst = estimateTravel(city, items, departLat, departLng, req.transport, originLat, originLng)
  const win = returnMin - startMin
  const available = win - mealMinutes(endS) - travelEst
  const scale = flexScale(city, items, req, baseTotal, available)
  const stretch = (item) => Math.max(5, Math.trunc((pyRound(((item.visit_minutes || 0) * scale) / 5.0) * 5)))

  let lunchDone = false
  for (const item of items) {
    const go = leg(city, curLat, curLng, item.lat, item.lng, req.transport)
    let travel = go.minutes
    let arrive = curTime + travel

    // 午饭要落在 11:20-14:00 之间。若这个景点会把午饭挤到 14:00 之后，就「到了先吃」；
    // 但等待超过 75 分钟就不值得等，那种情况改为下来后再吃。
    // ⚠️ 例外：「下来后再吃」如果会拖到 15:00 之后，宁可等 —— 实测西疆 Day6 出现过
    // 10:00 到达、拒绝等 80 分钟、结果午餐 16:40（回归红线 11:00~16:00，踩过）。
    if (!lunchDone) {
      const wait = Math.max(0, parseHHMM(LUNCH_FROM) - arrive)
      const after = arrive + stretch(item)
      if (after > parseHHMM('14:00') && (wait <= LUNCH_MAX_WAIT || after > parseHHMM('15:00'))) {
        addMeal('lunch', Math.max(arrive, parseHHMM(LUNCH_FROM)), item.district)
        lunchDone = true
        travel = 5
        arrive = curTime + travel
      }
    }

    const moved = movedFrom[item.id]
    const stay = stretch(item)
    const lunchAt = parseHHMM(LUNCH_FROM)

    // 弹性拉长后，大景点的游玩会跨过午饭窗口。这时把午饭插在**景点中途**
    // （上午段 → 午餐 → 下午段），而不是「玩完再吃」。
    const morning = lunchAt - arrive
    const afternoon = stay - morning
    if (!lunchDone && arrive < lunchAt && lunchAt < arrive + stay && morning >= MIN_SPLIT_MINUTES && afternoon >= MIN_SPLIT_MINUTES) {
      const backToSpot = 5 // 从餐厅走回景区只需要步行
      pushAttr(arrive, item, morning, travel, go.label, moved)
      curLat = item.lat
      curLng = item.lng
      addMeal('lunch', lunchAt, item.district, 0)
      lunchDone = true
      pushAttr(curTime + backToSpot, item, afternoon, backToSpot, go.label, moved, true)
      curLat = item.lat
      curLng = item.lng
      continue
    }

    pushAttr(arrive, item, stay, travel, go.label, moved)
    curLat = item.lat
    curLng = item.lng

    // 这个景点跨过了午饭点 → 出来立刻吃，别拖到下一站
    if (!lunchDone && curTime >= parseHHMM(LUNCH_AFTER)) {
      addMeal('lunch', curTime, item.district)
      lunchDone = true
    }
  }

  // 午餐兜底：一天都在赶路没吃上
  if (!lunchDone && items.length) {
    addMeal('lunch', Math.max(curTime, parseHHMM('12:00')), items[items.length - 1].district)
  }

  // ---------- 收尾前的硬约束：绝不超目标返回时间 ----------
  // 只有在「最后一天 **且** 用户显式设了返回时间」时才当作赶返程的硬约束。
  const strictReturn = dayIndex >= totalDays && Boolean(req.last_day_return_time)
  const backProbe = leg(city, curLat, curLng, originLat, originLng, req.transport).minutes
  if (strictReturn && curTime + backProbe > returnMin) {
    const excess = curTime + backProbe - returnMin
    let last = null
    for (let i = nodes.length - 1; i >= 0; i -= 1) {
      if (nodes[i].type === 'attraction') {
        last = nodes[i]
        break
      }
    }
    if (last) {
      const cut = Math.min(excess, Math.max(0, last.stay_minutes - MIN_STAY_MINUTES))
      if (cut > 0) {
        last.stay_minutes -= cut
        const note = `（为按时返回压缩 ${cut} 分钟）`
        last.advice = (last.advice.slice(0, Math.max(0, 200 - note.length)) + note).trim()
        curTime -= cut
      }
    }
  }

  // ---------- 收尾：晚餐 → 回住宿片区 ----------
  const backLeg = leg(city, curLat, curLng, originLat, originLng, req.transport)
  const back = backLeg.minutes
  const arriveBack = curTime + back

  const latestDinner = returnMin - (MEAL_DINNER_MINUTES + DINNER_TO_HOTEL)
  let dinnerAt = null
  if (latestDinner >= parseHHMM(EARLIEST_DINNER)) {
    dinnerAt = Math.max(arriveBack, Math.min(parseHHMM(DINNER_FROM), latestDinner))
    if (strictReturn && dinnerAt + MEAL_DINNER_MINUTES + DINNER_TO_HOTEL > returnMin) dinnerAt = null
  }

  if (dinnerAt === null) {
    push({
      time: fmtHHMM(arriveBack),
      type: 'hotel',
      name: `返回「${hotel.area}」，准备返程`,
      advice: `${hotel.reason} 今天的目标是 ${fmtHHMM(returnMin)} 前返回，行程到此收尾。`,
      stay_minutes: 0,
      travel_minutes: back,
      travel_mode: backLeg.label,
    })
  } else {
    // 下午早早收工 → 补一个留白节点，否则时间轴上会凭空缺好几个小时
    if (dinnerAt - arriveBack > 90) {
      push({
        time: fmtHHMM(arriveBack),
        type: 'transit',
        name: '返回住宿片区休整 · 自由活动',
        advice: '这一段刻意留白：可以回酒店歇脚，也可以临时加一个住宿片区附近的去处。',
        stay_minutes: dinnerAt - arriveBack,
        travel_minutes: back,
        travel_mode: backLeg.label,
      })
      addMeal('dinner', dinnerAt, '', 0)
    } else {
      addMeal('dinner', dinnerAt, '')
    }

    const hotelAt = curTime + DINNER_TO_HOTEL
    const overrun = hotelAt - returnMin
    let advice = hotel.reason
    if (overrun > OVERRUN_TOLERANCE) {
      advice += ` 注意：当天路程较远，实际回到酒店约 ${fmtHHMM(hotelAt)}，比目标时间晚 ${overrun} 分钟，建议这天的行程不要再加东西。`
    }
    push({
      time: fmtHHMM(hotelAt),
      type: 'hotel',
      name: `入住「${hotel.area}」`,
      advice,
      stay_minutes: 0,
      travel_minutes: DINNER_TO_HOTEL,
      travel_mode: '步行',
    })
  }

  // 丰富度：只看「真实活动」——景点停留 + 全部赶路。必须排除留白节点。
  const active =
    nodes.filter((n) => n.type !== 'transit').reduce((s, n) => s + n.stay_minutes, 0) +
    nodes.reduce((s, n) => s + n.travel_minutes, 0)
  const pace = active <= 300 ? '轻松' : active <= 450 ? '适中' : '紧凑'

  return {
    day: dayIndex,
    theme: opts.theme || makeTheme(city.name, items),
    pace,
    nodes,
    hotel,
  }
}

/** 纯规则模式下的单日构建（不带大纲） */
export const buildDay = (city, dayIndex, items, req, movedFrom = null, kwargs = {}) =>
  materializeDay(city, dayIndex, items, req, { ...kwargs, movedFrom })

/** 没有任何景点的一天（机动日）。时间轴仍然是自洽的。 */
export function emptyDay(city, req, dayIndex, hotel) {
  const [startS, endS] = dayWindow(req, dayIndex, req.days)
  const startMin = parseHHMM(startS)
  let endMin = parseHHMM(endS)
  if (endMin <= startMin) endMin += 24 * 60

  const nodes = []
  let cur = startMin
  const push = (node) => {
    const full = normNode(node)
    nodes.push(full)
    cur = parseHHMM(full.time) + full.stay_minutes
  }

  push({
    time: fmtHHMM(cur),
    type: 'depart',
    name: `从「${hotel.area}」出发`,
    advice: '这一天留白，可按体力临时加一个住宿片区附近的去处。',
    stay_minutes: 0,
    travel_minutes: 0,
  })

  const lunchAt = Math.max(cur, parseHHMM('12:00'))
  push({
    time: fmtHHMM(lunchAt),
    type: 'meal',
    name: mealName(city.name, hotel.area, 'lunch'),
    advice: '不赶行程，就近找家本地小馆。',
    stay_minutes: MEAL_LUNCH_MINUTES,
    travel_minutes: Math.max(0, lunchAt - cur),
  })

  const dinnerAt = Math.max(cur, parseHHMM(DINNER_FROM))
  push({
    time: fmtHHMM(dinnerAt),
    type: 'meal',
    name: mealName(city.name, hotel.area, 'dinner'),
    advice: '吃完回酒店休息。',
    stay_minutes: MEAL_DINNER_MINUTES,
    travel_minutes: Math.max(0, dinnerAt - cur),
  })

  const hotelAt = Math.max(cur + DINNER_TO_HOTEL, endMin)
  push({
    time: fmtHHMM(hotelAt),
    type: 'hotel',
    name: `入住「${hotel.area}」`,
    advice: hotel.reason,
    stay_minutes: 0,
    travel_minutes: Math.max(0, hotelAt - cur),
  })

  return { day: dayIndex, theme: `${city.name}机动日 · 自由安排`, pace: '轻松', nodes, hotel }
}

// ================================================================ 主入口
/** 规则引擎主入口，签名与 LLM 流水线一致，可互换。 */
export function planFallback(city, attractionsIn, req) {
  const attractions = [...attractionsIn]
  const days = req.days

  // ① 容量裁剪（容量 = 逐天预算求和）
  const [kept, dropped] = pruneToCapacity(city, attractions, req)
  // ②③ 独占型隔离 + 成链均分
  const groups = assignDays(city, kept, days, req.transport, req.pace)
  // 住宿：一次定全程，只有远郊日才换
  const hotels = planHotels(city, groups)

  const dayPlans = []
  let carried = []
  let carriedFrom = {}

  for (let idx = 1; idx <= groups.length; idx += 1) {
    const group = groups[idx - 1]
    const hotel = hotels[idx - 1]
    const origin = hotelOrigin(city, hotel)
    const prevHotel = idx >= 2 ? hotels[idx - 2] : null
    let depart = prevHotel ? hotelOrigin(city, prevHotel) : null
    if (depart && depart[0] === origin[0] && depart[1] === origin[1]) depart = null

    // 昨天没排下的先插到最前面
    const items = [...carried, ...group]
    const [keptDay, overflow] = trimDay(city, items, req, dayBudget(req, idx, days), origin, depart)

    if (!keptDay.length) {
      // 机动日：走 emptyDay（它有午餐）
      dayPlans.push(emptyDay(city, req, idx, hotel))
    } else {
      dayPlans.push(
        buildDay(city, idx, keptDay, req, carriedFrom, {
          totalDays: days,
          hotel,
          departFrom: depart ? prevHotel : null,
        }),
      )
    }

    carried = []
    carriedFrom = {}
    for (const item of overflow) {
      if (idx >= days) {
        dropped.push({
          name: item.name,
          reason: `Day ${idx} 装不下，且已是最后一天，无法顺延。`,
          attraction_id: item.id,
        })
      } else {
        carried.push(item)
        carriedFrom[item.id] = {
          from_day: idx,
          reason: `Day ${idx} 排不下，顺延到 Day ${idx + 1}，动线上仍然顺路。`,
        }
      }
    }
  }

  for (const item of carried) {
    dropped.push({
      name: item.name,
      reason: '行程最后仍未排入，建议单独安排一天或下次再去。',
      attraction_id: item.id,
    })
  }

  const mustReasons = [...attractions]
    .sort((a, b) => b.heat - a.heat)
    .slice(0, 3)
    .map((a) => ({
      name: a.name,
      reason: `热度 ${a.heat}｜${a.intro || '当地标志性去处'}`,
      attraction_id: a.id,
    }))

  const totalHours = kept.reduce((s, a) => s + (a.visit_minutes || 0), 0) / 60
  const transportLabel = req.transport_label
  let summary =
    `${city.name} ${days} 天：按地理邻近度把 ${kept.length} 个景点排成 ${days} 条顺路线，` +
    `日均游览约 ${pyFixed(totalHours / Math.max(1, days), 1)} 小时，` +
    `自动插入午晚餐，${transportLabel}通勤，住宿固定在同一片区，目标 ${req.return_time} 前回到酒店。`
  if (dropped.length) {
    const names = dropped.slice(0, 3).map((d) => d.name).join('、')
    summary += ` 因时间预算舍弃 ${dropped.length} 个（${names}）。`
  }

  return {
    city: city.name,
    days,
    summary,
    day_plans: dayPlans,
    must_visit_reasons: mustReasons,
    dropped,
  }
}

// 供 preview / 本地 API 层复用的内部件（后端是被 service.py 直接 import 的私有函数）
export { argMax, hotelOrigin, cost, value }
