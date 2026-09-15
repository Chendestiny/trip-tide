<template>
  <!-- 单根节点：页面必须只有一个根元素，详见 App.vue 的说明 -->
  <div class="page-root">
    <PhoneShell title="我的行程">
      <template #right>
        <button v-if="briefs.length" class="btn line sm" @click="clearAll">清空</button>
      </template>

      <div class="me">
        <div v-if="briefs.length" class="stats">
          <div class="stat"><strong>{{ briefs.length }}</strong><span>份方案</span></div>
          <div class="stat"><strong>{{ cityCount }}</strong><span>个城市</span></div>
          <div class="stat"><strong>{{ dayTotal }}</strong><span>天行程</span></div>
        </div>

        <div v-if="!briefs.length" class="empty">
          <div class="big">📭</div>
          <p>还没有生成过行程</p>
          <button class="btn ghost sm" style="margin-top: 12px" @click="$router.push('/trip')">去规划一次</button>
        </div>

        <template v-else>
          <div class="sec-title">历史方案</div>
          <div class="hist-grid">
            <button v-for="b in briefs" :key="b.plan_id" class="hist card" @click="open(b)">
              <div class="hist-top">
                <span class="hist-city">{{ b.city }}</span>
                <span class="chip primary">{{ b.days }} 天</span>
                <span class="chip">{{ b.attraction_count }} 景</span>
                <span v-if="b.source === 'fallback'" class="chip hot">规则引擎</span>
                <span v-else class="chip must">AI</span>
              </div>
              <p class="hist-sum">{{ b.summary || b.title }}</p>
              <div class="hist-foot">
                <span>{{ b.created_at }}</span>
                <button class="hist-del" @click.stop="del(b.plan_id)">删除</button>
              </div>
            </button>
          </div>
        </template>
      </div>
    </PhoneShell>

    <TripTabBar active="me" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import PhoneShell from '../../components/trip/PhoneShell.vue'
import TripTabBar from '../../components/trip/TripTabBar.vue'
import { clearHistory, historyBriefs, openHistory, removeHistory, store } from '../../trip-store'

const router = useRouter()

// store.history 是响应式的，briefs 跟着自动更新
const briefs = computed(() => historyBriefs())

const cityCount = computed(() => new Set(briefs.value.map((b) => b.city)).size)
const dayTotal = computed(() => briefs.value.reduce((s, b) => s + (b.days || 0), 0))

function open(b) {
  openHistory(b.plan_id)
  router.push({ path: '/trip/plan', query: { planId: String(b.plan_id) } })
}

function del(planId) {
  if (confirm('删除这份行程方案？')) removeHistory(planId)
}

function clearAll() {
  if (confirm(`清空全部 ${store.history.length} 份历史方案？此操作不可撤销。`)) clearHistory()
}
</script>

<style scoped>
.me { padding-bottom: 34px; }

/* ---------- 统计条 ---------- */
.stats {
  display: flex;
  max-width: 580px;
  margin: 6px var(--gutter) 0;
  padding: 17px 0;
  border-radius: var(--r-lg);
  background:
    radial-gradient(120% 160% at 92% 0%, var(--brand-tint) 0%, transparent 58%),
    var(--surface);
  border: 1px solid var(--line);
  box-shadow: var(--sh-1);
}
.stat { flex: 1; text-align: center; border-right: 1px solid var(--line); }
.stat:last-child { border-right: none; }
.stat strong {
  display: block;
  font-size: 27px; font-weight: 750; color: var(--brand);
  letter-spacing: -0.03em; line-height: 1.15;
}
.stat span { font-size: 12px; color: var(--ink-3); font-weight: 500; }

/* ---------- 历史卡片 ---------- */
.hist-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(336px, 1fr));
  gap: 13px;
}
.hist {
  position: relative; overflow: hidden;
  padding: 15px 16px 14px;
  text-align: left; display: block; width: 100%;
  border-radius: var(--r-lg);
  background: var(--surface);
  border: 1px solid var(--line);
  box-shadow: var(--sh-1);
  transition: transform 0.22s var(--ease), border-color 0.22s var(--ease),
    box-shadow 0.22s var(--ease);
}
.hist::before {
  content: '';
  position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
  background: linear-gradient(180deg, var(--brand), #17a7b1);
  transform: scaleY(0); transform-origin: top;
  transition: transform 0.26s var(--ease);
}
.hist:hover {
  transform: translateY(-3px);
  border-color: var(--brand);
  box-shadow: var(--sh-3);
}
.hist:hover::before { transform: scaleY(1); }
.hist:active { transform: translateY(-1px) scale(0.995); }

.hist-top { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.hist-city {
  font-size: 17px; font-weight: 720; letter-spacing: -0.02em;
  margin-right: 3px;
}
.hist-sum {
  margin: 9px 0 11px;
  font-size: 13px; color: var(--ink-2); line-height: 1.64;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; min-height: 58px;
}
.hist-foot {
  display: flex; align-items: center; justify-content: space-between;
  padding-top: 10px;
  font-size: 11.5px; color: var(--ink-4);
  border-top: 1px solid var(--line);
}
.hist-del {
  font-size: 12px; color: #c4534f; padding: 3px 8px; border-radius: 6px;
  transition: background 0.16s var(--ease);
}
.hist-del:hover { background: #fdeceb; }

/* ---------- 手机形态 ---------- */
@media (max-width: 640px) {
  .me { padding-bottom: 24px; }
  .stats { margin: 13px var(--gutter) 0; padding: 13px 0; border-radius: var(--r); }
  .stat strong { font-size: 21px; }
  .stat span { font-size: 11px; }

  .hist-grid { display: flex; flex-direction: column; gap: 9px; }
  .hist { padding: 13px 14px 12px; border-radius: var(--r); }
  .hist:hover { transform: none; box-shadow: var(--sh-1); }
  .hist-city { font-size: 15.5px; }
  .hist-sum {
    font-size: 12.5px; margin: 8px 0 10px;
    min-height: 0; -webkit-line-clamp: 2;
  }
  .hist-foot { padding-top: 9px; }
}
</style>
