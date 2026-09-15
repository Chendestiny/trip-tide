<template>
  <!-- 单根节点：见 App.vue 的说明 -->
  <div class="page-root">
    <PhoneShell>
      <div class="home">
        <section class="hero">
          <div class="hero-badge">国内自由行 · 智能编排</div>
          <h1>选个城市，剩下的交给 AI</h1>
          <p>勾选想去的景点，自动排出按天路线：顺路不折返、饭点自动插餐、给出住宿片区与取舍理由。</p>
          <div v-if="engineTip" :class="['engine-tip', engineTip.level]">
            <span>{{ engineTip.icon }}</span>{{ engineTip.text }}
          </div>

          <!-- 装饰：抽象路线图（纯装饰，aria-hidden） -->
          <svg class="hero-art" viewBox="0 0 340 220" aria-hidden="true">
            <path
              d="M18 178 C 74 178, 62 104, 132 104 S 208 52, 322 46"
              fill="none" stroke="rgba(255,255,255,0.34)" stroke-width="1.6"
              stroke-dasharray="5 7" stroke-linecap="round"
            />
            <circle cx="18" cy="178" r="4.5" fill="rgba(255,255,255,0.75)" />
            <circle cx="132" cy="104" r="4.5" fill="rgba(255,255,255,0.75)" />
            <circle cx="322" cy="46" r="4.5" fill="rgba(255,255,255,0.75)" />
            <circle cx="132" cy="104" r="13" fill="none" stroke="rgba(255,255,255,0.3)" stroke-width="1.2" />
            <circle cx="132" cy="104" r="21" fill="none" stroke="rgba(255,255,255,0.14)" stroke-width="1" />
          </svg>
        </section>

        <div class="sec-title">热门城市</div>

        <div v-if="loading" class="city-grid">
          <div v-for="i in 8" :key="i" class="city-card skeleton" />
        </div>

        <div v-else-if="error" class="empty">
          <div class="big">🔌</div>
          <p>{{ error }}</p>
          <button class="btn ghost sm" style="margin-top: 12px" @click="load">重新加载</button>
        </div>

        <div v-else class="city-grid">
          <button v-for="c in cities" :key="c.id" class="city-card" @click="goPick(c)">
            <span class="city-emoji">{{ c.emoji || '📍' }}</span>
            <span class="city-name">{{ c.name }}</span>
            <span class="city-tag">{{ c.tagline }}</span>
            <span class="city-count">{{ c.attraction_count }} 个景点</span>
          </button>
        </div>

        <p v-if="!loading && !error && !cities.length" class="hint">
          库里还没有城市数据。先跑一次初始化：<code>npm run seed</code>
        </p>
      </div>
    </PhoneShell>

    <TripTabBar active="home" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PhoneShell from '../../components/trip/PhoneShell.vue'
import TripTabBar from '../../components/trip/TripTabBar.vue'
import { getCities, health } from '../../trip-api'

const router = useRouter()
const cities = ref([])
const loading = ref(true)
const error = ref('')
const h = ref(null)

const engineTip = computed(() => {
  if (!h.value) return null
  if (!h.value.llm_ready) {
    return { level: 'warn', icon: '⚙️', text: '未配置 DeepSeek Key，当前由本地规则引擎生成方案' }
  }
  return { level: 'ok', icon: '✨', text: `AI 引擎已就绪 · ${h.value.model}` }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    cities.value = await getCities()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function goPick(city) {
  router.push({ path: '/trip/pick', query: { city: city.name } })
}

onMounted(() => {
  load()
  health().then((d) => { h.value = d }).catch(() => {})
})
</script>

<style scoped>
.home { padding-bottom: 30px; }

/* ================================================================
   Hero：多层径向渐变做柔和的「极光」底，叠一层细点阵
   ================================================================ */
.hero {
  position: relative;
  margin: 6px var(--gutter) 4px;
  padding: 40px 36px 34px;
  border-radius: var(--r-xl);
  overflow: hidden;
  color: #fff;
  background:
    radial-gradient(120% 140% at 88% 8%, rgba(23, 167, 177, 0.55) 0%, transparent 52%),
    radial-gradient(90% 120% at 6% 96%, rgba(255, 107, 53, 0.34) 0%, transparent 55%),
    linear-gradient(142deg, #0e8189 0%, #0a5f66 52%, #07454a 100%);
  box-shadow: 0 2px 6px rgba(8, 73, 78, 0.14), 0 24px 48px -20px rgba(8, 73, 78, 0.5);
}
.hero::before {
  content: '';
  position: absolute; inset: 0;
  background-image: radial-gradient(rgba(255, 255, 255, 0.16) 1px, transparent 1px);
  background-size: 22px 22px;
  opacity: 0.5;
  mask-image: linear-gradient(150deg, #000 0%, transparent 62%);
  -webkit-mask-image: linear-gradient(150deg, #000 0%, transparent 62%);
}
.hero::after {
  content: '';
  position: absolute; left: 0; right: 0; top: 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.42), transparent);
}
.hero > * { position: relative; z-index: 1; }
.hero-art {
  position: absolute; right: 26px; bottom: 10px;
  width: 340px; height: 220px; z-index: 0;
  pointer-events: none;
  opacity: 0.9;
}

.hero-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 11px; border-radius: 7px;
  background: rgba(255, 255, 255, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.18);
  backdrop-filter: blur(6px);
  font-size: 12px; letter-spacing: 0.1em; font-weight: 600;
}
.hero h1 {
  margin: 17px 0 12px;
  font-size: 38px; line-height: 1.24;
  font-weight: 750; letter-spacing: -0.032em;
  text-shadow: 0 2px 18px rgba(6, 52, 56, 0.28);
}
.hero p {
  margin: 0; max-width: 720px;
  font-size: 15.5px; line-height: 1.66;
  color: rgba(255, 255, 255, 0.86);
}
.engine-tip {
  margin-top: 18px; padding: 10px 13px; border-radius: 9px;
  display: inline-flex; gap: 9px; align-items: center;
  font-size: 13px; font-weight: 500;
  background: rgba(255, 255, 255, 0.14);
  border: 1px solid rgba(255, 255, 255, 0.16);
  backdrop-filter: blur(6px);
}
.engine-tip.warn { background: rgba(255, 176, 96, 0.24); border-color: rgba(255, 200, 140, 0.3); }

/* ================================================================
   城市宫格
   ================================================================ */
.city-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(238px, 1fr));
  gap: 14px;
}
.city-card {
  position: relative; overflow: hidden;
  text-align: left;
  padding: 17px 16px 15px;
  border-radius: var(--r-lg);
  background: var(--surface);
  border: 1px solid var(--line);
  box-shadow: var(--sh-1);
  display: flex; flex-direction: column;
  transition: transform 0.22s var(--ease), border-color 0.22s var(--ease),
    box-shadow 0.22s var(--ease);
}
/* 右上角的柔和光斑，hover 时放大 */
.city-card::after {
  content: '';
  position: absolute; right: -46px; top: -46px;
  width: 128px; height: 128px; border-radius: 50%;
  background: radial-gradient(circle, var(--brand-soft) 0%, transparent 70%);
  transition: transform 0.34s var(--ease), opacity 0.3s var(--ease);
  opacity: 0.85;
}
.city-card:hover {
  transform: translateY(-3px);
  border-color: var(--brand);
  box-shadow: var(--sh-3);
}
.city-card:hover::after { transform: scale(1.5); opacity: 1; }
.city-card:active { transform: translateY(-1px) scale(0.99); }
.city-card > * { position: relative; z-index: 1; }

