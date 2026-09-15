<template>
  <!-- 单根节点：页面必须只有一个根元素，详见 App.vue 的说明 -->
  <div class="page-root">
    <PhoneShell :title="city" :sub="mode === 'auto' ? '· 一键生成' : '· 勾选想去的景点'" back="/trip">
      <template #right>
        <button v-if="mode === 'detail'" class="btn line sm" @click="toggleAll">
          {{ allSelected ? '清空' : '全选' }}
        </button>
      </template>

      <!-- 顶部模式切换：直接生成（一键 AI）/ 详细规划（逐个勾景点） -->
      <div class="mode-tabs">
        <button :class="['mode-tab', { on: mode === 'auto' }]" @click="mode = 'auto'">
          直接生成
        </button>
        <button :class="['mode-tab', { on: mode === 'detail' }]" @click="mode = 'detail'">
          详细规划
        </button>
      </div>

      <!-- ① 直接生成：只要天数和节奏，景点交给 AI 挑 -->
      <div v-if="mode === 'auto'" class="auto-wrap">
        <div class="auto-card card">
          <h3 class="auto-title">一键生成行程</h3>
          <p class="auto-desc">
            定好天数和节奏就行，AI 会按地理顺路自动挑景点，并排出每天的时间轴。
          </p>

          <div class="set-row">
            <label>行程天数</label>
            <div class="seg days">
              <button
                v-for="d in 7" :key="d"
                :class="{ on: pref.days === d }"
                @click="pref.days = d"
              >{{ d }}</button>
            </div>
          </div>

          <div class="set-row">
            <label>节奏倾向</label>
            <div class="seg">
              <button
                v-for="p in PACES" :key="p.key"
                :class="{ on: pref.pace === p.key }"
                :title="p.tip"
                @click="pref.pace = p.key"
              >{{ p.label }}</button>
            </div>
          </div>

          <button class="btn lg auto-btn" :disabled="!!planning" @click="startAuto">
            ✨ 一键生成行程
          </button>

          <p class="auto-note">
            按「{{ PACE_LABEL_MAP[pref.pace] }}」节奏排 {{ pref.days }} 天，大约会挑
            {{ autoEstimate }} 个景点（必去优先，其次热度）；实际选中的会显示在结果页。
          </p>
        </div>
      </div>

      <!-- ② 详细规划：逐个勾景点 + 完整设置 -->
      <div v-else class="pick-layout">
        <!-- 左：景点列表 -->
        <div class="pick-main">
          <div class="pick-bar">
            <span>{{ attractions.length }} 个景点 · 按热度排序</span>
            <span v-if="selectedIds.length" class="pick-bar-num">已选 {{ selectedIds.length }}</span>
          </div>

          <div v-if="loading" class="att-grid">
            <div v-for="i in 6" :key="i" class="card sk" />
          </div>

          <div v-else-if="error" class="empty">
            <div class="big">😕</div>
            <p>{{ error }}</p>
            <button class="btn ghost sm" style="margin-top: 12px" @click="load">重试</button>
          </div>

          <div v-else class="att-grid">
            <button
              v-for="a in attractions"
              :key="a.id"
              :class="['att', { on: isOn(a.id) }]"
              @click="toggle(a.id)"
            >
              <div class="att-main">
                <div class="att-top">
                  <span class="att-name">{{ a.name }}</span>
                  <span v-if="a.must_visit" class="chip must">⭐ 必去</span>
                  <span class="chip hot">🔥 {{ a.heat }}</span>
                </div>
                <p class="att-intro">{{ a.intro }}</p>
                <div class="att-meta">
                  <span class="chip primary">⏱ 建议 {{ fmtMin(a.visit_minutes) }}</span>
                  <span v-if="a.district" class="chip">{{ a.district }}</span>
                  <span v-for="t in (a.tags || []).slice(0, 2)" :key="t" class="chip">{{ t }}</span>
                </div>
              </div>
              <span
                v-if="a.spot_count"
                class="att-more"
                role="button"
                title="查看内部点位与攻略"
                @click.stop="openSpots(a)"
              >›</span>
              <div :class="['tick', { on: isOn(a.id) }]">{{ isOn(a.id) ? '✓' : '' }}</div>
            </button>

            <p v-if="!attractions.length" class="hint">
              这个城市还没有景点数据，先跑一次初始化：<code>npm run seed -- --city {{ city }}</code>
            </p>
          </div>
        </div>

        <!-- 右：设置与提交（手机形态变成底部浮条） -->
        <aside class="pick-side">
          <div class="pick-panel">
            <div class="panel-head">
              <div class="panel-count">
                <strong>{{ selectedIds.length }}</strong>
                <span>个景点 · {{ pref.days }} 天</span>
              </div>
              <button class="panel-toggle" @click="showSet = !showSet">
                设置<i :class="{ up: showSet }">▾</i>
              </button>
            </div>

            <div v-show="showSet || isWide" class="panel-set">
              <div class="set-row">
                <label>行程天数</label>
                <div class="seg days">
                  <button v-for="d in 7" :key="d" :class="{ on: pref.days === d }" @click="pref.days = d">
                    {{ d }}
                  </button>
                </div>
              </div>

              <div class="set-row">
                <label>节奏倾向</label>
                <div class="seg">
                  <button
                    v-for="p in PACES" :key="p.key"
                    :class="{ on: pref.pace === p.key }"
                    :title="p.tip"
                    @click="pref.pace = p.key"
                  >{{ p.label }}</button>
                </div>
              </div>

              <div class="set-row">
                <label>出行方式</label>
                <div class="seg">
                  <button
                    v-for="t in TRANSPORTS" :key="t.key"
                    :class="{ on: pref.transport === t.key }"
                    :title="t.tip"
                    @click="pref.transport = t.key"
                  >{{ t.label }}</button>
                </div>
              </div>

              <div class="set-row">
                <label>每天出发</label>
                <input type="time" v-model="pref.startTime" class="time-input" />
                <span class="set-arrow">→</span>
                <input type="time" v-model="pref.returnTime" class="time-input" />
              </div>

              <label class="set-check">
                <input type="checkbox" v-model="pref.customEnds" />
                <span>首末天单独设置（第一天可能中午才到，最后一天要赶飞机）</span>
              </label>

              <!-- 首末天各占一行：挤在一行时两个时间框都只剩 60px 左右，看不清 -->
              <div v-if="pref.customEnds" class="set-ends">
                <div class="set-row">
                  <label>首天到达</label>
                  <input type="time" v-model="pref.firstDayStart" class="time-input" />
                </div>
                <div class="set-row">
                  <label>末天返回</label>
                  <input type="time" v-model="pref.lastDayReturn" class="time-input" />
                </div>
              </div>

              <!-- 紧凑度：纯硬编码预估，毫秒级返回 -->
              <div v-if="preview" :class="['load-box', `lv-${preview.tightness}`]">
                <div class="load-head">
                  <span class="load-tag">{{ preview.tightness }}</span>
                  <span class="load-num">
                    排入 {{ preview.scheduled_count }}/{{ preview.selected_count }}
                    <template v-if="preview.will_drop.length">· 舍弃 {{ preview.will_drop.length }}</template>
                  </span>
                </div>
                <p class="load-text">{{ preview.suggestion }}</p>
              </div>
              <p v-else-if="warn" class="set-warn">⚠️ {{ warn }}</p>
            </div>

            <div class="panel-cta">
              <button class="btn lg plan-btn" :disabled="!selectedIds.length || planning" @click="startPlan">
                开始规划
              </button>
            </div>
          </div>
        </aside>
      </div>

      <!-- 规划中 -->
      <div v-if="planning" class="loading-mask">
        <div class="spinner"><i /><i /></div>
        <div class="loading-steps">
          <h4>AI 规划中</h4>
          <p>{{ stepText }}</p>
        </div>
        <div class="loading-bar"><i /></div>
        <p class="loading-note">先按地理顺路分天，再并行生成每天的安排，通常 15~30 秒</p>
      </div>

      <!-- 景点详情 + 内部子景点（纯静态数据，不调 LLM） -->
      <SpotSheet
        :open="spotSheet.open"
        :att="spotSheet.att"
        :spots="spotSheet.spots"
        :loading="spotSheet.loading"
        @close="spotSheet.open = false"
      />
    </PhoneShell>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PhoneShell from '../../components/trip/PhoneShell.vue'
