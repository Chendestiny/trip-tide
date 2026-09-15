# 前端地图

> 符号级导航。**行号基于当前代码**，对不上就是代码变了——以代码为准。
> 全局认知看 [`README.md`](README.md)；接口契约看 [`API.md`](API.md)；时间轴字段看 [`PLAN_SCHEMA.md`](PLAN_SCHEMA.md)。

---

## 技术栈与形态

| 维度 | 事实 |
|---|---|
| 框架 | Vue 3（全部 `<script setup>`）+ Vite |
| 路由 | `vue-router` 4，`createWebHistory` |
| 状态 | **无 Pinia / Vuex / 任何状态库**。一个 `reactive` 对象 + `localStorage` 就够 |
| UI 框架 | **无**。4 个页面全部自绘 |
| 请求 | 原生 `fetch` + `AbortController` 超时控制 |
| 样式 | 单文件 `style.css`（设计系统）+ 各组件 `<style scoped>` |
| 响应式 | **桌面优先**，唯一断点 `max-width: 640px` |

---

## 目录结构

```
frontend/
├── index.html              极简入口（无外部字体/CDN）
├── vite.config.js          port 5173；proxy /api → http://127.0.0.1:8000
├── .env                    VITE_AMAP_JS_KEY / VITE_AMAP_SECURITY_CODE
└── src/
    ├── main.js             路由表 + 4 层全局错误兜底
    ├── App.vue             站点骨架（SiteHeader + router-view）
    ├── style.css           设计系统：CSS 变量 + reset + 共用类 + 手机覆盖
    ├── trip-api.js         8 个接口封装 + 错误文案归一
    ├── trip-store.js       localStorage：历史方案 + 上次勾选偏好
    ├── trip-theme.js       按天配色 / 节点图标 / 节奏标签（单一真源）
    ├── use-media.js        视口断点（matchMedia）
    ├── components/trip/
    │   ├── SiteHeader.vue  顶部导航（仅宽屏可见）
    │   ├── PhoneShell.vue  页面外壳：标题行 + 滚动主体 + 3 个 slot
    │   ├── TripTabBar.vue  底部标签栏（仅手机可见）
    │   └── TripMap.vue     半屏地图（高德 JS API，缺失/失败降级 SVG）
    └── views/trip/
        ├── TripHome.vue    ① 城市宫格
        ├── TripPick.vue    ② 景点勾选 + 设置面板 + 紧凑度预估
        ├── TripPlan.vue    ③ 按天时间轴（核心页）
        └── TripMe.vue      ④ 历史方案
```

> 所有 `.vue` 都**没有声明组件 `name`**（无 `defineOptions`），组件名由 SFC 编译器按文件名推断。

---

## 路由（`main.js:46–69`）

| 行号 | path | name | 组件 | meta.title |
|---|---|---|---|---|
| 49 | `/` | — | — | `redirect: '/trip'` |
| 50 | `/trip` | `trip-home` | TripHome | `AI 行程规划师` |
| 51 | `/trip/pick` | `trip-pick` | TripPick | `选景点 · …` |
| 52 | `/trip/plan` | `trip-plan` | TripPlan | `行程方案 · …` |
| 53 | `/trip/me` | `trip-me` | TripMe | `我的行程 · …` |
| 54 | `/:pathMatch(.*)*` | — | — | `redirect: '/trip'` |

- **没有 404 页面组件**，通配是 redirect。
- `scrollBehavior`(56) 固定返回 `{ top: 0 }`。
- `afterEach`(61) 写 `document.title`；`onError`(66) 调 `renderFatal`。

### 全局错误兜底（`main.js`）— 4 层

| 行号 | 机制 |
|---|---|
| 66–69 | `router.onError` → `renderFatal(err, 'router')` |
| 73–76 | `app.config.errorHandler` → `renderFatal(err, info)` |
| 81 | `window.addEventListener('error', ...)` → `renderFatal(..., 'window')` |
| 82 | `window.addEventListener('unhandledrejection', ...)` → `renderFatal(..., 'promise')` |

