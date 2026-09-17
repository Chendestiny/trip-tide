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

        <!-- 城市 | 区域 两个入口（kind: city / region） -->
        <div class="home-tabs">
          <button :class="{ on: tab === 'city' }" @click="tab = 'city'">城市</button>
          <button :class="{ on: tab === 'region' }" @click="tab = 'region'">区域</button>
        </div>

        <!-- 检索：按名称 / 拼音 / 一句话简介 / 所属大区匹配（纯前端过滤，不打接口） -->
        <div class="home-search">
          <svg class="search-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.3" fill="none" stroke="currentColor" stroke-width="2" /><path d="M15.6 15.6 L20.4 20.4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" /></svg>
          <input
            v-model="q"
            type="text"
            :placeholder="tab === 'city' ? '搜索城市名或拼音' : '搜索区域'"
            aria-label="搜索目的地"
          />
          <button v-if="q" class="search-clear" aria-label="清空搜索" @click="q = ''">✕</button>
        </div>

        <!-- 只在「搜索有命中」时出现，显示命中数；不搜索时整行不渲染（原来这里是一句固定的 tab 提示语） -->
        <p v-if="tabNote" class="home-tab-note">{{ tabNote }}</p>

        <div v-if="loading" class="city-grid">
          <div v-for="i in 8" :key="i" class="city-card sk-card" aria-hidden="true">
            <span class="sk-emoji" />
            <span class="sk-line sk-w52" />
            <span class="sk-line sk-w88" />
            <span class="sk-line sk-w40" />
          </div>
        </div>

        <div v-else-if="error" class="empty">
          <div class="big">🔌</div>
          <p>{{ error }}</p>
          <button class="btn ghost sm" style="margin-top: 12px" @click="load">重新加载</button>
        </div>

        <div v-else class="city-grid">
          <button v-for="c in shown" :key="c.id" class="city-card" @click="goPick(c)">
            <span class="city-emoji">{{ c.emoji || '📍' }}</span>
            <span v-if="c.kind === 'region'" class="chip kind-tag">环线</span>
            <span class="city-name">{{ c.name }}</span>
            <span class="city-tag">{{ c.tagline }}</span>
            <span class="city-count">{{ c.attraction_count }} 个景点</span>
          </button>
        </div>

        <p v-if="!loading && !error && !shown.length" class="hint">
          <template v-if="q.trim()">没有匹配「{{ q.trim() }}」的目的地</template>
          <template v-else-if="tab === 'region'">区域游目的地筹备中，敬请期待</template>
          <template v-else>库里还没有城市数据。先跑一次初始化：<code>npm run seed</code></template>
        </p>
      </div>
    </PhoneShell>

    <TripTabBar active="home" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PhoneShell from '../../components/trip/PhoneShell.vue'
import TripTabBar from '../../components/trip/TripTabBar.vue'
import { getCities, health } from '@api'

const router = useRouter()
const route = useRoute()
const cities = ref([])
const loading = ref(true)
const error = ref('')
const h = ref(null)
// tab 同步进 URL（?tab=region）：本组件在 /trip ↔ /trip/pick 之间没有 keep-alive，
// 从 pick 返回会整个重挂载 —— 只存 ref 的话 tab 必回「城市」。
// 初值从 query 读，变化时 router.replace 写回（city 时不带参数，保持 /trip 干净）。
const tab = ref(route.query.tab === 'region' ? 'region' : 'city')
watch(tab, (v) => {
  router.replace({ query: { ...route.query, tab: v === 'city' ? undefined : v } })
})
const q = ref('')

// 城市 = 单城+周边（现状）；区域 = 环线游（贵州这类多基地目的地）
const shown = computed(() => {
  const kw = q.value.trim().toLowerCase()
  return cities.value
    .filter((c) => (c.kind || 'city') === tab.value)
    .filter((c) => {
      if (!kw) return true
      // 拼音也参与匹配：「cd」「chengdu」都能搜到成都
      const hay = [c.name, c.pinyin, c.tagline, c.region].filter(Boolean).join(' ').toLowerCase()
      return hay.includes(kw)
    })
})

// 只有「正在搜索且搜到了」时有内容 —— 显示命中数。
// 不搜索（或没搜到，那种情况由下面 .hint 的空态接管）时返回空串，整行不渲染。
// 原来这里还有一句固定的 tab 提示语（「选一个城市，勾景点就出发」/「一次玩一条线…」），已去掉。
const tabNote = computed(() => {
  if (!q.value.trim() || !shown.value.length) return ''
  return `找到 ${shown.value.length} 个`
})