.city-emoji {
  width: 44px; height: 44px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  font-size: 24px; line-height: 1;
  background: var(--brand-soft);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.5), var(--sh-1);
}
/* 八色循环，让宫格看起来有颜色而不是一片灰 */
.city-card:nth-child(8n+2) .city-emoji { background: var(--accent-soft); }
.city-card:nth-child(8n+3) .city-emoji { background: #eae6fb; }
.city-card:nth-child(8n+4) .city-emoji { background: var(--gold-soft); }
.city-card:nth-child(8n+5) .city-emoji { background: #e6f2ea; }
.city-card:nth-child(8n+6) .city-emoji { background: #e6eefb; }
.city-card:nth-child(8n+7) .city-emoji { background: #fdeaf1; }
.city-card:nth-child(8n+8) .city-emoji { background: #eaf1f0; }
.city-name {
  font-size: 19px; font-weight: 720; letter-spacing: -0.02em;
  margin-top: 11px;
}
.city-tag {
  font-size: 12.5px; color: var(--ink-3); line-height: 1.55;
  margin-top: 3px; min-height: 39px;
}
.city-count {
  display: inline-flex; align-items: center; gap: 5px;
  margin-top: 10px; padding-top: 9px;
  border-top: 1px solid var(--line);
  font-size: 12px; font-weight: 620; color: var(--brand);
}
.city-count::before {
  content: ''; width: 5px; height: 5px; border-radius: 50%;
  background: var(--brand); flex-shrink: 0;
}

.skeleton {
  height: 156px; border-radius: var(--r-lg);
  background: linear-gradient(100deg, var(--surface-2) 30%, var(--surface-3) 50%, var(--surface-2) 70%);
  background-size: 220% 100%;
  animation: sh 1.4s linear infinite;
  border: 1px solid var(--line);
}
@keyframes sh { to { background-position: -220% 0; } }

.hint { font-size: 13px; color: var(--ink-3); text-align: center; margin: 20px 0 0; }
.hint code {
  background: var(--surface-3); padding: 2px 6px; border-radius: 5px;
  font-size: 12.5px; color: var(--ink-2);
}

/* ================================================================
   手机形态
   ================================================================ */
@media (max-width: 640px) {
  .home { padding-bottom: 22px; }

  .hero {
    margin: 0; padding: 24px 18px 22px; border-radius: 0;
    box-shadow: none;
  }
  .hero h1 { font-size: 23px; margin: 11px 0 8px; letter-spacing: -0.022em; }
  .hero p { font-size: 13px; line-height: 1.66; }
  .hero-badge { padding: 3px 9px; font-size: 11px; }
  .engine-tip { margin-top: 13px; padding: 8px 11px; font-size: 12px; align-items: flex-start; }
  .hero-art { display: none; }

  .city-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
  .city-card { padding: 13px 12px 11px; border-radius: var(--r); }
  .city-card:hover { transform: none; box-shadow: var(--sh-1); }
  .city-emoji { width: 36px; height: 36px; border-radius: 9px; font-size: 19px; }
  .city-name { font-size: 16px; margin-top: 9px; }
  .city-tag { font-size: 11.5px; min-height: 30px; }
  .city-count { margin-top: 8px; padding-top: 8px; font-size: 11px; }
  .skeleton { height: 120px; border-radius: var(--r); }
}
</style>
