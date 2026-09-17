// 排程引擎 · 地理与交通（`planner.py` 的 haversine_m / leg / _mk_leg 镜像）
//
// 全部本地计算，**不打任何地图接口**：规划阶段十几条腿逐条打接口既慢又费配额，
// 而精度要求不高（±10 分钟不影响方案可用性）。

import {
  CITY_RADIUS_KM,
  EARTH_RADIUS_M,
  SHORT_TAXI_KM,
  SUBURB_LEG_KM,
  URBAN_LEG_KM,
} from './consts.js'
import { pyRound } from './pyfmt.js'

export { pyRound }

/** 两点球面距离（米）。输入为 GCJ-02 经纬度，同坐标系下差值可直接使用。 */
export function haversineM(lat1, lng1, lat2, lng2) {
  const p1 = (lat1 * Math.PI) / 180
  const p2 = (lat2 * Math.PI) / 180
  const dp = p2 - p1
  const dl = ((lng2 - lng1) * Math.PI) / 180
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(a))
}

/** 直线距离（km），保留 1 位小数——前端「周边 / 远郊」分档用 */
export const distanceKm = (lat1, lng1, lat2, lng2) =>
  pyRound((haversineM(lat1, lng1, lat2, lng2) / 1000) * 10) / 10

/** 交通方式：速度 km/h + 固定耗时（等车/停车/进出站）+ 适用距离上限 */
const MODES = {
  walk: { key: 'walk', label: '步行', speed: 4.5, overhead: 0, maxKm: 1.5 },
  metro: { key: 'metro', label: '地铁/公交', speed: 22.0, overhead: 9, maxKm: 40 },
  taxi: { key: 'taxi', label: '打车', speed: 26.0, overhead: 5, maxKm: 40 },
  driveCity: { key: 'drive', label: '自驾', speed: 30.0, overhead: 8, maxKm: 30 },
  driveHwy: { key: 'drive', label: '自驾·高速', speed: 62.0, overhead: 8, maxKm: 80 },
  driveFar: { key: 'drive', label: '自驾·高速', speed: 85.0, overhead: 8, maxKm: 9999 },
  carpool: { key: 'carpool', label: '顺风车', speed: 50.0, overhead: 14, maxKm: 90 },
  coach: { key: 'coach', label: '城际大巴', speed: 42.0, overhead: 22, maxKm: 150 },
  rail: { key: 'rail', label: '城际铁路', speed: 130.0, overhead: 40, maxKm: 9999 },
}

const WALK_MAX_KM = MODES.walk.maxKm

/** 一段行程的交通安排：{mode, label, km, minutes} */
function mkLeg(mode, km) {
  let minutes = (km / mode.speed) * 60.0 + mode.overhead
  minutes = Math.max(5.0, minutes)
  return {
    mode: mode.key,
    label: mode.label,
    km: pyRound(km * 10) / 10,
    minutes: Math.trunc(pyRound(minutes / 5.0) * 5),
  }
}

/**
 * 算一段路怎么走、要多久。
 *
 * transport 是用户选的大方向，具体方式按距离与场景挑：
 *   · drive   自驾全程（城区 30 / 高速 62 / 长途 85 km/h）
 *   · mixed   打车+公共：≤6km 打车、跨区中长途地铁、近郊顺风车、远郊城际铁路
 *   · transit 公共：地铁公交 → 城际大巴 → 城际铁路
 */
export function leg(city, fromLat, fromLng, toLat, toLng, transport) {
  const km = haversineM(fromLat, fromLng, toLat, toLng) / 1000.0

  if (km <= WALK_MAX_KM) return mkLeg(MODES.walk, km)

  const fromOut = haversineM(city.center_lat, city.center_lng, fromLat, fromLng) / 1000.0 > CITY_RADIUS_KM
  const toOut = haversineM(city.center_lat, city.center_lng, toLat, toLng) / 1000.0 > CITY_RADIUS_KM
  const bothOutside = fromOut && toOut
  const urban = km <= URBAN_LEG_KM && !bothOutside

  let mode
  if (transport === 'drive') {
    mode = urban ? MODES.driveCity : km <= SUBURB_LEG_KM ? MODES.driveHwy : MODES.driveFar
  } else if (transport === 'transit') {
    mode = urban ? MODES.metro : km <= SUBURB_LEG_KM ? MODES.coach : MODES.rail
  } else {
    // mixed（含历史值 taxi）
    if (urban && km <= SHORT_TAXI_KM) mode = MODES.taxi
    else if (urban) mode = MODES.metro
    else if (km <= SUBURB_LEG_KM) mode = MODES.carpool
    else mode = MODES.rail
  }
  return mkLeg(mode, km)
}

/** 从市中心到某景点的单程分钟数 */
export function travelFrom(city, item, transport) {
  return leg(city, city.center_lat, city.center_lng, item.lat, item.lng, transport).minutes
}

// ---------------------------------------------------------------- 时间
export function parseHHMM(value) {
  const [h, m] = String(value).split(':')
  return Number(h) * 60 + Number(m)
}

export function fmtHHMM(totalMinutes) {
  let t = Math.trunc(totalMinutes)
  t %= 24 * 60 // 跨天时折回 24 小时制，避免出现 25:10
  const h = Math.floor(t / 60)
  const m = t % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}
