<template>
  <!-- 单根节点：页面必须只有一个根元素，详见 App.vue 的说明 -->
  <div class="page-root">
    <PhoneShell :title="plan?.result?.city || '行程方案'" :sub="plan ? `· ${plan.result.days} 天` : ''" :back="backTo">
      <template #right>
        <button v-if="isWide && plan" class="btn line sm" @click="mapOpen = true">🗺️ 路线地图</button>
        <!-- 注意 !!busy：busy 是空字符串表示「空闲」，而 Vue 对布尔属性把 '' 判为真
             （因为 disabled="" 在 HTML 里就是禁用），不加 !! 会导致按钮永远禁用 -->
        <button class="btn ghost sm" :disabled="!plan || !!busy" @click="openRegen">
          {{ busy ? '生成中…' : '↻ 重新生成' }}
        </button>
      </template>

      <div v-if="loading" class="empty"><div class="big">⏳</div>正在读取方案…</div>

      <div v-else-if="!plan" class="empty">
        <div class="big">🧭</div>
        <p>还没有行程方案</p>
        <button class="btn ghost sm" style="margin-top: 12px" @click="$router.push('/trip')">去规划</button>
      </div>

      <template v-else>
        <!-- 总览：分段展示，景点名加粗 -->
        <div class="ov">
          <div class="ov-sum">
            <p v-for="(seg, i) in summarySegments" :key="i" v-html="seg" />
          </div>
          <div class="ov-meta">
            <span class="chip primary">{{ TRANSPORT_LABEL[plan.request.transport] }}</span>
            <span class="chip">{{ plan.request.start_time }} 出发</span>
            <span class="chip">{{ plan.request.return_time }} 回酒店</span>
            <span class="chip">{{ plan.attractions.length }} 个景点</span>
            <span class="chip">{{ PACE_LABEL[plan.request.pace] || '平衡' }}</span>
            <span v-if="plan.source === 'fallback'" class="chip hot">规则引擎生成</span>
            <span v-else-if="plan.source === 'tweak'" class="chip must">倾向微调</span>
            <span v-else class="chip must">AI 生成</span>
          </div>
        </div>

        <div class="plan-layout">
          <!-- 左：按天时间轴 -->
          <div class="plan-days">
            <div class="sec-title">按天行程</div>
            <div
              v-for="dp in plan.result.day_plans"
              :key="dp.day"
              class="day card"
              :style="{ '--dc': dayColor(dp.day) }"
            >
              <div class="day-head">
                <span class="day-no">Day {{ dp.day }}</span>
                <span class="pace" :style="{ background: paceStyle(dp.pace).bg, color: paceStyle(dp.pace).fg }">
                  {{ dp.pace }}
                </span>
              </div>
              <p class="day-theme">{{ dp.theme }}</p>

              <div class="tl">
                <div v-for="(n, i) in dp.nodes" :key="i" :class="['tl-node', `t-${n.type}`, { moved: n.moved_to_day }]">
                  <div class="tl-time">{{ n.time }}</div>
                  <div class="tl-rail">
                    <span class="tl-dot">{{ nodeVisual(n.type, plan.request.transport).icon }}</span>
                    <span v-if="i < dp.nodes.length - 1" class="tl-line" />
                  </div>
                  <div class="tl-body">
                    <div class="tl-name">
                      {{ n.name }}
                      <span v-if="n.must_visit" class="chip must">⭐ 必去</span>
                    </div>
                    <p v-if="n.advice" class="tl-advice">{{ n.advice }}</p>
                    <p v-if="n.moved_to_day" class="tl-moved">
                      已挪至 Day {{ n.moved_to_day }}<template v-if="n.moved_from_day">（原 Day {{ n.moved_from_day }}）</template>
                      <template v-if="n.move_reason">· {{ n.move_reason }}</template>
                    </p>
                    <p v-if="n.travel_minutes > 0" class="tl-travel">
                      <template v-if="n.travel_mode">{{ n.travel_mode }}约 </template>
                      <template v-else>路上约 </template>{{ n.travel_minutes }} 分钟<template v-if="n.stay_minutes"> · 停留 {{ fmtMin(n.stay_minutes) }}</template>
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- 右：方案说明（手机形态落到时间轴下方） -->
          <aside class="plan-side">
            <div class="sec-title">方案说明</div>
            <div class="fold card">
              <button v-for="s in sections" :key="s.key" class="fold-head" @click="toggle(s.key)">
                <span class="fold-ic">{{ s.icon }}</span>
                <span class="fold-title">{{ s.title }}</span>
                <span class="fold-badge">{{ s.count }}</span>
                <i :class="{ up: open[ s.key ] }">▾</i>
              </button>
            </div>

            <!-- AI 复核意见 -->
            <div v-if="open.review" class="panel card">
              <p v-if="!reviewIssues.length" class="panel-empty">AI 复核通过，没有发现明显问题。</p>
              <div v-for="(it, i) in reviewIssues" :key="i" class="issue" :class="it.level">
                <span class="issue-lv">{{ { error: '需修正', warn: '建议调整', info: '提醒' }[it.level] }}</span>
                <span class="row-text">
                  <template v-if="it.day">Day {{ it.day }} · </template>{{ it.text }}
                </span>
              </div>
            </div>

            <!-- 舍弃的景点 -->
            <div v-if="open.dropped" class="panel card">
              <p v-if="!plan.result.dropped.length" class="panel-empty">没有被舍弃的景点，勾选的都排进去了。</p>
              <div v-for="(d, i) in plan.result.dropped" :key="i" class="row">
                <span class="row-name">{{ d.name }}</span>
                <span class="row-text">{{ d.reason }}</span>
              </div>
            </div>

            <!-- 必去理由 -->
            <div v-if="open.must" class="panel card">
              <div v-for="(m, i) in plan.result.must_visit_reasons" :key="i" class="row">
                <span class="row-name">{{ m.name }}</span>
                <span class="row-text">{{ m.reason }}</span>
              </div>
            </div>

            <!-- 酒店建议 -->
            <div v-if="open.hotel" class="panel card">
              <p class="panel-hint">全程默认住同一片区，避免频繁搬行李；只有远郊日才建议就近过夜。</p>
              <div v-for="dp in plan.result.day_plans" :key="dp.day" class="hotel">
                <div class="hotel-head">
                  <span class="chip" :style="{ background: dayColor(dp.day) + '1f', color: dayColor(dp.day) }">
                    Day {{ dp.day }}
                  </span>
                  <strong>{{ dp.hotel?.area || '未给出建议' }}</strong>
                </div>
                <p class="row-text">{{ dp.hotel?.reason }}</p>
                <div v-for="(alt, i) in (dp.hotel?.alternatives || [])" :key="i" class="alt">
                  <span class="alt-name">{{ alt.area }}</span>
                  <span class="row-text">{{ alt.tradeoff }}</span>
                </div>
              </div>
            </div>
          </aside>
        </div>
      </template>

      <!-- 手机形态：地图按钮放底部浮条 -->
      <template #footer>
        <div v-if="plan && !isWide" class="foot">
          <button class="btn map-btn" @click="mapOpen = true">🗺️ 查看路线地图</button>
        </div>
      </template>

      <TripMap :plan="plan" :open="mapOpen" @close="mapOpen = false" />

      <!-- 重新生成弹窗 -->
      <div v-if="regenOpen" class="dlg-mask" @click.self="regenOpen = false">
        <div class="dlg card">
          <div class="dlg-head">
            <strong>重新生成</strong>
            <button class="dlg-x" @click="regenOpen = false">✕</button>
          </div>

          <div class="dlg-body">
            <div class="dlg-row">
              <label>行程天数</label>
              <div class="seg days">
                <button v-for="d in 7" :key="d" :class="{ on: rg.days === d }" @click="rg.days = d">{{ d }}</button>
              </div>
            </div>

            <div class="dlg-row">
              <label>节奏倾向</label>
              <div class="seg">
                <button :class="{ on: rg.pace === 'relaxed' }" @click="rg.pace = 'relaxed'">宽松</button>
                <button :class="{ on: rg.pace === 'balanced' }" @click="rg.pace = 'balanced'">平衡</button>
                <button :class="{ on: rg.pace === 'packed' }" @click="rg.pace = 'packed'">紧凑</button>
              </div>
            </div>

            <div class="dlg-row">
              <label>出行方式</label>
              <div class="seg">
                <button :class="{ on: rg.transport === 'drive' }" @click="rg.transport = 'drive'">🚗 自驾</button>
                <button :class="{ on: rg.transport === 'mixed' }" @click="rg.transport = 'mixed'">🚕 打车+公共</button>
                <button :class="{ on: rg.transport === 'transit' }" @click="rg.transport = 'transit'">🚇 公共</button>
              </div>
            </div>

            <div class="dlg-row">
              <label>每天出发</label>
              <input type="time" v-model="rg.startTime" class="time-input" />
              <span class="dlg-arrow">→</span>
              <input type="time" v-model="rg.returnTime" class="time-input" />
            </div>

            <label class="dlg-check">
              <input type="checkbox" v-model="rg.customEnds" />
              <span>首末天单独设置</span>
            </label>
            <div v-if="rg.customEnds" class="dlg-ends">
              <div class="dlg-row">
                <label>首天到达</label>
                <input type="time" v-model="rg.firstDayStart" class="time-input" />
              </div>
              <div class="dlg-row">
                <label>末天返回</label>
                <input type="time" v-model="rg.lastDayReturn" class="time-input" />
              </div>
            </div>

            <p class="dlg-hint">
              <b>按倾向微调</b>：沿用现在的景点分配，只按新参数重排时间轴，秒级完成、不重写文案。<br />
              <b>全部重做</b>：重新跑一遍 AI，文案与分天都会变。
            </p>
          </div>

          <div class="dlg-actions">
            <button class="btn line sm" @click="regenOpen = false">取消</button>
            <button class="btn ghost sm" :disabled="!!busy" @click="doAdjust">按倾向微调</button>
            <button class="btn sm" :disabled="!!busy" @click="doRebuild">全部重做</button>
          </div>
        </div>
      </div>

      <div v-if="busy" class="loading-mask">
        <div class="spinner"><i /><i /></div>
        <div class="loading-steps">
          <h4>{{ busy === 'adjust' ? '正在按倾向重排…' : '重新规划中' }}</h4>
          <p>{{ busy === 'adjust' ? '复用现有景点分配，只重排时间轴' : stepText }}</p>
        </div>
        <div class="loading-bar"><i /></div>
      </div>
    </PhoneShell>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PhoneShell from '../../components/trip/PhoneShell.vue'