`renderFatal(err, info)`(18–44)：把异常渲染成**可见的错误面板**（标题「页面渲染出错」+ 堆栈 + 刷新按钮），而不是白屏。
**防误报守卫(22)**：若 `#app` 已有超过 40 字符内容就放弃覆盖，避免把局部错误放大成整页。

> 这是为「白屏但硬刷新就好」那个坑加的防复发措施。**别删。**

---

## 状态与持久化（`trip-store.js`）

**V1 没有账号体系**，全部状态在 localStorage。

| key | 行号 | 内容 |
|---|---|---|
| `triptide.history.v1` | 6 | `Array<PlanResponse>`，最新在前，**上限 30 条**（`MAX_HISTORY` 7 行） |
| `triptide.pref.v1` | 81 | `{ city, pref }`，上次在某城市用的设置 |

| 导出 | 行号 | 行为 |
|---|---|---|
| `store`（`reactive`） | 30 | **只有两个字段**：`current`（当前方案快照）、`history`（初始化时 `readHistory()`） |
| `hasCurrent` | 37 | computed。**全项目零引用** |
| `commitPlan(plan)` | 40 | 设 `current`，按 `plan_id` 去重后头插并截断 30 条 |
| `openHistory(planId)` | 48 | 命中则设 `current`，**返回命中项或 `null`** |
| `removeHistory(planId)` | 54 | 删除并持久化；若删的是 `current` 则置 `null` |
| `clearHistory()` | 60 | 清空 history + current |
| `historyBriefs()` | 67 | **普通函数（非 computed）**，map 出轻量条目 |
| `savePref(city, pref)` | 83 | try/catch 静默失败 |
| `loadPref(city)` | 89 | 仅在 `raw.city === city` 时返回 `raw.pref`，否则 `null` |

`writeHistory`(19–28) 有**配额溢出兜底**：失败后砍到 15 条重试一次，再失败静默放弃（不阻断主流程）。

`historyBriefs()` 单条形状（68–77）：`{ plan_id, city, days, title, source, created_at, attraction_count, summary }`。

> 勾选状态**不在 store 里**，是 `TripPick.vue` 的组件内 `ref`；store 只负责把它序列化到 `triptide.pref.v1`。

---

## 请求层（`trip-api.js`）

`BASE = '/api/trip'`(5)，dev 下由 Vite 代理到 `127.0.0.1:8000`。

| 超时常量 | 行号 | 值 |
|---|---|---|
| `PLAN_TIMEOUT` | 8 | `150000`（150s） |
| `DEFAULT_TIMEOUT` | 9 | `15000`（15s） |

核心 `request(path, { method, body, timeout })`(11–45)：`AbortController` + `setTimeout`；仅带 body 时加 `Content-Type`；先 `r.text()` 再 try `JSON.parse`（解析失败静默置 `null`）。

| 导出 | 行号 | 方法 + 路径 | 超时 |
|---|---|---|---|
| `getCities()` | 48 | `GET /cities` | 15s |
| `getAttractions(city)` | 51 | `GET /attractions?city=` | 15s |
| `previewPlan(payload)` | 54 | `POST /preview` | **12s** |
| `createPlan(payload)` | 58 | `POST /plan` | **150s** |
| `adjustPlan(planId, payload)` | 62 | `POST /plan/{id}/adjust` | 15s |
| `getPlan(id)` | 66 | `GET /plan/{id}` | 15s |
| `listPlans(limit=30)` | 69 | `GET /plans` | 15s |
| `health()` | 72 | `GET /api/health`（原生 fetch，不走 `request`） | 无 |

**错误文案**（全部 `throw new Error(...)`，`e.message` 精确如下）：

| 场景 | 行号 | 文案 |
|---|---|---|
| `!r.ok` 且 `detail` 是字符串 | 27 | 原文透传 |
| `detail` 是数组 | 29 | `detail.map(d => d.msg).join('；')` |
| 无 detail | 30 | `请求失败（HTTP {status}）` |
| 超时且是 `PLAN_TIMEOUT` | 36 | `AI 规划超时了（150s），请减少景点数量或稍后重试` |
| 其它超时 | 38 | `请求超时，请检查后端是否已启动` |
| `TypeError`（网络层） | 40 | `连不上后端服务，请确认 8000 端口已启动` |

