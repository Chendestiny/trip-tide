<template>
  <transition name="sheet">
    <div v-if="open" class="map-mask" @click.self="$emit('close')">
      <div class="map-sheet">
        <div class="map-head">
          <div>
            <strong>路线地图</strong>
            <span class="map-sub">{{ plan?.result?.city }} · GCJ-02 · {{ points.length }} 个点位</span>
          </div>
          <button class="map-x" @click="$emit('close')">✕</button>
        </div>

        <!-- 按天切换：全部 / Day N -->
        <div class="map-days">
          <button :class="['map-day', { on: activeDay === null }]" @click="activeDay = null">全部</button>
          <button
            v-for="d in dayCount"
            :key="d"
            :class="['map-day', { on: activeDay === d }]"
            :style="activeDay === d ? { background: color(d), borderColor: color(d) } : {}"
            @click="activeDay = d"
          >
            Day {{ d }}
          </button>
        </div>

        <!-- 高德 JS API 挂载点；未配 Key 或加载失败时用下面的 SVG 示意图 -->
        <div v-if="useAmap" ref="amapEl" class="map-canvas"></div>

        <div v-else class="map-canvas">
          <svg v-if="points.length" :viewBox="`0 0 ${W} ${H}`" class="map-svg">
            <!-- 底纹 -->
            <defs>
              <pattern id="tt-grid" width="34" height="34" patternUnits="userSpaceOnUse">
                <path d="M34 0 L0 0 0 34" fill="none" stroke="#e6ecef" stroke-width="1" />
              </pattern>
            </defs>
            <rect :width="W" :height="H" fill="url(#tt-grid)" />

            <!-- 每天的折线 -->
            <polyline
              v-for="line in polylines"
              :key="'l' + line.day"
              :points="line.pts"
              fill="none"
              :stroke="color(line.day)"
              stroke-width="2.4"
              stroke-linecap="round"
              stroke-linejoin="round"
              :opacity="activeDay === null || activeDay === line.day ? 0.85 : 0.12"
            />

            <!-- 点位 -->
            <g
              v-for="p in points"
              :key="p.key"
              :opacity="activeDay === null || activeDay === p.day ? 1 : 0.16"
              @click="selected = selected?.key === p.key ? null : p"
              style="cursor: pointer"
            >
              <circle v-if="p.mustVisit" :cx="p.x" :cy="p.y" r="13" :fill="color(p.day)" opacity="0.16" />
              <circle :cx="p.x" :cy="p.y" r="9" :fill="color(p.day)" stroke="#fff" stroke-width="2" />
              <text :x="p.x" :y="p.y + 3.4" class="map-num">{{ p.order }}</text>
              <text :x="p.x" :y="p.y - 15" class="map-name">{{ short(p.name) }}</text>
            </g>

            <!-- 选中详情 -->
            <g v-if="selected">
              <rect
                :x="clamp(selected.x - 74, 4, W - 152)" :y="Math.max(4, selected.y - 62)"
                width="148" height="42" rx="9" fill="#16232a" opacity="0.9"
              />
              <text :x="clamp(selected.x, 78, W - 78)" :y="Math.max(22, selected.y - 46)" class="map-tip" text-anchor="middle">
                {{ selected.name }}
              </text>
              <text :x="clamp(selected.x, 78, W - 78)" :y="Math.max(37, selected.y - 31)" class="map-tip sub" text-anchor="middle">
                {{ selected.time }} · {{ selected.dayLabel }}
              </text>
            </g>
          </svg>
          <div v-else class="empty"><div class="big">🗺️</div>这个方案还没有可定位的景点</div>
        </div>

        <!-- 图例 -->
        <div v-if="!useAmap && points.length" class="map-legend">
          <span v-for="d in dayCount" :key="'g' + d" class="lg">
            <i :style="{ background: color(d) }"></i>Day {{ d }}
          </span>
          <span class="lg-note">示意地图（未启用高德 JS API）</span>
        </div>
      </div>
    </div>
  </transition>
</template>

<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { dayColor } from '../../trip-theme'

// 半屏地图。两种模式：
//   1. 配置了 AMAP_JS_KEY（frontend/.env 的 VITE_AMAP_JS_KEY）→ 真·高德 JS API
//   2. 未配置 → 用 GCJ-02 经纬度做等距投影的 SVG 示意图（零依赖、离线可用）
// 两种模式的数据源完全相同：plan.attractions 的 lat/lng 与时间轴上的 attraction_id。
const props = defineProps({
  plan: { type: Object, default: null },
  open: { type: Boolean, default: false },
  day: { type: Number, default: null },
})

defineEmits(['close'])

const amapKey = import.meta.env.VITE_AMAP_JS_KEY || ''
const amapSecurity = import.meta.env.VITE_AMAP_SECURITY_CODE || ''
const amapEl = ref(null)
const amapFailed = ref(false)
const useAmap = computed(() => !!amapKey && !amapFailed.value)
let amapMap = null
let amapMarkers = []
let amapLines = []