import TripMap from '../../components/trip/TripMap.vue'
import { adjustPlan, createPlan, getPlan } from '../../trip-api'
import { commitPlan, openHistory, store } from '../../trip-store'
import { TRANSPORT_LABEL, dayColor, nodeVisual, paceStyle } from '../../trip-theme'
import { useIsWide } from '../../use-media'

const route = useRoute()
const router = useRouter()
const isWide = useIsWide()
const plan = ref(null)
const loading = ref(true)
const busy = ref('')          // '' | 'adjust' | 'rebuild'
const mapOpen = ref(false)
const regenOpen = ref(false)
const stepText = ref('')
const open = reactive({ review: false, dropped: false, must: false, hotel: false })

const PACE_LABEL = { relaxed: '宽松', balanced: '平衡', packed: '紧凑' }

const STEPS = [
  '重新按地理邻近度分天…',
  '并行生成每天的安排…',
  '在饭点插入午晚餐…',
  '校验时间自洽性…',
]

/** 返回景点池时要带上城市，否则 pick 页拿不到 city 会报「缺少 city 参数」 */
const backTo = computed(() => {
  const c = plan.value?.request?.city
  return c ? `/trip/pick?city=${encodeURIComponent(c)}` : '/trip'
})

const sections = computed(() => {
  const list = [
    { key: 'dropped', icon: '🗑️', title: '舍弃的景点', count: plan.value?.result?.dropped?.length || 0 },
    { key: 'must', icon: '⭐', title: '必去理由', count: plan.value?.result?.must_visit_reasons?.length || 0 },
    { key: 'hotel', icon: '🏨', title: '酒店建议与理由', count: plan.value?.result?.day_plans?.length || 0 },
  ]
  if (reviewIssues.value.length) {
    list.unshift({ key: 'review', icon: '🔍', title: 'AI 复核意见', count: reviewIssues.value.length })
  }
  return list
})