const engineTip = computed(() => {
  if (!h.value) return null
  // 离线版（单文件 HTML）没有后端也没有模型调用，说清楚「算在哪」比说「没配 Key」有用
  if (h.value.db === 'offline') {
    return { level: 'ok', icon: '🧭', text: '离线演示版 · 路线由本地规则引擎在你的浏览器里算出' }
  }
  if (!h.value.llm_ready) {
    return { level: 'warn', icon: '⚙️', text: '未配置 DeepSeek Key，当前由本地规则引擎生成方案' }
  }
  return { level: 'ok', icon: '✨', text: `AI 引擎已就绪 · ${h.value.model}` }
})

/**
 * 拉目的地列表。
 *
 * 带退避重试：`npm run dev` 时 vite 比 uvicorn 快约 2s（后端要先连库、跑建表与轻量迁移），
 * 浏览器抢先发的请求会拿到 ECONNREFUSED。重试两次就自动过去了，
 * 用户不会一进首页就看到红字（这不是后端故障，见 DEVELOPMENT.md 排查表）。
 */
async function load() {
  loading.value = true
  error.value = ''
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      cities.value = await getCities()
      loading.value = false
      return
    } catch (e) {
      const retryable = e.message.includes('连不上后端') && attempt < 2
      if (!retryable) {
        error.value = e.message
        loading.value = false
        return
      }
      await new Promise((r) => setTimeout(r, 1200))
    }
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
  margin: 0 var(--gutter) 4px;
  padding: 26px 26px 22px;
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
  padding: 3px 10px; border-radius: 6px;
  background: rgba(255, 255, 255, 0.15);
  border: 1px solid rgba(255, 255, 255, 0.18);
  backdrop-filter: blur(6px);
  font-size: 12px; letter-spacing: 0.1em; font-weight: 600;
}
.hero h1 {
  margin: 10px 0 7px;
  font-size: 38px; line-height: 1.24;
  font-weight: 750; letter-spacing: -0.032em;
  text-shadow: 0 2px 18px rgba(6, 52, 56, 0.28);
}
.hero p {
  margin: 0; max-width: 720px;
  font-size: 15.5px; line-height: 1.6;
  color: rgba(255, 255, 255, 0.86);
}
.engine-tip {
  margin-top: 11px; padding: 7px 10px; border-radius: 8px;
  display: inline-flex; gap: 8px; align-items: center;
  font-size: 13px; font-weight: 500;
  background: rgba(255, 255, 255, 0.14);
  border: 1px solid rgba(255, 255, 255, 0.16);
  backdrop-filter: blur(6px);
}
.engine-tip.warn { background: rgba(255, 176, 96, 0.24); border-color: rgba(255, 200, 140, 0.3); }

/* ================================================================
   城市 | 区域 tab
   ================================================================ */
/* ================================================================
   城市 | 区域 tab —— 与 TripPick 的 .mode-tabs 同一套视觉：
   撑满宽度、容器带边框（否则底色 --surface-3 与页面底 --bg 太接近分不开）
   ================================================================ */
.home-tabs {
  display: flex; gap: 4px;
  margin: 0 var(--gutter) 9px;
  padding: 3px; border-radius: 10px;
  background: var(--surface-3);
  border: 1px solid var(--line);
}
.home-tabs button {
  flex: 1; padding: 7px 18px; border-radius: 8px;
  font-size: 13.5px; font-weight: 620; color: var(--ink-2);
  transition: background 0.16s var(--ease), color 0.16s var(--ease), box-shadow 0.16s var(--ease);
}
.home-tabs button:hover { color: var(--ink); }
.home-tabs button.on {
  background: var(--surface); color: var(--brand-deep); font-weight: 680;
  box-shadow: var(--sh-1);
}
.home-tab-note {
  display: block;
  margin: 0 var(--gutter) 12px;
  font-size: 12px; color: var(--ink-4);
}

/* ---------- 目的地检索 ---------- */
.home-search {
  display: flex; align-items: center; gap: 8px;
  margin: 0 var(--gutter) 14px;
  padding: 0 12px; height: 42px;
  border-radius: var(--r);
  background: var(--surface);
  border: 1px solid var(--line);
  transition: border-color 0.18s var(--ease), box-shadow 0.18s var(--ease);
}
.home-search:focus-within {
  border-color: var(--brand);
  box-shadow: 0 0 0 3px var(--brand-soft);
}
.search-icon { width: 16px; height: 16px; color: var(--ink-4); flex-shrink: 0; transition: color 0.18s var(--ease); }
.home-search:focus-within .search-icon { color: var(--brand); }
.home-search input {
  flex: 1; min-width: 0;
  border: none; outline: none; background: none;
  font-size: 14px; color: var(--ink);
}
.home-search input::placeholder { color: var(--ink-4); }
.search-clear {
  flex-shrink: 0; width: 20px; height: 20px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; color: var(--ink-3);
  background: var(--surface-3);
  transition: background 0.16s var(--ease), color 0.16s var(--ease);
}
.search-clear:hover { background: var(--line-2); color: var(--ink); }