> `listPlans`(69) **全项目零引用**——「我的」页读的是 localStorage，这个接口留给接入登录后切换。

---

## 视觉常量（`trip-theme.js`）

**地图、时间轴、图例的唯一配色真源。**

| 导出 | 行号 | 内容 |
|---|---|---|
| `DAY_COLORS` | 4 | **7 色**数组 |
| `dayColor(day)` | 6 | `DAY_COLORS[(max(1,day) - 1) % 7]` |
| `nodeVisual(type, transport='mixed')` | 9 | 返回 `{ icon, label }` |
| `paceStyle(pace)` | 30 | 返回 `{ bg, fg }`（入参是**中文标签**「轻松/适中/紧凑」） |
| `TRANSPORT_LABEL` | 37 | `drive: 自驾` / `mixed: 打车+公共` / `transit: 公共交通` / `taxi: 打车+公共` |
| `PACE_LABEL` | 45 | `relaxed: 宽松` / `balanced: 平衡` / `packed: 紧凑` |

**`DAY_COLORS` 顺序（≥8 天回到第 1 色）**

| Day | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| 色值 | `#0E7C86` | `#FF7A45` | `#7C5CE0` | `#2E9E5B` | `#D9541F` | `#3B7DD8` | `#B57B13` |

**`nodeVisual` 分派表**

| type | icon | label |
|---|---|---|
| `depart` | `drive→🚗` / `transit→🚇` / 其它（含 `mixed`）`→🚕` | 出发 |
| `attraction` | 📍 | 景点 |
| `meal` | 🍜 | 餐饮 |
| `hotel` | 🏨 | 住宿 |
| `transit` | 🚶 | 活动 |
| 未知 | • | `''` |

**`paceStyle`**

| pace | bg | fg |
|---|---|---|
| 轻松 | `#e8f5ec` | `#2e7d4f` |
| 适中 | `#e6f0f7` | `#2a6a8c` |
| 紧凑 | `#fdece4` | `#c05a24` |
| 其它 | `#f0f3f4` | `#5b6b74` |

> ⚠️ `DAY_COLORS`(4) 与 `PACE_LABEL`(45) **零引用**：`TripPlan.vue:255` 本地重复定义了一份 `PACE_LABEL`，内容与 theme 里的一致。改配色时注意别只改一处。

---

## 响应式（`use-media.js`）

```js
MOBILE_MAX = 640                                    // 行 10，模块私有
useMediaQuery(query) → ref<boolean>                 // 行 12，matchMedia + addEventListener('change')
useIsWide() → useMediaQuery('(min-width: 641px)')   // 行 26
```

- **用 `matchMedia`，不是 `resize` 监听**；`onUnmounted` 里 removeEventListener。
- 初始化时同步取 `mql.matches`（16 行），避免首帧闪错形态。
- SSR / 不支持 matchMedia 时降级 `ref(false)`。

**只有两处需要 JS 知道形态**（其余全是 CSS）：

| 位置 | 差异 |
|---|---|
| `TripPick.vue` | 设置面板：宽屏是常驻侧栏，手机是折叠的底部浮条（`showSet` 控制） |
| `TripPlan.vue` | 地图按钮：宽屏在页头 `#right`，手机在底部 `#footer` |

---

## 设计系统（`style.css`，372 行）

### `:root` 变量全表（11–61 行，共 32 个）

**底色**

| 变量 | 值 | 变量 | 值 |
|---|---|---|---|
| `--bg` | `#f2f4f3` | `--line` | `#e5eae8` |
| `--surface` | `#ffffff` | `--line-2` | `#d3dbd8` |
| `--surface-2` | `#f8faf9` | | |
| `--surface-3` | `#eef1f0` | | |

**品牌（青绿）**

| 变量 | 值 | 变量 | 值 |
|---|---|---|---|
| `--brand` | `#0d7d86` | `--brand-soft` | `#e4f2f3` |
| `--brand-dark` | `#0a5f66` | `--brand-tint` | `#f2fafb` |
| `--brand-deep` | `#08494e` | | |

**强调**