import SpotSheet from '../../components/trip/SpotSheet.vue'
import { autoPlan, createPlan, getAttractions, getSpots, previewPlan } from '../../trip-api'
import { commitPlan } from '../../trip-store'
import { useIsWide } from '../../use-media'

const route = useRoute()
const router = useRouter()
const isWide = useIsWide()
const city = computed(() => String(route.query.city || ''))

const attractions = ref([])

// ---------- 景点详情弹窗（内部子景点，纯静态数据） ----------
const spotSheet = reactive({ open: false, att: null, spots: [], loading: false })
async function openSpots(a) {
  spotSheet.att = a
  spotSheet.spots = []
  spotSheet.loading = true
  spotSheet.open = true
  try {
    spotSheet.spots = await getSpots(a.id)
  } catch {
    spotSheet.spots = []   // 拿不到就展示景点本身的信息，不打断
  } finally {
    spotSheet.loading = false
  }
}
const selectedIds = ref([])
const loading = ref(true)
const error = ref('')
const planning = ref(false)
const showSet = ref(false)
const stepText = ref('')
const preview = ref(null)
/** 'auto' = 直接生成（一键 AI，只要天数和节奏）· 'detail' = 详细规划（逐个勾景点） */
const mode = ref('detail')

const PACES = [
  { key: 'relaxed', label: '宽松', tip: '每天少排一点，早点收工，会舍弃更多景点' },
  { key: 'balanced', label: '平衡', tip: '按时间预算正常排' },
  { key: 'packed', label: '紧凑', tip: '尽量多塞景点，每天晚点收工' },
]

