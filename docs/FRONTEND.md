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
├── vite.config.js          port 5176；proxy /api → 127.0.0.1:8002；
│                           别名 `@api` 按构建模式切 trip-api / trip-api-local（离线单文件版）
├── .env                    VITE_AMAP_JS_KEY / VITE_AMAP_SECURITY_CODE
└── src/
    ├── main.js             路由表 + 4 层全局错误兜底（离线版走 hash 路由）
    ├── App.vue             站点骨架（SiteHeader + router-view）
    ├── style.css           设计系统：CSS 变量 + reset + 共用类 + 手机覆盖
    ├── trip-api.js         10 个接口封装 + 错误文案归一（联机版）
    ├── trip-api-local.js   同名导出的**离线实现**：数据内联 + 浏览器内排程，无后端
    ├── engine/             排程引擎：`backend/app/trip/planner.py` 的 JS 镜像
    │   ├── consts.js       常量表（与 planner.py 一一对应）
    │   ├── pyfmt.js        Python 的 round / 定点格式化语义（别用 Math.round）
    │   ├── geo.js          haversine / 交通方式与耗时
    │   ├── rules.js        时段规则 / 分级 / 必去判定 / 每日预算
    │   ├── planner.js      裁剪 / 聚类分天 / 逐天裁剪 / 住宿 / 时间轴物化
    │   ├── preview.js      紧凑度预估 + 倾向微调
    │   └── offline-data.json  构建期生成（勿手改，见 docs/OFFLINE.md）
    ├── trip-store.js       localStorage：历史方案（上限 30 条）
    ├── trip-theme.js       按天配色 / 节点图标 / 节奏标签
    ├── use-media.js        视口断点（matchMedia）
    ├── tests/              前端单元测试（node --test 零依赖，`npm test`，84 用例）
    │   ├── engine-basics.test.js    pyfmt / 地理交通 / 判定规则
    │   ├── engine-planner.test.js   聚类 / 裁剪 / 物化不变量 / plan_fallback
    │   ├── engine-preview.test.js   region 回归（莫高窟）+ 预估 + 微调冒烟
    │   ├── engine-invariants.test.js  引擎补漏 + 跨目的地真数据不变量
    │   └── api-boot-retry.test.js   接口层冷启动重试（测 trip-api.js，不在引擎里）
    ├── components/trip/
    │   ├── SiteHeader.vue  顶部导航（仅宽屏可见）
    │   ├── PhoneShell.vue  页面外壳：标题行 + 滚动主体 + 3 个 slot
    │   ├── TripTabBar.vue  底部标签栏（仅手机可见）
    │   ├── TripMap.vue     半屏地图（高德 JS API，缺失/失败降级 SVG）
    │   └── SpotSheet.vue   景点详情弹窗（热度/建议时长/子景点，纯静态数据）
    └── views/trip/
        ├── TripHome.vue    ① 首页：「城市 | 区域」两 tab + 目的地宫格
        ├── TripPick.vue    ② 景点勾选 + 设置面板 + 紧凑度预估
        ├── TripPlan.vue    ③ 按天时间轴（核心页）
        └── TripMe.vue      ④ 历史方案