// 组件卸载（离开行程页）时也要销毁，否则高德实例会泄漏
onUnmounted(() => destroyAmap())

const W = 400
const H = 320
const PAD = 34

const activeDay = ref(props.day)
watch(() => props.day, (v) => { activeDay.value = v })
watch(() => props.open, (v) => { if (v) selected.value = null })

const color = dayColor

const dayCount = computed(() => props.plan?.result?.day_plans?.length || 0)

/** 从时间轴抽出带坐标的点位（含所属天、顺序、时间） */
const rawPoints = computed(() => {
  const plan = props.plan
  if (!plan) return []
  const coordOf = new Map((plan.attractions || []).map((a) => [a.id, a]))
  const out = []
  ;(plan.result?.day_plans || []).forEach((dp) => {
    let order = 0
    ;(dp.nodes || []).forEach((n) => {
      if (n.type !== 'attraction' || n.attraction_id == null) return
      const a = coordOf.get(n.attraction_id)
      if (!a) return
      order += 1
      out.push({
        key: `${dp.day}-${order}-${a.id}`,
        name: a.name,
        lat: a.lat,
        lng: a.lng,
        day: dp.day,
        dayLabel: `Day ${dp.day}`,
        order,
        time: n.time,
        mustVisit: !!n.must_visit,
      })
    })
  })
  return out
})

/** 等距投影：经度按中心纬度做 cos 修正，保证形状不变形 */
const points = computed(() => {
  const pts = rawPoints.value
  if (!pts.length) return []

  const lats = pts.map((p) => p.lat)
  const lngs = pts.map((p) => p.lng)
  const latMid = (Math.min(...lats) + Math.max(...lats)) / 2
  const kx = Math.cos((latMid * Math.PI) / 180)

  const xs = lngs.map((l) => l * kx)
  const ys = lats.map((l) => -l) // 北在上
  const minX = Math.min(...xs), maxX = Math.max(...xs)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const spanX = maxX - minX || 1e-6
  const spanY = maxY - minY || 1e-6
  const scale = Math.min((W - PAD * 2) / spanX, (H - PAD * 2) / spanY)

  const offX = (W - spanX * scale) / 2
  const offY = (H - spanY * scale) / 2

  return pts.map((p, i) => ({
    ...p,
    x: offX + (xs[i] - minX) * scale,
    y: offY + (ys[i] - minY) * scale,
  }))
})

const selected = ref(null)

const polylines = computed(() =>
  Array.from({ length: dayCount.value }, (_, i) => i + 1)
    .map((d) => ({
      day: d,
      pts: points.value.filter((p) => p.day === d).map((p) => `${p.x},${p.y}`).join(' '),
    }))
    .filter((l) => l.pts.includes(','))
)

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi)
const short = (s) => (s && s.length > 6 ? s.slice(0, 6) + '…' : s || '')

// ---- 高德 JS API 模式（有 Key 才走） ----
// 关键：弹层是 v-if 控制的，关闭时 DOM 会被销毁。
// 地图实例必须跟着一起销毁，否则二次打开会往「已被移除的容器」上挂载 → 白屏 + 报错。
watch(
  () => props.open,
  async (open) => {
    if (!open) {
      destroyAmap()
      return
    }
    if (!amapKey || !points.value.length) return

    await nextTick()                       // 等 v-if 把容器插进 DOM
    const AMap = await loadAmap()
    if (!AMap || !amapEl.value) return

    destroyAmap()
    amapMap = new AMap.Map(amapEl.value, {
      zoom: 11,
      center: [points.value[0].lng, points.value[0].lat],
      viewMode: '2D',
    })
    drawAmap(AMap)
  }
)

// 切「全部 / Day N」只重画覆盖物，不重建地图
watch(activeDay, () => {
  if (amapMap && window.AMap) drawAmap(window.AMap)
})

function destroyAmap() {
  if (amapMap) {
    try { amapMap.clearMap(); amapMap.destroy() } catch { /* 已销毁就忽略 */ }
    amapMap = null
  }
  amapMarkers = []
  amapLines = []
}

/** 画 marker + 当天去程折线（不画回酒店的返程线） */
function drawAmap(AMap) {
  amapMarkers.forEach((m) => m.setMap(null))
  amapLines.forEach((l) => l.setMap(null))
  amapMarkers = []
  amapLines = []

  const shown = points.value.filter((p) => activeDay.value === null || p.day === activeDay.value)
  const days = activeDay.value === null
    ? Array.from({ length: dayCount.value }, (_, i) => i + 1)
    : [activeDay.value]

  // 折线先画，压在 marker 下面
  days.forEach((d) => {
    const seq = shown.filter((p) => p.day === d)
    if (seq.length < 2) return
    amapLines.push(
      new AMap.Polyline({
        path: seq.map((p) => [p.lng, p.lat]),
        strokeColor: color(d),
        strokeWeight: 4,
        strokeOpacity: 0.85,
        lineJoin: 'round',
        lineCap: 'round',
        showDir: true,               // 带方向箭头，一眼看出是「去程」
        map: amapMap,
      })
    )
  })

  shown.forEach((p) => {
    amapMarkers.push(
      new AMap.Marker({
        position: [p.lng, p.lat],
        title: p.name,
        label: { content: `${p.order}. ${p.name}（${p.time}）`, direction: 'top' },
        zIndex: 100 + p.order,
        map: amapMap,
      })
    )
  })

  if (amapMarkers.length) amapMap.setFitView(amapMarkers, false, [60, 60, 60, 60])
}