const TRANSPORTS = [
  { key: 'drive', label: '🚗 自驾', tip: '全程自己开，含找车位时间' },
  { key: 'mixed', label: '🚕 打车+公共', tip: '短途打车、跨区坐地铁、近郊顺风车，自动判断' },
  { key: 'transit', label: '🚇 公共', tip: '公交地铁 + 城际大巴 / 城际铁路' },
]

const pref = reactive({
  days: 2,
  pace: 'balanced',
  transport: 'mixed',
  startTime: '09:00',
  returnTime: '19:30',
  customEnds: false,
  firstDayStart: '13:00',
  lastDayReturn: '16:00',
})

const STEPS = [
  '正在按地理邻近度分天…',
  '计算顺路动线，避免折返…',
  '并行生成每天的安排…',
  '在饭点插入午晚餐…',
  '挑选住宿片区…',
  '生成时间轴并校验自洽性…',
]

const isOn = (id) => selectedIds.value.includes(id)
const allSelected = computed(
  () => attractions.value.length > 0 && selectedIds.value.length === attractions.value.length
)

const warn = computed(() => {
  const n = selectedIds.value.length
  if (!n) return '至少勾选 1 个景点'
  if (n < pref.days) return `勾了 ${n} 个景点却排 ${pref.days} 天，会有几天没内容`
  return ''
})

// ---------- 一键 AI：只要天数和节奏 ----------
const PACE_LABEL_MAP = { relaxed: '宽松', balanced: '平衡', packed: '紧凑' }
/** 与后端 planner.PACE_TARGET_MINUTES 对齐：每天的节奏目标游玩分钟数 */
const PACE_TARGET = { relaxed: 240, balanced: 360, packed: 450 }

/** 按节奏目标粗估会挑中几个（纯展示，实际以服务端为准） */
const autoEstimate = computed(() => {
  const pool = attractions.value
  if (!pool.length) return '若干'
  const target = pref.days * (PACE_TARGET[pref.pace] || 360)
  const avg =
    pool.reduce((s, a) => s + (a.visit_minutes || 90), 0) / pool.length
  return Math.max(1, Math.min(pool.length, Math.round(target / Math.max(30, avg))))
})

/** 提交给后端的完整参数 */
function buildPayload() {
  return {
    city: city.value,
    attraction_ids: [...selectedIds.value],
    days: pref.days,
    start_time: pref.startTime,
    return_time: pref.returnTime,
    first_day_start_time: pref.customEnds ? pref.firstDayStart : null,
    last_day_return_time: pref.customEnds ? pref.lastDayReturn : null,
    transport: pref.transport,
    pace: pref.pace,
  }
}