const reviewIssues = computed(() => plan.value?.review?.issues || [])

/**
 * 总述按句分段，并把景点名加粗。
 * 一大段糊在一起读起来很累；加粗景点名能让用户一眼扫到「去了哪几个地方」。
 * 先转义再插 <strong>，所以 v-html 是安全的。
 */
const summarySegments = computed(() => {
  const text = plan.value?.result?.summary || ''
  if (!text) return []
  const names = (plan.value?.attractions || [])
    .map((a) => a.name)
    .filter(Boolean)
    .sort((a, b) => b.length - a.length)   // 长名优先，避免短名先匹配把长名切断

  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

  return text
    .split(/(?<=[。；！])/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((sent) => {
      let html = esc(sent)
      for (const n of names) {
        const en = esc(n)
        if (en && html.includes(en)) html = html.split(en).join(`<strong>${en}</strong>`)
      }
      return html
    })
})

const toggle = (k) => { open[k] = !open[k] }
const fmtMin = (m) => (m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? (m % 60) + 'm' : ''}` : `${m}m`)

// ---------- 重新生成弹窗的状态 ----------
const rg = reactive({
  days: 1,
  pace: 'balanced',
  transport: 'mixed',
  startTime: '09:00',
  returnTime: '19:30',
  customEnds: false,
  firstDayStart: '13:00',
  lastDayReturn: '16:00',
})

function openRegen() {
  const req = plan.value?.request
  if (!req) return
  Object.assign(rg, {
    days: req.days,
    pace: req.pace || 'balanced',
    transport: req.transport || 'mixed',
    startTime: req.start_time,
    returnTime: req.return_time,
    customEnds: !!(req.first_day_start_time || req.last_day_return_time),
    firstDayStart: req.first_day_start_time || '13:00',
    lastDayReturn: req.last_day_return_time || '16:00',
  })
  regenOpen.value = true
}

function regenPayload() {
  return {
    city: plan.value.request.city,
    attraction_ids: [...plan.value.request.attraction_ids],
    days: rg.days,
    start_time: rg.startTime,
    return_time: rg.returnTime,
    first_day_start_time: rg.customEnds ? rg.firstDayStart : null,
    last_day_return_time: rg.customEnds ? rg.lastDayReturn : null,
    transport: rg.transport,
    pace: rg.pace,
  }
}

async function doAdjust() {
  busy.value = 'adjust'
  regenOpen.value = false
  try {
    const next = await adjustPlan(plan.value.plan_id, regenPayload())
    commitPlan(next)
    plan.value = next
    router.replace({ path: '/trip/plan', query: { planId: String(next.plan_id) } })
  } catch (e) {
    alert(e.message)
  } finally {
    busy.value = ''
  }
}

async function doRebuild() {
  busy.value = 'rebuild'
  regenOpen.value = false
  let i = 0
  stepText.value = STEPS[0]
  const timer = setInterval(() => { i = Math.min(i + 1, STEPS.length - 1); stepText.value = STEPS[i] }, 3400)
  try {
    const next = await createPlan(regenPayload())
    commitPlan(next)
    plan.value = next
    router.replace({ path: '/trip/plan', query: { planId: String(next.plan_id) } })
  } catch (e) {
    alert(e.message)
  } finally {
    clearInterval(timer)
    busy.value = ''
  }
}

async function load() {
  loading.value = true
  try {
    const planId = Number(route.query.planId)
    // 优先用内存里的当前方案；刷新页面后回落到本地历史；再不行才请求后端
    if (store.current && (!planId || store.current.plan_id === planId)) {
      plan.value = store.current
    } else if (planId && openHistory(planId)) {
      plan.value = store.current
    } else if (planId) {
      plan.value = await getPlan(planId)
      commitPlan(plan.value)
    }
  } catch (e) {
    alert(e.message)
  } finally {
    loading.value = false
    // 有需要调整的意见就默认展开，别让它藏在折叠区里
    if (reviewIssues.value.some((i) => i.level === 'error' || i.level === 'warn')) {
      open.review = true
    }
  }
}

onMounted(load)
</script>

<style scoped>
/* ================================================================
   总览
   ================================================================ */
.ov { padding: 4px var(--gutter) 0; }
.ov-sum {
  margin: 0 0 13px; max-width: 940px;
  padding-left: 13px;
  border-left: 3px solid var(--brand-soft);
}
.ov-sum p { margin: 0 0 5px; font-size: 14.5px; line-height: 1.72; color: var(--ink-2); }
.ov-sum p:last-child { margin-bottom: 0; }
.ov-sum :deep(strong) {
  color: var(--ink); font-weight: 680;
  background: linear-gradient(180deg, transparent 62%, var(--brand-soft) 62%);
  padding: 0 1px;
}
.ov-meta { display: flex; flex-wrap: wrap; gap: 6px; }

/* ================================================================
   宽屏：时间轴 + 右侧粘性说明
   ================================================================ */
.plan-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  gap: 24px;
  align-items: start;
  padding-bottom: 34px;
}
.plan-days { min-width: 0; }
.plan-side { position: sticky; top: 0; }

/* ---------- 天卡片 ---------- */
.day {
  position: relative;
  padding: 17px 19px 9px;
  margin-bottom: 13px;
  border-radius: var(--r-lg);
  border: 1px solid var(--line);
  border-top: none;
  box-shadow: var(--sh-1);
  overflow: hidden;
  transition: box-shadow 0.22s var(--ease);
}
.day:hover { box-shadow: var(--sh-2); }
.day::before {
  content: '';
  position: absolute; left: 0; right: 0; top: 0; height: 3px;
  background: linear-gradient(90deg, var(--dc) 0%, color-mix(in srgb, var(--dc) 30%, transparent) 62%, transparent 100%);
}
.day-head { display: flex; align-items: center; gap: 10px; }
.day-no {
  display: inline-flex; align-items: center;
  padding: 3px 11px; border-radius: 8px;
  font-size: 13px; font-weight: 720; letter-spacing: 0.01em;
  color: #fff; background: var(--dc);
  box-shadow: 0 2px 8px -2px color-mix(in srgb, var(--dc) 55%, transparent);
}
.pace { padding: 3px 10px; border-radius: 7px; font-size: 11.5px; font-weight: 650; }
.day-theme { margin: 10px 0 15px; font-size: 13.5px; color: var(--ink-2); line-height: 1.58; }

/* ---------- 时间轴 ---------- */
.tl { display: flex; flex-direction: column; }
.tl-node { display: grid; grid-template-columns: 56px 30px 1fr; gap: 0 8px; }
.tl-time {
  font-size: 12.5px; font-weight: 620; color: var(--ink-3);
  font-variant-numeric: tabular-nums;
  padding-top: 6px; letter-spacing: 0.01em;
}
.tl-rail { display: flex; flex-direction: column; align-items: center; }
.tl-dot {
  width: 26px; height: 26px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; line-height: 1;
  background: var(--surface);
  border: 1.5px solid var(--line-2);
  flex-shrink: 0; z-index: 1;
  transition: transform 0.2s var(--ease);
}
.tl-node:hover .tl-dot { transform: scale(1.1); }
.t-depart .tl-dot { background: var(--surface-2); color: var(--ink-3); }
.t-attraction .tl-dot { border-color: var(--dc); box-shadow: 0 0 0 3px color-mix(in srgb, var(--dc) 12%, transparent); }
.t-meal .tl-dot { border-color: #e0a24a; background: #fffdf6; }
.t-hotel .tl-dot { border-color: var(--dc); background: var(--dc); }
.t-transit .tl-dot { border-color: var(--line-2); background: var(--surface-2); color: var(--ink-4); }
.tl-line {
  flex: 1; width: 2px; min-height: 16px; margin: 3px 0;
  background: linear-gradient(180deg, var(--line-2), var(--line));
  border-radius: 1px;
}
.tl-body { padding: 4px 0 15px 9px; min-width: 0; }
.tl-name {
  font-size: 15px; font-weight: 660; letter-spacing: -0.01em;
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
}
.t-attraction .tl-name { color: var(--dc); }
.tl-advice { margin: 5px 0 0; font-size: 12.5px; color: var(--ink-2); line-height: 1.66; }
.tl-moved {
  margin: 6px 0 0; padding: 5px 10px;
  font-size: 12px; color: var(--ink-3); line-height: 1.6;
  background: var(--surface-2);
  border-left: 2.5px solid var(--line-2);
  border-radius: 0 7px 7px 0;
}
.tl-travel {
  display: inline-flex; align-items: center; gap: 5px;
  margin: 6px 0 0; padding: 3px 8px;
  font-size: 11.5px; color: var(--ink-3);
  background: var(--surface-2); border-radius: 6px;
}
.tl-travel::before {
  content: ''; width: 5px; height: 5px; border-radius: 50%;
  background: var(--ink-4); flex-shrink: 0;
}

/* ================================================================
   折叠区
   ================================================================ */
.fold { overflow: hidden; padding: 0; border-radius: var(--r-lg); }
.fold-head {
  width: 100%; display: flex; align-items: center; gap: 10px;
  padding: 13px 15px; text-align: left; font-size: 14px;
  border-bottom: 1px solid var(--line);
  transition: background 0.18s var(--ease);
}
.fold-head:hover { background: var(--surface-2); }
.fold-head:last-child { border-bottom: none; }
.fold-ic {
  width: 24px; height: 24px; border-radius: 7px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; background: var(--surface-3);
}
.fold-title { flex: 1; font-weight: 620; }
.fold-badge {
  min-width: 22px; height: 22px; padding: 0 7px; border-radius: 11px;
  background: var(--surface-3); color: var(--ink-3);
  font-size: 11.5px; font-weight: 680;
  display: flex; align-items: center; justify-content: center;
}
.fold-head i { font-style: normal; color: var(--ink-4); font-size: 11px; transition: transform 0.24s var(--ease); }
.fold-head i.up { transform: rotate(180deg); }

.panel { margin-top: 12px; padding: 15px 16px; border-radius: var(--r-lg); }
.panel-empty { margin: 0; font-size: 13px; color: var(--ink-3); }
.panel-hint {
  margin: 0 0 10px; padding-bottom: 9px;
  font-size: 12px; color: var(--ink-3); line-height: 1.6;
  border-bottom: 1px dashed var(--line);
}
.row { padding: 9px 0; border-bottom: 1px dashed var(--line); }
.row:last-child { border-bottom: none; padding-bottom: 0; }
.row:first-child { padding-top: 0; }
.row-name { display: block; font-size: 14px; font-weight: 640; margin-bottom: 4px; }
.row-text { margin: 0; font-size: 12.5px; color: var(--ink-2); line-height: 1.66; }

.issue { display: flex; gap: 9px; align-items: flex-start; padding: 9px 0; border-bottom: 1px dashed var(--line); }
.issue:last-child { border-bottom: none; padding-bottom: 0; }
.issue:first-child { padding-top: 0; }
.issue-lv { flex-shrink: 0; margin-top: 2px; padding: 2.5px 9px; border-radius: 7px; font-size: 11px; font-weight: 680; }
.issue.error .issue-lv { background: #fdeceb; color: #c4534f; }
.issue.warn .issue-lv { background: var(--gold-soft); color: #a8761a; }
.issue.info .issue-lv { background: var(--surface-3); color: var(--ink-3); }

.hotel { padding: 10px 0; border-bottom: 1px dashed var(--line); }
.hotel:last-child { border-bottom: none; padding-bottom: 0; }
.hotel:first-child { padding-top: 0; }
.hotel-head { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
.hotel-head strong { font-size: 14.5px; font-weight: 680; }
.alt {
  margin-top: 7px; padding: 8px 11px; border-radius: 8px;
  background: var(--surface-2); display: flex; gap: 9px; align-items: baseline;
}
.alt-name { flex-shrink: 0; font-size: 12.5px; font-weight: 640; color: var(--ink-2); }

/* ---------- 底部（仅手机） ---------- */
.foot {
  flex-shrink: 0;
  padding: 9px var(--gutter) calc(9px + env(safe-area-inset-bottom));
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: saturate(1.8) blur(16px);
  -webkit-backdrop-filter: saturate(1.8) blur(16px);
  border-top: 1px solid var(--line);
  box-shadow: var(--sh-up);
}
.map-btn { width: 100%; padding: 11px; border-radius: 10px; }

/* ================================================================
   重新生成弹窗
   ================================================================ */
.dlg-mask {
  position: absolute; inset: 0; z-index: 70;
  background: rgba(10, 30, 36, 0.42);
  backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
  animation: fadeIn 0.18s var(--ease);
}
@keyframes fadeIn { from { opacity: 0; } }
.dlg {
  width: 100%; max-width: 470px;
  border-radius: var(--r-xl);
  box-shadow: 0 24px 60px -16px rgba(10, 30, 36, 0.4);
  animation: dlgIn 0.24s var(--ease);
  overflow: hidden;
}
@keyframes dlgIn { from { transform: translateY(14px) scale(0.98); opacity: 0.6; } }
.dlg-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 15px 18px; border-bottom: 1px solid var(--line);
}
.dlg-head strong { font-size: 16px; font-weight: 700; letter-spacing: -0.015em; }
.dlg-x { width: 26px; height: 26px; border-radius: 7px; color: var(--ink-3); }
.dlg-x:hover { background: var(--surface-3); color: var(--ink); }
.dlg-body { padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; }
.dlg-row { display: flex; align-items: center; gap: 9px; }
.dlg-row > label:first-child { width: 64px; flex-shrink: 0; font-size: 12.5px; color: var(--ink-3); }
.dlg-row .seg { flex: 1; min-width: 0; }
.dlg-arrow { color: var(--ink-4); font-size: 12px; }
/* 首末天拆两行，时间框拿到整行宽度 */
.dlg-ends { display: flex; flex-direction: column; gap: 8px; }
.dlg-check {
  display: flex; align-items: center; gap: 8px;
  font-size: 12.5px; color: var(--ink-2); cursor: pointer; user-select: none;
}
.dlg-check input { accent-color: var(--brand); }
.dlg-hint {
  margin: 0; padding: 10px 12px; border-radius: 8px;
  font-size: 12px; line-height: 1.7; color: var(--ink-2);
  background: var(--surface-2);
}
.dlg-hint b { color: var(--ink); font-weight: 650; }
.dlg-actions {
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 13px 18px; border-top: 1px solid var(--line);
  background: var(--surface-2);
}
.time-input {
  flex: 1; min-width: 0; padding: 7px 10px; border-radius: 8px;
  border: 1px solid var(--line-2); background: var(--surface);
  font-size: 13.5px; font-weight: 620;
}
.time-input:focus { outline: none; border-color: var(--brand); box-shadow: 0 0 0 3px var(--brand-soft); }

/* ================================================================
   手机形态
   ================================================================ */
@media (max-width: 640px) {
  .ov { padding: 13px var(--gutter) 0; }
  .ov-sum { padding-left: 11px; margin-bottom: 11px; }
  .ov-sum p { font-size: 13px; line-height: 1.66; }
  .ov-meta { gap: 5px; }

  .plan-layout { display: block; padding-bottom: 0; }
  .plan-days .sec-title { margin-top: 15px; }
  .plan-side { position: static; }
  .plan-side .sec-title { margin-top: 18px; }

  .day { padding: 14px 14px 6px; margin-bottom: 11px; border-radius: var(--r); }
  .day-no { font-size: 12px; padding: 2.5px 8px; }
  .day-theme { font-size: 12.5px; margin: 8px 0 13px; }

  .tl-node { grid-template-columns: 44px 24px 1fr; gap: 0 5px; }
  .tl-time { font-size: 11.5px; padding-top: 6px; }
  .tl-dot { width: 22px; height: 22px; font-size: 11px; }
  .tl-body { padding-left: 7px; padding-bottom: 13px; }
  .tl-name { font-size: 14.5px; }
  .tl-advice { font-size: 12px; line-height: 1.64; }
  .tl-travel { font-size: 11px; padding: 2.5px 7px; }
  .tl-moved { font-size: 11.5px; padding: 4px 8px; }

  .dlg-mask { align-items: flex-end; padding: 0; }
  .dlg { max-width: 100%; border-radius: var(--r-xl) var(--r-xl) 0 0; }
  .dlg-body { max-height: 60vh; overflow-y: auto; }
  .dlg-row > label:first-child { width: 58px; font-size: 12px; }
}
</style>