| 变量 | 值 | 变量 | 值 |
|---|---|---|---|
| `--accent` | `#ff6b35` | `--gold` | `#d99b2b` |
| `--accent-dark` | `#e05523` | `--gold-soft` | `#fdf6e6` |
| `--accent-soft` | `#fff0e9` | `--violet` | `#7c5ce0` |

**文字**

| 变量 | 值 |
|---|---|
| `--ink` | `#101d23` |
| `--ink-2` | `#4b5a62` |
| `--ink-3` | `#8a99a1` |
| `--ink-4` | `#aab6bc` |

**圆角 / 阴影 / 布局**

| 变量 | 值 | 变量 | 值 |
|---|---|---|---|
| `--r-sm` | `9px` | `--sh-1` | 双层 1–3px 微阴影 |
| `--r` | `14px` | `--sh-2` | 2px + 6/16px |
| `--r-lg` | `18px` | `--sh-3` | 4px + 16/36px |
| `--r-xl` | `24px` | `--sh-brand` | 品牌色阴影 |
| `--shell-max` | `1120px` | `--sh-up` | 上向阴影（底部浮条） |
| `--gutter` | `28px` | `--ease` | `cubic-bezier(0.22, 0.61, 0.36, 1)` |
| `--nav-h` | `60px` | `--head-h` | `64px` |

> **没有 `--font-*` 变量**：字体栈硬编码在 `body`(73–74)：`-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', system-ui, sans-serif`。
> **没有 spacing 变量组**（只有 `--gutter`），也没有 z-index 变量（直接写 20/40/60）。

**手机覆盖（338–343）**：`--shell-max: 100%`、`--gutter: 15px`、`--head-h: 56px`、`--r: 13px`。

### 共用类

| 类 | 行号 | 说明 |
|---|---|---|
| `.site` / `.site-main` | 92 / 93 | 站点骨架 |
| `.site-head` | 96 | 顶部导航条（手机隐藏） |
| `.brand` / `.brand-mark` | 115 / 120 | 品牌区 + 内联罗盘 SVG |
| `.site-nav a` / `a.router-link-active` | 129 / 135 | 导航项与激活态 |
| `.page-root` | 140 | **每个页面必须有这个单根节点** |
| `.shell` | 148 | 应用壳容器（`max-width: var(--shell-max)`） |
| `.shell-head` / `.shell-title` / `.shell-sub` / `.shell-head-right` | 157 / 165 / 169 / 170 | 页头 |
| `.back-link` / `.back-arrow` | 172 / 179 | 返回按钮 |
| `.shell-body` | 181 | 可滚动主体（自定义滚动条 182–188） |
| **`.btn`** | **193** | 基类：`hover`(203) `active`(204) `[disabled]`(205) |
| `.btn.ghost` | **207** | 品牌浅底 |
| `.btn.line` | **212** | 白底 + 边框 |
| `.btn.sm` / `.btn.lg` | 218 / 219 | 尺寸 |
| **`.card`** | **224** | surface + line 边框 + `--r` + `--sh-1` |
| **`.chip`** | **231** | 小标签基类 |
| `.chip.hot` / `.chip.must` / `.chip.primary` | 238 / 239 / 240 | 橙 / 金 / 品牌 |
| `.sec` / `.sec-title` | 242 / 243 | 分区标题（右侧渐隐线 249） |
| **`.empty`** / `.empty .big` | **251** / 252 | 空态（76px padding 居中 + 40px emoji） |
| **`.seg`** / `.seg button` / **`.seg button.on`** | **255** / 256 / 262 | 分段控件 |
| `.loading-mask` / `.spinner` / `.loading-steps` / `.loading-bar` / `.loading-note` | 270–311 | 规划中遮罩 |
| **`.tabbar`** | **316** | 底部标签栏（基础 `display:none`，手机才 flex） |

> `.panel` **不是**全局类——它是 `TripPlan.vue` 的 scoped 类（该文件 561 行）。`TripPick.vue` 用的是 `.pick-panel / .panel-head / .panel-set / .panel-cta`。

### 媒体查询

全文**只有一个** `@media (max-width: 640px)`（337–372），内含：
`.site-head { display: none }`(347) → `.shell-head` 转 sticky + 毛玻璃(350–357) → 返回按钮只剩箭头(361) → `.tabbar { display: flex }`(364)。