function fmtMin(m) {
  if (!m) return '—'
  if (m < 60) return `${m} 分钟`
  const h = m / 60
  return Number.isInteger(h) ? `${h} 小时` : `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

function toggle(id) {
  selectedIds.value = isOn(id) ? selectedIds.value.filter((x) => x !== id) : [...selectedIds.value, id]
}

function toggleAll() {
  selectedIds.value = allSelected.value ? [] : attractions.value.map((a) => a.id)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    attractions.value = await getAttractions(city.value)
    // 不记忆上次勾选：每次进页面都是干净状态。
    // 只保留必要的一步清理——换城市后旧 id 不再有效，直接剔除。
    selectedIds.value = selectedIds.value.filter((id) =>
      attractions.value.some((a) => a.id === id)
    )
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
    refreshPreview()
  }
}

// ---------- 紧凑度预估：防抖 350ms，纯硬编码接口，毫秒级返回 ----------
let previewTimer = null
let previewSeq = 0   // 请求序号：只认最新一次的响应，否则改了天数/节奏后
                     // 旧请求后返回会把新结果盖掉（表现为「排 6 天」却按 7 天算）

function refreshPreview() {
  clearTimeout(previewTimer)
  if (!selectedIds.value.length) {
    preview.value = null
    return
  }
  const seq = ++previewSeq
  previewTimer = setTimeout(async () => {
    try {
      const data = await previewPlan(buildPayload())
      if (seq !== previewSeq) return // 已有更新的请求在跑，丢弃这次结果
      preview.value = data
    } catch {
      if (seq === previewSeq) preview.value = null // 预估失败不影响主流程
    }
  }, 350)
}

watch(
  [selectedIds, () => pref.days, () => pref.pace, () => pref.transport,
   () => pref.startTime, () => pref.returnTime, () => pref.customEnds,
   () => pref.firstDayStart, () => pref.lastDayReturn],
  refreshPreview,
  { deep: true }
)

/** 两种模式共用的「跑规划 + loading 文案轮换 + 跳结果页」 */
async function runPlan(call) {
  planning.value = true
  let i = 0
  stepText.value = STEPS[0]
  const timer = setInterval(() => {
    i = Math.min(i + 1, STEPS.length - 1)
    stepText.value = STEPS[i]
  }, 3400)

  try {
    const plan = await call()
    commitPlan(plan)
    router.push({ path: '/trip/plan', query: { planId: String(plan.plan_id) } })
  } catch (e) {
    alert(e.message)
  } finally {
    clearInterval(timer)
    planning.value = false
  }
}

/** 详细规划：带着勾选的景点去生成 */
async function startPlan() {
  if (!selectedIds.value.length) return
  await runPlan(() => createPlan(buildPayload()))
}

/** 一键 AI：不传景点，由服务端按天数和节奏自动挑选 */
async function startAuto() {
  await runPlan(() =>
    autoPlan({
      city: city.value,
      days: pref.days,
      pace: pref.pace,
      transport: pref.transport,
      start_time: pref.startTime,
      return_time: pref.returnTime,
      first_day_start_time: pref.customEnds ? pref.firstDayStart : null,
      last_day_return_time: pref.customEnds ? pref.lastDayReturn : null,
    })
  )
}

onMounted(load)
watch(city, load)
</script>

<style scoped>
/* ================================================================
   顶部模式切换（直接生成 / 详细规划）
   ================================================================ */
.mode-tabs {
  display: flex; gap: 4px;
  margin: 0 var(--gutter) 14px;
  padding: 3px; border-radius: 9px;
  background: var(--surface-3);
}
.mode-tab {
  flex: 1; padding: 8px 12px; border-radius: 7px;
  font-size: 13.5px; font-weight: 550; color: var(--ink-2);
  transition: background 0.18s var(--ease), color 0.18s var(--ease),
    box-shadow 0.18s var(--ease);
}
.mode-tab:hover { color: var(--ink); }
.mode-tab.on {
  background: var(--surface); color: var(--brand-deep); font-weight: 660;
  box-shadow: var(--sh-1);
}

/* ---------- 直接生成（一键 AI） ---------- */
.auto-wrap { padding: 0 var(--gutter) 30px; }
.auto-card {
  max-width: 520px; margin: 0 auto;
  padding: 20px;
  display: flex; flex-direction: column; gap: 13px;
}
.auto-title { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.015em; }
.auto-desc { margin: 0; font-size: 13px; color: var(--ink-2); line-height: 1.6; }
.auto-btn { width: 100%; margin-top: 3px; }
.auto-note {
  margin: 0; font-size: 12px; color: var(--ink-3);
  line-height: 1.65; text-align: center;
}

/* ================================================================
   宽屏：左列表 + 右粘性设置栏
   ================================================================ */
.pick-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 356px;
  gap: 22px;
  align-items: start;
  padding-bottom: 30px;
}
.pick-bar {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 12.5px; color: var(--ink-3);
  padding: 0 2px 12px;
}
.pick-bar-num {
  display: inline-flex; align-items: center; gap: 5px;
  color: var(--brand); font-weight: 650;
}
.pick-bar-num::before {
  content: ''; width: 6px; height: 6px; border-radius: 50%;
  background: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-soft);
}

/* ---------- 景点卡片 ---------- */
.att-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 11px; }
.att {
  position: relative; overflow: hidden;
  display: flex; gap: 12px; align-items: flex-start; text-align: left;
  padding: 14px 15px 13px;
  border-radius: var(--r-lg);
  background: var(--surface);
  border: 1px solid var(--line);
  box-shadow: var(--sh-1);
  transition: transform 0.2s var(--ease), border-color 0.2s var(--ease),
    box-shadow 0.2s var(--ease), background 0.2s var(--ease);
}
.att::before {
  content: '';
  position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: var(--brand);
  transform: scaleY(0); transform-origin: center;
  transition: transform 0.24s var(--ease);
}
.att:hover { transform: translateY(-2px); border-color: var(--line-2); box-shadow: var(--sh-3); }
.att.on {
  border-color: var(--brand);
  background: linear-gradient(180deg, var(--brand-tint) 0%, var(--surface) 58%);
  box-shadow: 0 0 0 1px var(--brand-soft), var(--sh-2);
}
.att.on::before { transform: scaleY(1); }
.att:active { transform: translateY(-1px) scale(0.995); }

.att-main { flex: 1; min-width: 0; }
.att-top { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.att-name { font-size: 16px; font-weight: 680; letter-spacing: -0.012em; color: var(--ink); }
.att.on .att-name { color: var(--brand-deep); }
.att-intro { margin: 7px 0 9px; font-size: 13px; color: var(--ink-2); line-height: 1.62; }
.att-meta { display: flex; flex-wrap: wrap; gap: 5px; }

.tick {
  flex-shrink: 0; width: 22px; height: 22px; margin-top: 2px;
  border-radius: 50%;
  border: 1.5px solid var(--line-2);
  background: var(--surface);
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 700; color: #fff;
  transition: all 0.2s var(--ease);
}
.att:hover .tick { border-color: var(--brand); }
.tick.on {
  background: var(--brand); border-color: var(--brand);
  box-shadow: 0 0 0 4px var(--brand-soft);
  transform: scale(1.06);
}

/* 景点详情入口（有子景点数据才显示） */
.att-more {
  flex-shrink: 0; align-self: center;
  width: 24px; height: 24px; margin-left: -2px;
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  font-size: 17px; font-weight: 700; line-height: 1;
  color: var(--ink-4);
  transition: color 0.18s var(--ease), background 0.18s var(--ease), transform 0.18s var(--ease);
  cursor: pointer;
}
.att:hover .att-more { color: var(--brand); background: var(--brand-soft); }
.att-more:hover { transform: translateX(2px); }

.sk {
  height: 110px; border-radius: var(--r-lg);
  border: 1px solid var(--line);
  background: linear-gradient(100deg, var(--surface-2) 30%, var(--surface-3) 50%, var(--surface-2) 70%);
  background-size: 220% 100%;
  animation: sh 1.4s linear infinite;
}
@keyframes sh { to { background-position: -220% 0; } }
.hint { font-size: 13px; color: var(--ink-3); text-align: center; margin-top: 22px; }
.hint code { background: var(--surface-3); padding: 2px 6px; border-radius: 5px; font-size: 12.5px; color: var(--ink-2); }

/* ---------- 设置面板 ---------- */
.pick-side { position: sticky; top: 0; }
.pick-panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-2);
  padding: 17px;
  display: grid;
  grid-template-areas: "head" "set" "cta";
  gap: 14px;
}
.panel-head { grid-area: head; display: flex; align-items: baseline; justify-content: space-between; }
.panel-set { grid-area: set; display: flex; flex-direction: column; gap: 11px; }
.panel-cta { grid-area: cta; }
.panel-count strong { font-size: 30px; font-weight: 750; color: var(--brand); letter-spacing: -0.03em; line-height: 1; }
.panel-count span { font-size: 13px; color: var(--ink-2); margin-left: 7px; }
.panel-toggle { display: none; font-size: 12.5px; color: var(--ink-3); }
.panel-toggle i { font-style: normal; margin-left: 3px; display: inline-block; transition: transform 0.22s var(--ease); }
.panel-toggle i.up { transform: rotate(180deg); }

.set-row { display: flex; align-items: center; gap: 8px; }
.set-row > label:first-child {
  width: 58px; flex-shrink: 0;
  font-size: 12.5px; font-weight: 550; color: var(--ink-3);
}
.set-row .seg { flex: 1; min-width: 0; }
/* 首末天拆成两行，时间框才有足够宽度显示完整时刻 */
.set-ends { display: flex; flex-direction: column; gap: 8px; }
.set-arrow { color: var(--ink-4); font-size: 12px; flex-shrink: 0; }
.time-input {
  flex: 1; min-width: 0; padding: 7px 10px; border-radius: 8px;
  border: 1px solid var(--line-2); background: var(--surface);
  font-size: 13.5px; font-weight: 620; letter-spacing: 0.01em;
  transition: border-color 0.18s var(--ease), box-shadow 0.18s var(--ease);
}
.time-input:hover { border-color: var(--ink-4); }
.time-input:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 3px var(--brand-soft); }

.set-check {
  display: flex; align-items: flex-start; gap: 8px;
  font-size: 12px; color: var(--ink-2); line-height: 1.55;
  cursor: pointer; user-select: none;
}
.set-check input { margin: 3px 0 0; accent-color: var(--brand); flex-shrink: 0; }

/* ---------- 紧凑度 ---------- */
.load-box {
  padding: 10px 12px; border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--surface-2);
}
.load-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 5px; }
.load-tag { font-size: 12.5px; font-weight: 700; }
.load-num { font-size: 11.5px; color: var(--ink-3); }
.load-text { margin: 0; font-size: 12px; color: var(--ink-2); line-height: 1.6; }
.lv-轻松 { background: #f0f8f3; border-color: #d5ebdc; }
.lv-轻松 .load-tag { color: #2e7d4f; }
.lv-适中 { background: var(--brand-tint); border-color: #d3e9ea; }
.lv-适中 .load-tag { color: var(--brand-dark); }
.lv-紧凑 { background: var(--gold-soft); border-color: #f0e2c0; }
.lv-紧凑 .load-tag { color: #a8761a; }
.lv-超载 { background: #fdeceb; border-color: #f5d5d3; }
.lv-超载 .load-tag { color: #c4534f; }

.set-warn {
  margin: 0; padding: 8px 11px; border-radius: 8px;
  font-size: 12px; line-height: 1.6;
  background: var(--gold-soft); color: #9c6c14;
  border: 1px solid rgba(217, 155, 43, 0.22);
}
.plan-btn { width: 100%; }

/* ================================================================
   手机形态：设置面板变成底部浮条
   ================================================================ */
@media (max-width: 640px) {
  .mode-tabs { margin: 0 var(--gutter) 11px; }
  .mode-tab { padding: 7.5px 10px; font-size: 13px; }
  .auto-wrap { padding: 0 var(--gutter) 24px; }
  .auto-card { padding: 16px; gap: 11px; }
  .auto-title { font-size: 16px; }

  .pick-layout { display: block; padding-bottom: 100px; }
  .pick-bar { padding: 11px 0 3px; font-size: 12px; }
  .att-grid { display: flex; flex-direction: column; gap: 9px; padding-top: 8px; }
  .att { padding: 13px 14px 12px; border-radius: var(--r); }
  .att:hover { transform: none; box-shadow: var(--sh-1); }
  .att-name { font-size: 15.5px; }
  .att-intro { font-size: 12.5px; margin: 6px 0 8px; line-height: 1.6; }
  .tick { width: 22px; height: 22px; font-size: 12px; }
  .tick.on { box-shadow: 0 0 0 3px var(--brand-soft); }
  .sk { height: 96px; }

  .pick-side { position: fixed; left: 0; right: 0; bottom: 0; top: auto; z-index: 30; }
  .pick-panel {
    border-radius: 0; border-left: none; border-right: none; border-bottom: none;
    box-shadow: var(--sh-up);
    padding: 0 0 env(safe-area-inset-bottom);
    grid-template-areas: "head cta" "set set";
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: 0;
    background: rgba(255, 255, 255, 0.96);
    backdrop-filter: saturate(1.8) blur(16px);
    -webkit-backdrop-filter: saturate(1.8) blur(16px);
  }
  .panel-head { padding: 9px 14px; }
  .panel-count strong { font-size: 22px; }
  .panel-count span { font-size: 12px; margin-left: 5px; }
  .panel-toggle { display: block; }
  .panel-set {
    padding: 13px 14px 11px;
    border-top: 1px solid var(--line);
    background: var(--surface-2);
    max-height: 52vh; overflow-y: auto;
  }
  .set-row > label:first-child { width: 58px; font-size: 12px; }
  .panel-cta { padding: 0 14px; }
  .plan-btn { width: auto; padding: 10px 21px; }
}
</style>