.kind-tag {
  position: absolute; right: 12px; top: 12px;
  background: var(--gold-soft); color: #9c6c14;
  font-size: 10.5px; font-weight: 680; padding: 2px 8px;
  border-radius: 6px; z-index: 2;
}

/* ================================================================
   城市宫格
   ================================================================ */
.city-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(238px, 1fr));
  gap: 14px;
  /* 必须自己带 gutter：.shell-body 没有左右内边距，而 .hero / .home-tabs
     都是各自加 margin，漏了这行宫格会比 hero 宽出左右各 24px */
  margin: 0 var(--gutter);
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

/* 骨架屏：形状按真实卡片摆（emoji 方块 + 三行文字条）。
   .sk-emoji / .sk-line 的底色与 shimmer 动画是通用的，定义在 style.css */
.sk-card { pointer-events: none; }
.sk-w52 { width: 52%; }
.sk-w88 { width: 88%; }
.sk-w40 { width: 40%; margin-top: 7px; }

.hint { font-size: 13px; color: var(--ink-3); text-align: center; margin: 20px 0 0; }
.hint code {
  background: var(--surface-3); padding: 2px 6px; border-radius: 5px;
  font-size: 12.5px; color: var(--ink-2);
}

/* ================================================================
   手机形态
   ================================================================ */
@media (max-width: 640px) {
  /* App 形态：固定头部 + 只有宫格滚动。
     .home 撑满 .shell-body 的高度并转成纵向 flex，hero / tab / 搜索栏都不收缩，
     剩余高度全给 .city-grid 自己滚 —— 这样内容不溢出，外层 .shell-body 自然没有滚动条 */
  .home {
    display: flex; flex-direction: column;
    flex: 1; min-height: 0; padding-bottom: 0;
  }
  .hero, .home-tabs, .home-search, .home-tab-note { flex-shrink: 0; }

  .hero {
    margin: 0; padding: 16px 16px 14px; border-radius: 0;
    box-shadow: none;
  }
  .hero h1 { font-size: 23px; margin: 7px 0 5px; letter-spacing: -0.022em; }
  .hero p { font-size: 13px; line-height: 1.55; }
  .hero-badge { padding: 2px 8px; font-size: 11px; }
  .engine-tip { margin-top: 8px; padding: 6px 9px; font-size: 12px; align-items: flex-start; }
  .hero-art { display: none; }

  .city-grid {
    grid-template-columns: 1fr 1fr; gap: 10px;
    /* ⚠️ 这行不能删：手机端宫格是「定高滚动容器」（flex:1 + min-height:0 + overflow-y:auto），
       此时 `grid-auto-rows: auto` 的隐式轨道会退到**最小贡献**而不是内容高度 ——
       实测一张卡片只分到 26px（内容其实要 164px），卡片被自己的 overflow:hidden 裁成一条，
       只剩下 emoji 的上半截，城市名/简介/景点数全看不见。
       显式写 max-content 让它按内容撑高即可。桌面端宫格没有定高，两种写法结果一样。 */
    grid-auto-rows: max-content;
    flex: 1; min-height: 0;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    align-content: start;
    padding-bottom: 22px;
  }
  .home-tabs { margin-bottom: 8px; }
  .home-tabs button { padding: 6px 14px; font-size: 12.5px; }
  .home-search { height: 40px; margin-bottom: 11px; }
  .home-tab-note { margin-bottom: 10px; font-size: 11.5px; }
  .city-card { padding: 13px 12px 11px; border-radius: var(--r); }
  .city-card:hover { transform: none; box-shadow: var(--sh-1); }
  .city-emoji { width: 36px; height: 36px; border-radius: 9px; font-size: 19px; }
  .city-name { font-size: 16px; margin-top: 9px; }
  .city-tag { font-size: 11.5px; min-height: 30px; }
  .city-count { margin-top: 8px; padding-top: 8px; font-size: 11px; }
  .sk-card .sk-emoji { width: 36px; height: 36px; border-radius: 9px; }
}
</style>