> **没有 `@media (min-width: ...)`** —— JS 侧的 641px 只存在于 `use-media.js`。

---

## 组件

### `App.vue`（21 行）

```html
<div class="site">
  <SiteHeader />
  <div class="site-main"><router-view /></div>
</div>
```

**只有一个 `<router-view>`，没有 `v-slot`，没有 `<transition>`**（12–16 行有注释解释原因）。本文件无任何脚本逻辑、无 props/emits/slots。

### `PhoneShell.vue`（50 行）— 页面外壳

| prop | 行号 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `title` | 26 | String | `''` | 标题 |
| `sub` | 27 | String | `''` | 副标题 |
| `back` | 28 | Boolean \| String | `false` | `true` → `router.back()`；字符串 → `router.push(该路径)` |

| slot | 行号 | 位置 |
|---|---|---|
| 默认 | 14 | `.shell-body`（`ref="bodyEl"` 的可滚动容器） |
| `right` | 11 | 页头右侧 |
| `footer` | 16 | `.shell-body` **之后**（TripPlan 用它放底部浮条） |

`emits: ['back']`(33)、`defineExpose({ scrollToTop })`(43) —— **两者全项目都无人使用**。
页头渲染条件(3)：`v-if="title || back || $slots.right"`。

宽屏与手机的 DOM **完全相同**，差异全在 `style.css`（见上文媒体查询）。

### `SiteHeader.vue`（26 行）

宽屏专属顶部导航。`<script setup>`(23–26) **只有注释，没有任何代码**。品牌区内联罗盘 SVG（5–12）。导航 2 项：`规划 → /trip`(16)、`我的行程 → /trip/me`(17)。
**只在 `App.vue:3` 渲染一次**，页面不再引用。

### `TripTabBar.vue`（20 行）

手机专属底部标签栏。`props: { active: String = 'home' }`(12–14)。

| key | icon | label | to |
|---|---|---|---|
| `home` | 🧭 | 规划 | `/trip` |
| `me` | 👤 | 我的 | `/trip/me` |

**由各页面自己渲染**（`TripHome.vue:56` 传 `active="home"`、`TripMe.vue:44` 传 `active="me"`）。`TripPick` 与 `TripPlan` **不渲染它**。

### `TripMap.vue`（402 行）— 半屏地图

**双模式**，数据源相同（`plan.attractions` 的 lat/lng + 时间轴的 `attraction_id`）：

1. 配了 `VITE_AMAP_JS_KEY` → 真·高德 JS API
2. 未配 Key 或加载失败（`amapFailed`）→ **GCJ-02 等距投影的 SVG 示意图**（零依赖、离线可用）

| prop | 行号 |
|---|---|
| `plan` / `open` / `day` | 104–108 |

| 关键实现 | 行号 | 说明 |
|---|---|---|
| `amapKey` / `amapSecurity` | 112–113 | 读 `import.meta.env.VITE_AMAP_JS_KEY` / `VITE_AMAP_SECURITY_CODE` |
| `useAmap` | 116 | `!!amapKey && !amapFailed` |
| `onUnmounted(() => destroyAmap())` | 122 | 组件卸载要销毁实例，否则泄漏 |
| `rawPoints` | 137 | 从时间轴抽带坐标的点位（含所属天、顺序、时间） |
| `points` | 166 | 等距投影：经度按中心纬度 cos 修正，保证形状不变形 |
| `polylines` | 195 | SVG 模式下每天的折线（**按时间轴顺序连接 = 只连去程**） |
| `watch(() => props.open, ...)` | 210 | 关闭时 `destroyAmap()`；打开时 `nextTick` → `loadAmap` → `new AMap.Map` → `drawAmap` |
| `destroyAmap` | 238 | `clearMap() + destroy()`，容错 try/catch |
| `drawAmap` | 248 | 折线先画压在 marker 下面；`showDir: true` 带方向箭头（**不画回酒店的返程线**）；最后 `setFitView` |
| `loadAmap` | 292 | JS API 2.0：**若 Key 绑的是安全密钥，必须在加载脚本前注入 `window._AMapSecurityConfig`**，否则地图不渲染 |