function loadAmap() {
  if (window.AMap) return Promise.resolve(window.AMap)
  // JS API 2.0：若 Key 绑定的是「安全密钥」，必须在加载脚本前注入，否则地图不渲染
  if (amapSecurity) {
    window._AMapSecurityConfig = { securityJsCode: amapSecurity }
  }
  return new Promise((resolve) => {
    const s = document.createElement('script')
    s.src = `https://webapi.amap.com/maps?v=2.0&key=${amapKey}`
    s.onload = () => {
      if (window.AMap) resolve(window.AMap)
      else { amapFailed.value = true; resolve(null) }
    }
    // 域名白名单没配 / 网络不通 → 回落 SVG 示意图，别留一块空白
    s.onerror = () => { amapFailed.value = true; resolve(null) }
    document.head.appendChild(s)
  })
}
</script>

<style scoped>
.map-mask {
  position: absolute; inset: 0; z-index: 80;
  background: rgba(10, 30, 36, 0.42);
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
  display: flex; align-items: flex-end;
  animation: maskIn 0.2s var(--ease);
}
@keyframes maskIn { from { opacity: 0; } }

.map-sheet {
  width: 100%; max-height: 74%;
  background: var(--surface);
  border-radius: var(--r-xl) var(--r-xl) 0 0;
  padding: 16px 16px 18px;
  display: flex; flex-direction: column; gap: 11px;
  box-shadow: 0 -12px 44px -12px rgba(10, 30, 36, 0.32);
  animation: sheetUp 0.28s var(--ease);
}
@keyframes sheetUp { from { transform: translateY(32px); opacity: 0.5; } }
.sheet-leave-active .map-sheet { animation: sheetUp 0.2s reverse; }

/* 顶部小把手 */
.map-sheet::before {
  content: '';
  width: 38px; height: 4px; border-radius: 2px;
  background: var(--line-2);
  margin: -6px auto 2px;
}

.map-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.map-head strong { font-size: 16px; font-weight: 700; letter-spacing: -0.015em; }
.map-sub { display: block; font-size: 11.5px; color: var(--ink-3); margin-top: 2px; }
.map-x {
  width: 28px; height: 28px; border-radius: 8px; flex-shrink: 0;
  color: var(--ink-3); font-size: 14px;
  background: var(--surface-3);
  transition: background 0.16s var(--ease), color 0.16s var(--ease);
}
.map-x:hover { background: var(--line-2); color: var(--ink); }

.map-days { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 2px; }
.map-days::-webkit-scrollbar { height: 0; }
.map-day {
  flex-shrink: 0; padding: 5px 12px; border-radius: 17px;
  border: 1px solid var(--line-2); background: var(--surface);
  font-size: 12.5px; font-weight: 550; color: var(--ink-2);
  transition: all 0.18s var(--ease);
}
.map-day:hover { border-color: var(--ink-4); }
.map-day.on {
  background: var(--brand); border-color: var(--brand);
  color: #fff; font-weight: 650;
  box-shadow: 0 2px 10px -3px rgba(8, 73, 78, 0.5);
}

.map-canvas {
  flex: 1; min-height: 260px;
  border-radius: var(--r-lg);
  overflow: hidden;
  border: 1px solid var(--line);
  background: var(--surface-2);
}
.map-svg { width: 100%; height: 100%; display: block; }
.map-num { font-size: 9px; fill: #fff; font-weight: 700; text-anchor: middle; }
.map-name { font-size: 9.5px; fill: var(--ink-2); text-anchor: middle; font-weight: 550; }
.map-tip { font-size: 11px; fill: #fff; font-weight: 620; }
.map-tip.sub { font-size: 9.5px; font-weight: 400; opacity: 0.78; }

.map-legend {
  display: flex; flex-wrap: wrap; gap: 11px; align-items: center;
  font-size: 11.5px; color: var(--ink-3);
}
.lg { display: inline-flex; align-items: center; gap: 5px; }
.lg i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.lg-note { margin-left: auto; font-size: 10.5px; opacity: 0.75; }

/* 宽屏：弹层收成居中的卡片，别横跨整个屏幕 */
@media (min-width: 641px) {
  .map-mask { align-items: center; justify-content: center; padding: 24px; }
  .map-sheet {
    max-width: 760px; max-height: 86%;
    border-radius: var(--r-xl);
    padding: 19px 21px 21px;
  }
  .map-sheet::before { display: none; }
  .map-canvas { min-height: 400px; }
}
</style>