```

> 所有 `.vue` 都**没有声明组件 `name`**（无 `defineOptions`），组件名由 SFC 编译器按文件名推断。

> 📌 **微信小程序端在 `miniprogram/`**，与 `frontend/` **各写各的、不共享代码**。
> 两边刻意重复的文件清单（改一边记得改另一边）见 [`MINIPROGRAM.md`](MINIPROGRAM.md) §2。

---

## 路由（`main.js:46–69`）

| 行号 | path | name | 组件 | meta.title |
|---|---|---|---|---|
| 49 | `/` | — | — | `redirect: '/trip'` |
| 50 | `/trip` | `trip-home` | TripHome | `AI 旅行搭子` |
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

| key | 内容 |
|---|---|
| `triptide.history.v1` | `Array<PlanResponse>`，最新在前，**上限 30 条**（`MAX_HISTORY`） |

> ⚠️ `triptide.pref.v1` **已移除**（见 CHANGELOG §8）。需求变更：进景点池不再恢复上次的勾选与设置，
> 每次都是干净状态；天数、节奏等在页面内仍然可用，只是不跨会话记住。
> **`triptide.history.v1` 这个键不能改**——改了用户已有历史全部丢失。

| 导出 | 行为 |
|---|---|
| `store`（`reactive`） | **只有两个字段**：`current`（当前方案快照）、`history`（初始化时 `readHistory()`） |
| `hasCurrent` | computed。**全项目零引用** |
| `commitPlan(plan)` | 设 `current`，按 `plan_id` 去重后头插并截断 30 条 |
| `openHistory(planId)` | 命中则设 `current`，**返回命中项或 `null`** |
| `removeHistory(planId)` | 删除并持久化；若删的是 `current` 则置 `null` |
| `clearHistory()` | 清空 history + current |
| `historyBriefs()` | **普通函数（非 computed）**，map 出轻量条目 |

`writeHistory` 有**配额溢出兜底**：失败后砍到 15 条重试一次，再失败静默放弃（不阻断主流程）。

`historyBriefs()` 单条形状：`{ plan_id, city, days, title, source, created_at, attraction_count, summary }`。
注意 `result` / `request` 都可能缺（老记录、降级记录），所以**每个字段都有兜底**。

> 勾选状态**不在 store 里**，是 `TripPick.vue` 的组件内 `ref`。

> 📌 小程序端有一份**独立实现** `miniprogram/utils/store.js`（`wx.storage` + 手写订阅，
> 小程序没有 vue）。业务规则是**刻意重复**的，改一边记得改另一边 —— 见 [`MINIPROGRAM.md`](MINIPROGRAM.md) §2。
> 两端的 storage 是独立命名空间，**同名 key 不会冲突也不会互通**。

---

## 请求层（`trip-api.js`）

`API_BASE = '/api/trip'`，dev 下由 Vite 代理到 `127.0.0.1:8002`。

| 超时常量 | 值 | 说明 |
|---|---|---|
| `TIMEOUT.default` | `15000` | 普通接口 |
| `TIMEOUT.preview` | `12000` | 纯硬编码、毫秒级，给 12s 已是极大冗余 |
| `TIMEOUT.plan` | `150000` | LLM 并行流水线实测 15~30s 属正常，别按十几秒设 |
| `TIMEOUT.adjust` | `15000` | 微调纯硬编码、秒级 |

核心 `request(path, { method, body, timeout })`：`AbortController` + `setTimeout`；仅带 body 时加 `Content-Type`；先 `r.text()` 再 try `JSON.parse`（解析失败静默置 `null`）。

| 导出 | 方法 + 路径 | 超时 |
|---|---|---|
| `getCities()` | `GET /cities` | 15s |
| `getAttractions(city)` | `GET /attractions?city=` | 15s |
| `getSpots(id)` | `GET /attractions/{id}/spots` | 15s |
| `previewPlan(payload)` | `POST /preview` | **12s** |
| `createPlan(payload)` | `POST /plan` | **150s** |
| `autoPlan(payload)` | `POST /auto-plan` | **150s** |
| `adjustPlan(planId, payload)` | `POST /plan/{id}/adjust` | 15s |
| `getPlan(id)` | `GET /plan/{id}` | 15s |
| `listPlans(limit=30)` | `GET /plans` | 15s |
| `health()` | `GET /api/health`（原生 fetch，不走 `request`） | 无 |

**错误文案**（全部 `throw new Error(...)`，`e.message` 精确如下）：

| 场景 | 文案 |
|---|---|
| `!r.ok` 且 `detail` 是字符串 | 原文透传 |
| `detail` 是数组 | `detail.map(d => d.msg).join('；')` |
| 无 detail | `请求失败（HTTP {status}）` |
| 超时且是 `TIMEOUT.plan` | `AI 规划超时了（150s），请减少景点数量或稍后重试` |
| 其它超时 | `请求超时，请检查后端是否已启动` |
| `TypeError`（网络层） | `连不上后端服务（/api → 127.0.0.1:8002（vite 代理）），请确认已启动` |

> `listPlans` **全项目零引用**——「我的」页读的是 localStorage，这个接口留给接入登录后切换。

> 📌 小程序端对应实现是 `miniprogram/utils/request.js`（`wx.request`，走 `config.js` 的绝对地址，
> 没有 vite 代理那一层）。契约刻意重复，改一边记得改另一边。

---

## 视觉常量（`trip-theme.js`）

**地图、时间轴、图例共用一套，避免两边各写一份跑偏。**

| 导出 | 内容 |
|---|---|
| `DAY_COLORS` | **7 色**数组 |
| `dayColor(day)` | `DAY_COLORS[(max(1,day) - 1) % 7]` |
| `nodeVisual(type, transport='mixed')` | 返回 `{ icon, label }` |
| `paceStyle(pace)` | 返回 `{ bg, fg }`（入参是**中文标签**「轻松/适中/紧凑」） |
| `TRANSPORT_LABEL` | `drive: 自驾` / `mixed: 打车+公共` / `transit: 公共交通` / `taxi: 打车+公共` |
| `PACE_LABEL` | `relaxed: 宽松` / `balanced: 平衡` / `packed: 紧凑` |

> 📌 小程序端有一份**独立副本** `miniprogram/utils/theme.js`（内容一致，刻意重复）。
> 改配色 / 图标时**两边一起改**。

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

> ⚠️ **`DAY_COLORS` 与 `PACE_LABEL` 都是零引用**（`dayColor()` 内部用了 `DAY_COLORS`，但没有页面直接 import 它）。
> `PACE_LABEL` 被**两处本地副本**替代：`TripPlan.vue:274`（`PACE_LABEL`）与
> `TripPick.vue:352`（`PACE_LABEL_MAP`），内容与 theme 里的一致。
> **改标签文案时注意别只改一处。**

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

### `:root` 变量全表（11–61 行，共 35 个）

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

### `PhoneShell.vue`（46 行）— 页面外壳

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

**由各页面自己渲染**（`TripHome.vue:83` 传 `active="home"`、`TripMe.vue:44` 传 `active="me"`）。`TripPick` 与 `TripPlan` **不渲染它**。

### `TripMap.vue`（401 行）— 半屏地图

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

### `TripHome.vue`（450 行）— ① 首页：「城市 | 区域」两 tab + 目的地宫格

两 tab 靠 `City.kind` 过滤（`city` / `region`）；region 卡片带「环线」角标，点击同样进 `/trip/pick`，引擎按 kind 走不同住宿策略。

> **当前 tab 同步在 URL 里**（`?tab=region`，103–106）：本组件与 `/trip/pick` 之间没有 keep-alive，
> 从 pick 返回会整个重挂载 —— tab 只存 `ref` 的话必回「城市」。初值从 `route.query.tab` 读，
> 变化时 `router.replace` 写回（city 时不带参数，保持 `/trip` 干净）。刷新 / 分享链接同样能还原。

| 状态 | 行号 |
|---|---|
| `cities` / `loading` / `error` / `h` | 96–99 |
| `tab`（初值读 query + watch 写回） | 103–106 |
| `engineTip` computed | 130 |

`engineTip` 来自 `health()` → `GET /api/health`（`onMounted` 169，失败静默）：

| 条件 | 输出 |
|---|---|
| `h` 为空 | `null`（不渲染） |
| `!h.llm_ready` | `{ level:'warn', icon:'⚙️', text:'未配置 DeepSeek Key，当前由本地规则引擎生成方案' }` |
| 否则 | `{ level:'ok', icon:'✨', text:'AI 引擎已就绪 · {model}' }` |

结构：hero 区（badge + h1 + 说明 + engine-tip + 装饰性内联 SVG 路线图，`aria-hidden`）→ 城市宫格（`.city-emoji / .city-name / .city-tag / .city-count`）→ 空库提示 → `TripTabBar`。
**hero 下方的 `.home-tab-note`(48) 只在搜索有命中时渲染**（显示「找到 N 个」）；不搜索时整行不渲染，
**原来那句固定的 tab 提示语（「选一个城市，勾景点就出发」）已去掉**。

hero 的高度是首屏面积的对手，**容器与内部间距都已收紧过**（2026-09-17）：

| 层 | 宽屏 | 手机 |
|---|---|---|
| `.hero` padding(174 / 408) | `26px 26px 22px` | `16px 16px 14px` |
| `.hero` 纵向 margin | `0 … 4px` | `0` |
| `.hero-badge`(209) | `padding: 3px 10px`、`radius 6px` | `padding: 2px 8px` |
| `.hero h1`(217) | `margin: 10px 0 7px` | `margin: 7px 0 5px` |
| `.hero p`(223) | `line-height: 1.6` | `line-height: 1.55` |
| `.engine-tip`(228) | `margin-top: 11px`、`padding: 7px 10px`、`gap 8px`、`radius 8px` | `margin-top: 8px`、`padding: 6px 9px` |

横向 margin 保持 `var(--gutter)`，以便与 tab / 搜索栏 / 宫格左右对齐。
实测 hero 高：宽屏 **266 → 212px**，手机 **212 → 177px**（宫格起始位置相应上移 60 / 35px）。
⚠️ 再往下压要注意 `.hero-art`：它 `bottom: 10px` + 高 `220px`，hero 低于约 230px 就开始裁掉 SVG 顶部（当前裁 18px，曲线本身没被切到）。
`goPick(city)`(165)：`router.push({ path:'/trip/pick', query:{ city: city.name } })`。
宫格列数：宽屏 `repeat(auto-fill, minmax(238px, 1fr))`(309)；手机强制 2 列(418，`grid-template-columns: 1fr 1fr`)。城市图标有独立的 **8 色 CSS 循环**（355–361，`nth-child(8n+2)…(8n+7)`）。

> ⚠️ **手机端宫格必须写 `grid-auto-rows: max-content`（424）——别删。**
> 手机端 `.city-grid` 是「定高滚动容器」（`flex:1 + min-height:0 + overflow-y:auto`，425–426），
> 此时隐式轨道（`grid-auto-rows: auto`）会退到**最小贡献**而不是内容高度：
> Blink 实测一张卡片只分到 **26px**（内容其实要 164px），卡片被自己的 `overflow:hidden` 裁成一条，
> 只剩 emoji 上半截——看起来「卡片全被折叠、看不到城市名/简介/景点数」。
> 宽屏没有这个问题（宫格不在定高容器里，`auto` 轨道取内容高度），所以**只在手机形态下翻车**。
> 已验证的等效写法：`grid-auto-rows: min-content` / 去掉 `.city-card` 的 `overflow:hidden` / 让宫格不再是滚动容器。
> ⚠️ `align-content: start`、`align-self: start`、`min-height: max-content` 都**不解决**（后两个只是让卡片溢出轨道、互相重叠）。
> 同源坑：`TripPick.vue` 的 `.att` 靠 `flex-shrink: 0` 躲过同一类问题（`.att` 是行向 flex，受影响更小）。
> 复现与消融脚本的做法见 `docs/CHANGELOG.md` 2026-09-17 §2。

### `TripPick.vue`（877 行）— ② 景点勾选

| 状态 | 行号 | 说明 |
|---|---|---|
| `attractions` | 282 | 景点池（`getAttractions()`） |
| `selectedIds` / `loading` / `error` | 299–301 | |
| `planning` / `showSet` / `stepText` | 302–304 | |
| `preview` | 305 | 紧凑度预估结果 |
| `mode` | 307 | 视图模式（默认 `'detail'`） |
| `spotSheet`（reactive） | 285 | 景点详情弹窗状态（`open` / `att` / `spots` / `loading`） |
| `PACES` | 309–313 | 宽松 / 平衡 / 紧凑（带 `tip`） |
| `TRANSPORTS` | 315–319 | 🚗 自驾 / 🚕 打车+公共 / 🚇 公共 |
| `pref`（reactive） | 321–330 | `days=2` / `pace='balanced'` / `transport='mixed'` / `startTime='09:00'` / `returnTime='19:30'` / `customEnds=false` / `firstDayStart='13:00'` / `lastDayReturn='16:00'` |
| `STEPS` | 332 | loading 期轮播文案（6 条） |
| `areaFilter` / `allAreas` / `visibleAttractions` | 350–354 | 区域筛选 chips（纯前端过滤，不调后端） |
| `warn` computed | 371 | 只有两个分支：没勾景点 / `n < pref.days`（会提示「勾了 N 个景点却排 D 天」） |

**紧凑度预估**（`refreshPreview()` 449）：防抖 **350ms** → `previewPlan(buildPayload())`；
失败静默置 `null`（不影响主流程）。`watch` 依赖 9 个值（勾选 / 天数 / 节奏 / 出行方式 / 出发 / 返回 / 首末天开关与两值）。

**提交**（`startPlan()` 498）：`createPlan` → `commitPlan` → `router.push('/trip/plan?planId=...')`。
loading 期间用 `setInterval` 每 **3400ms** 轮换一条 `STEPS` 文案。

> ⚠️ `savePref` / `loadPref` **已移除**（见 CHANGELOG §8）——进景点池不再恢复上次的勾选与设置，
> 每次都是干净状态。**现在没有「恢复上次勾选」这一步**；天数、节奏等在页面内仍然可用，只是不跨会话记住。

### `TripPlan.vue`（764 行）— ③ 按天时间轴（核心页）

| 状态 | 行号 |
|---|---|
| `plan` / `loading` / `busy` / `mapOpen` / `regenOpen` / `stepText` | 266–271 |
| `open`（reactive，4 个折叠区开关） | 272 |
| `PACE_LABEL` | 274（**本地重复定义**，与 trip-theme 重复） |
| `STEPS` | 276 |
| `rg`（reactive，重新生成弹窗状态） | 350 |

**关键 computed**

| 名字 | 行号 | 作用 |
|---|---|---|
| `backTo` | 284 | **返回景点池必须带 city**，否则 pick 页拿不到 city 会报「缺少 city 参数」。取 `plan.request.city`，没有则回 `/trip` |
| `sections` | 289 | 折叠区清单（有复核意见时把 `review` 插到最前） |
| `reviewIssues` | 301 | `plan.review.issues` |
| `summarySegments` | 308 | **总述按句分段 + 景点名加粗**：先转义再插 `<strong>`（所以 `v-html` 安全）；景点名按长度降序替换，避免短名先匹配把长名切断 |

**加载（`load()` 425）三级回落**：
`store.current`（内存，planId 匹配时） → `openHistory(planId)`（localStorage） → `getPlan(planId)`（后端）。
有 `error`/`warn` 级别复核意见时默认展开复核区。

**重新生成弹窗**（模板 172–234）：可改天数 / 节奏 / 出行方式 / 出发返回 / 首末天，两个动作：

| 动作 | 函数 | 行号 | 行为 |
|---|---|---|---|
| 按倾向微调 | `doAdjust` | 391 | `adjustPlan(id, payload)` → 秒级、不花 token、沿用文案 |
| 全部重做 | `doRebuild` | 406 | `createPlan(payload)` → 走 LLM、文案与分天都会变 |

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
| TripPick（弹窗） | `getSpots(id)` | `GET /attractions/{id}/spots`（SpotSheet，纯静态） |
| TripPick（筛选） | `district` 多选 chips | 纯前端过滤，`distance_km` 渲染周边/远郊标记 |
| TripPick | `getAttractions()` / `previewPlan()` / `createPlan()` | `list_attractions` / `preview_plan` / `generate_plan` |
| TripPlan | `getPlan()` / `adjustPlan()` / `createPlan()` | `get_plan` / `adjust_plan` / `generate_plan` |
| TripMe | —（纯 localStorage） | `list_plans` 已就绪但未接 |

---

## 迁移到微信小程序的映射（V1 已按此设计）

> 📌 小程序端**已落地在 `miniprogram/`**，与 `frontend/` 各写各的、不共享代码。
> 完整说明（本地开发三档、刻意重复清单、待验证假设、待办）见 [`MINIPROGRAM.md`](MINIPROGRAM.md)。

| Web | 小程序 | 状态 |
|---|---|---|
| `views/trip/TripHome.vue` | `pages/index/index` | 🟡 现为自检页，待搬正式宫格 |
| `views/trip/TripPick.vue` | `pages/pick/pick` | ⬜ 待做 |
| `views/trip/TripPlan.vue` | `pages/plan/plan` | ⬜ 待做 |
| `views/trip/TripMe.vue` | `pages/me/me` | ⬜ 待做 |
| `main.js` + `App.vue` | `app.js` + `app.json` | ✅ 已建 |
| `trip-api.js` | `utils/request.js`（`wx.request` 封装） | ✅ 已建 |
| `trip-store.js` | `utils/store.js`（`wx.storage` + 手写订阅） | ✅ 已建 |
| `trip-theme.js` | `utils/theme.js`（独立副本） | ✅ 已建 |
| `TripMap.vue`（高德 JS API，失败降级 SVG） | `<map>` 组件（腾讯地图） | ⬜ **必须重写** |
| `style.css` 的 35 个 CSS 变量 | WXSS 无变量机制，样式各写各的 | ⬜ |
| `PhoneShell.vue` | 真机上要**去掉**手机壳 | — |
| `SpotSheet.vue` | 景点详情弹窗 | ⬜ 待做 |

> **地图是唯一必须重写的组件**：`TripMap.vue` 依赖高德 **JS API** + `window._AMapSecurityConfig`，
> 小程序没有 DOM，高德 JS API 根本用不了。好在全库坐标已是 GCJ-02，腾讯 `<map>` 直接吃（铁律 4），
> 而且 marker / polyline **不需要 key**。

后端完全不用改——流水线那套与端无关。上架前把请求域名加进小程序后台的 **request 合法域名**。