> ⚠️ 弹层是 `v-if` 控制的，关闭时 DOM 被销毁，所以地图实例必须跟着销毁——否则二次打开会往「已被移除的容器」挂载 → **白屏 + 报错**。`watch(props.open)` + `destroyAmap` 就是为这个加的。改动这个文件时务必回归「开关两次地图弹窗」。

### `TripHome.vue`（285 行）— ① 城市宫格

| 状态 | 行号 |
|---|---|
| `cities` / `loading` / `error` / `h` | 68–71 |
| `engineTip` computed | 73–79 |

`engineTip` 来自 `health()` → `GET /api/health`（`onMounted` 97–100，失败静默）：

| 条件 | 输出 |
|---|---|
| `h` 为空 | `null`（不渲染） |
| `!h.llm_ready` | `{ level:'warn', icon:'⚙️', text:'未配置 DeepSeek Key，当前由本地规则引擎生成方案' }` |
| 否则 | `{ level:'ok', icon:'✨', text:'AI 引擎已就绪 · {model}' }` |

结构：hero 区（badge + h1 + 说明 + engine-tip + 装饰性内联 SVG 路线图，`aria-hidden`）→ 城市宫格（`.city-emoji / .city-name / .city-tag / .city-count`）→ 空库提示 → `TripTabBar`。
`goPick(city)`(93)：`router.push({ path:'/trip/pick', query:{ city: city.name } })`。
宫格列数：宽屏 `repeat(auto-fill, minmax(238px, 1fr))`(178)；手机强制 2 列(276)。城市图标有独立的 **8 色 CSS 循环**（219–225，`nth-child(8n+2)…(8n+8)`）。

### `TripPick.vue`（535 行）— ② 景点勾选

| 状态 | 行号 | 说明 |
|---|---|---|
| `attractions` / `selectedIds` / `loading` / `error` | 173–176 | |
| `planning` / `showSet` / `stepText` | 177–179 | |
| `preview` | 180 | 紧凑度预估结果 |
| `pref`（reactive） | 194–203 | `days=2` / `pace='balanced'` / `transport='mixed'` / `startTime='09:00'` / `returnTime='19:30'` / `customEnds=false` / `firstDayStart='13:00'` / `lastDayReturn='16:00'` |
| `PACES` | 182–186 | 宽松 / 平衡 / 紧凑（带 `tip`） |
| `TRANSPORTS` | 188–192 | 🚗 自驾 / 🚕 打车+公共 / 🚇 公共 |
| `warn` computed | 219–224 | 只有两个分支：没勾景点 / `n < pref.days`（会提示「勾了 N 个景点却排 D 天」） |

**紧凑度预估**（277–300）：`refreshPreview()` 防抖 **350ms** → `previewPlan(buildPayload())`；失败静默置 `null`（不影响主流程）。`watch` 依赖 9 个值（勾选 / 天数 / 节奏 / 出行方式 / 出发 / 返回 / 首末天开关与两值）。

**提交**（`startPlan()` 302–324）：`savePref` → `createPlan` → `commitPlan` → `router.push('/trip/plan?planId=...')`。loading 期间用 `setInterval` 每 **3400ms** 轮换一条 `STEPS` 文案（205–212，6 条）。

**恢复上次勾选**（`load()` 262–268）：`loadPref(city)` 拿回 `pref` 与 `selectedIds`（并过滤掉不在当前城市的 id）。

### `TripPlan.vue`（694 行）— ③ 按天时间轴（核心页）

| 状态 | 行号 |
|---|---|
| `plan` / `loading` / `busy` / `mapOpen` / `regenOpen` / `stepText` | 247–252 |
| `open`（reactive，4 个折叠区开关） | 253 |
| `PACE_LABEL` | 255（**本地重复定义**，与 trip-theme 重复） |
| `rg`（reactive，重新生成弹窗状态） | 317–326 |

**关键 computed**

| 名字 | 行号 | 作用 |
|---|---|---|
| `backTo` | 265 | **返回景点池必须带 city**，否则 pick 页拿不到 city 会报「缺少 city 参数」。取 `plan.request.city`，没有则回 `/trip` |
| `sections` | 270 | 折叠区清单（有复核意见时把 `review` 插到最前） |
| `reviewIssues` | 282 | `plan.review.issues` |
| `summarySegments` | 289 | **总述按句分段 + 景点名加粗**：先转义再插 `<strong>`（所以 `v-html` 安全）；景点名按长度降序替换，避免短名先匹配把长名切断 |

