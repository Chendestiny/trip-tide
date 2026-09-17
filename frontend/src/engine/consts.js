// 排程引擎 · 常量表
//
// 这里是 `backend/app/trip/planner.py` 里所有常量的**镜像**，一个字都不许自己发挥：
// 两边必须能算出同一个结果，否则「离线版」和「联机版」会给出不同的行程。
// 改任何一个值，两边一起改（对照见 docs/OFFLINE.md）。

// ---------------------------------------------------------------- 交通模型
// 用户只选大方向（自驾 / 打车+公共 / 公共），具体方式按距离与场景挑。
export const EARTH_RADIUS_M = 6371000

export const CITY_RADIUS_KM = 20 // 距市中心超过这个值算「城区外」
export const URBAN_LEG_KM = 30 // 城区腿的距离上限
export const SUBURB_LEG_KM = 80 // 近郊腿的距离上限
export const SHORT_TAXI_KM = 6 // 混合模式里，短于这个距离直接打车

export const MEAL_LUNCH_MINUTES = 60
export const MEAL_DINNER_MINUTES = 75
export const LUNCH_FROM = '11:20' // 午饭锚点
export const LUNCH_AFTER = '11:40' // 景点结束后过了这个点 → 出来立刻吃
export const LUNCH_MAX_WAIT = 75 // 为在饭点前吃饭最多愿意等多久
export const DINNER_FROM = '18:00' // 晚餐锚点：回到住宿片区之后才吃
export const EARLIEST_DINNER = '17:00' // 允许开饭的最早时刻
export const DINNER_TO_HOTEL = 10 // 饭后走回酒店的分钟数
export const MIN_STAY_MINUTES = 20 // 为按时返回，单个景点最少保留的停留时间
export const STANDALONE_MINUTES = 240 // 游览 ≥4h → 独占一天
export const STANDALONE_TRAVEL_MIN = 45 // 单程 ≥45min → 独占一天
export const CAPACITY_SLACK = 1.1 // 容量裁剪的松弛系数
export const OVERRUN_TOLERANCE = 30 // 超目标返回多少分钟算「跑长了」
export const OVERNIGHT_KM = 45 // 当天景点离常住片区超过这个距离 → 建议就近过夜
export const OVERPACK_RATIO = 1.5 // 单景耗时超当天预算这么多倍 → 放弃留机动日

// ---------------------------------------------------------------- 分天模型 v2
export const GRADE_SMALL_MAX = 90 // ≤90 分钟 → 小景点
export const GRADE_MEDIUM_MAX = 180 // 91~180 → 中景点；>180 → 大景点

// 每天目标「游玩时长」（分钟）
export const PACE_TARGET_MINUTES = { relaxed: 240, balanced: 360, packed: 450 }

// 弹性填充：(填充率 r, 倍数上限 m)
//     最终游玩 = clamp(可用游玩 × r, 基础游玩, 基础游玩 × m)
export const PACE_FLEX = {
  relaxed: [0.8, 1.0], // 按最少，早点收工
  balanced: [0.95, 1.25], // 适当扩充
  packed: [1.0, 1.5], // 填满可用时间
}
export const BIG_FLEX_MAX = 1.5 // 大景点 / 远郊一律允许扩到 1.5

export const CLUSTER_MAX_METERS = 1200 // 直线 ≤1.2km → 同一片区簇
export const MIN_SPLIT_MINUTES = 30 // 大景点跨午饭拆分时每段至少这么长

// 「必去」的热度线：全国 T 级 ≥500 直接视为必去
export const MUST_HEAT = 500

export const SPOT_ADVICE_MAX = 200 // 与 TimelineNode.advice 的 max_length 对齐

export const TRANSPORT_LABELS = {
  drive: '自驾',
  mixed: '打车+公共',
  transit: '公共交通',
  taxi: '打车+公共',
}
export const PACE_LABELS = { relaxed: '宽松', balanced: '平衡', packed: '紧凑' }

// 时段硬约束：少数景点的最佳时段是硬知识，写死比让模型猜可靠
export const TIME_RULES = {
  morning: {
    kind: 'morning',
    label: '看动物要赶早，午后动物多在半睡',
    latestStart: '12:00',
    earliestStart: '',
  },
  night: {
    kind: 'night',
    label: '夜景类要等亮灯后才有意义',
    latestStart: '',
    earliestStart: '15:00',
  },
  museum: {
    kind: 'museum',
    label: '博物馆一般 17:00 闭馆',
    latestStart: '15:30',
    earliestStart: '',
  },
}

// best_time 为空时的兜底关键词（兼容早期数据）
export const MORNING_KEYS = ['熊猫', '动物园', '植物园', '繁育研究基地']
export const NIGHT_KEYS = ['洪崖洞', '不夜城', '九眼桥', '夜景', '夜游', '酒吧', '灯光秀', '兰桂坊']
export const MUSEUM_KEYS = ['博物馆', '博物院', '纪念馆', '美术馆']