**加载（`load()` 392–414）三级回落**：
`store.current`（内存，planId 匹配时） → `openHistory(planId)`（localStorage） → `getPlan(planId)`（后端）。
有 `error`/`warn` 级别复核意见时默认展开复核区（410）。

**重新生成弹窗**（模板 155–220）：可改天数 / 节奏 / 出行方式 / 出发返回 / 首末天，两个动作：

| 动作 | 函数 | 行号 | 行为 |
|---|---|---|---|
| 按倾向微调 | `doAdjust` | 358 | `adjustPlan(id, payload)` → 秒级、不花 token、沿用文案 |
| 全部重做 | `doRebuild` | 373 | `createPlan(payload)` → 走 LLM、文案与分天都会变 |

两者都 `router.replace` 成新的 `planId`（**生成的是新记录**）。

`busy` 用 `''` / `'adjust'` / `'rebuild'` 三态区分 loading 文案。**布尔属性必须写 `!!busy`**（`''` 被 Vue 判为真，会让按钮永久禁用——踩过）。

**时间轴渲染**：`grid-template-columns: 56px 30px 1fr`（时间 / 轨道 / 内容）；节点按 `t-{type}` 上色；`.tl-travel` 显示「{travel_mode}约 N 分钟 · 停留 X」；`moved_to_day` 的节点显示「已挪至 Day N（原 Day M）· 原因」。

### `TripMe.vue`（174 行）— ④ 历史

**数据来自 localStorage，不是 `GET /plans`**（`listPlans()` 零引用）。

| computed | 行号 | 内容 |
|---|---|---|
| `briefs` | 58 | `computed(() => historyBriefs())` |
| `cityCount` | 60 | 去重城市数 |
| `dayTotal` | 61 | 天数总和 |

每条展示：城市 / 「N 天」chip / 「N 景」chip / **来源 chip**（`source === 'fallback'` → `.chip.hot`「规则引擎」，否则 `.chip.must`「AI」）/ 总述（3 行截断）/ 创建时间 / 删除按钮。

`open(b)`(63)：先 `openHistory(b.plan_id)` 写 `store.current`，再 `router.push({ path:'/trip/plan', query:{ planId: String(...) } })`——**两件事都要做**。
`del`(68) 与 `clearAll`(72) 都有 `confirm` 确认。
顶部 `#right` slot 放「清空」按钮（仅在有数据时显示）。

---

## 页面 → 接口 → 后端 对照

| 页面 | 调用 | 后端 |
|---|---|---|
| TripHome | `getCities()` + `health()` | `service.list_cities` + `main.health` |
| TripPick | `getAttractions()` / `previewPlan()` / `createPlan()` | `list_attractions` / `preview_plan` / `generate_plan` |
| TripPlan | `getPlan()` / `adjustPlan()` / `createPlan()` | `get_plan` / `adjust_plan` / `generate_plan` |
| TripMe | —（纯 localStorage） | `list_plans` 已就绪但未接 |

---

## 迁移到微信小程序的映射（V1 已按此设计）

| Web | 小程序 |
|---|---|
| `views/trip/TripHome.vue` | `pages/index/index` |
| `views/trip/TripPick.vue` | `pages/pick/pick` |
| `views/trip/TripPlan.vue` | `pages/plan/plan` |
| `views/trip/TripMe.vue` | `pages/me/me` |
| `trip-api.js` | `utils/request.js`（`wx.request` 封装） |
| `trip-store.js` | `wx.setStorageSync` / `getStorageSync` |
| `TripMap.vue`（SVG 示意图） | `<map>` 组件（腾讯地图，GCJ-02 直接可用） |
| `style.css` 的 CSS 变量 | WXSS 不支持变量，需替换成字面量或预处理器产物 |
| `PhoneShell.vue` | 真机上要**去掉**手机壳 |

后端完全不用改——流水线那套与端无关。请求域名要加进小程序后台白名单。
