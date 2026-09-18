# AI 旅行搭子 文档索引

> **给接手项目的 AI / 新人**：先读完这一页（约 8 分钟）建立全局认知，再按需跳转。
> 本页只讲「是什么、怎么跑、改哪里」，细节在下面的分册里。

---

## 30 秒速览

**AI 旅行搭子是一个国内自由行行程规划工具**：选城市 → 勾景点 → 设置天数/节奏/出行方式 → AI 出按天时间轴（含动线、饭点、住宿片区与取舍理由）。

| 维度 | 事实 |
|---|---|
| 前端 | Vue 3 + Vite + vue-router，**无 UI 框架**（自绘 4 个页面），`frontend/` |
| 后端 | Python 3.13 + FastAPI + SQLAlchemy 2.0，`backend/` |
| 数据库 | MySQL 8（`DATABASE_URL` 留空则自动退化 SQLite，零配置可跑） |
| 大模型 | DeepSeek（OpenAI 兼容端点），默认别名 `deepseek-flash` |
| 坐标源 | 高德开放平台 **Web 服务** Key：seed 主路径调用 `/v3/geocode/geo`（15 万/月），失败才回退 `/v3/place/text`（5 千/月） |
| 地图渲染 | 高德 **JS API** Key（`frontend/.env`），缺 Key 自动降级 SVG 示意图 |
| 规划通行时间 | **不调用高德路径规划**；`planner.py` 用 GCJ-02 坐标 + haversine + 交通方式分档本地估算 |
| 定稿校准 | **尚未接入**；未来用户确认方案后，再调用高德驾车/步行/骑行/公交路径规划并缓存结果 |
| 目的地 | **两种 kind**：`city`（城市+周边，47 个）/ `region`（区域游环线，21 个）——首页「城市 | 区域」两个 tab。⚠️ **判定标准是「住宿基准」，不是行政区划**；边界规则是「离市中心 ≤80km 且单程 ≤90 分钟 → `city`，否则 `region`」 |
| 规模 | **68 个目的地 / 899 景 / 1724 条子景点**（47 城 + 21 区域，平均每个 13 景）。离线种子 `seed_attractions.json` **与库逐字段一致**，离线灌数据不打任何高德请求；全国统一热度 T 级 0~1000 |
| 子景点 | `trip_spot` 表（内部点位 + 攻略）。共 **1724 条、覆盖 61 个目的地**；少数老城（北京/上海/西安/重庆/广州/南京）仍为空，详情弹窗对空表显示空态 |
| 响应式 | **桌面优先**，`≤640px` 才切手机形态，同一 DOM 靠媒体查询切换 |

**不是**什么：没有账号体系、没有支付、没有**已接入的真实路径规划**（规划阶段用本地分档经验系数）、没有生产部署产物。

**高德调用边界**：高德 Web 服务只在 seed 补坐标时使用（`/v3/geocode/geo` 主路径，`/v3/place/text` 低频兜底）；前端地图按需加载高德 JS API。
`POST /api/trip/plan` 生成方案时不打高德路径规划接口，因此不会因为规划请求消耗驾车/步行/公交路径规划额度。

---

## 当前状态（2026-09-18 · 新会话从这里接）

**最近一轮（2026-09-17 傍晚～晚间）**：离线单文件版（参赛形态）+ region 排程误判修复 + 引擎单元测试。

- **离线单文件版落地**（详见 [`OFFLINE.md`](OFFLINE.md)）：`frontend/src/engine/` 是 `planner.py` 的逐函数 JS 镜像，
  排程在浏览器里算、数据内联（68 目的地 / 899 景点 / 1724 子景点）；`npm run build:offline` 出 Vue 正式版单文件，
  `scripts/build_mock_html.py` 出**无需 npm 的 mock 单文件**（`dist/trip-buddy-mock.html`，含宽屏双栏 + 手机两套布局）。
  两份 planner 的同步靠三重回归守着：`npm test`（**39 用例**）+ `local/offline-diff.py`（12 用例逐节点对照）+ `check_planner.py`。
- **修掉 region（环线）排程误判（用户实测：河西走廊+莫高窟报超载）**：
  路程参照点从「目的地中心」改为「最近过夜基地」（`_cost` / `_is_standalone`）；
  `preview_plan` 的裁剪传当天住宿片区做 origin，与生成同口径；午餐启发式改「**拆分优先于先吃**」。
  详见 `CHANGELOG.md` 2026-09-17（傍晚）§7/§8。

- **修掉两个用户报的 BUG（2026-09-17 下午）**：
  ① `GET /cities` 原来是 **70 条 SQL / 3.4 秒**（`len(c.attractions)` 的 68 连击 × 远端 MySQL 38.6ms RTT），
  现改为**1 条 SQL + 进程内缓存**（`cities_cache_ttl`），实测 **60~160ms、缓存命中 0ms**（提速约 20~55 倍），
  顺序与景点数与旧实现逐条一致；
  ② 手机端首页宫格卡片被压成 **26px**（只剩 emoji）—— `.city-grid` 是定高滚动容器时
  `grid-auto-rows: auto` 会退到最小贡献，必须显式写 `max-content`。两处的复现脚本与消融数据见 `CHANGELOG.md`。
- **首页排序算法重写 + 排序分预存 + 补齐景点数据（2026-09-17 下午）**：旧口径「前 10 热度之和」因 **30 个目的地恰好 8 景**
  而退化成「景点数量 × 人均热度」（泰安有泰山却排 67/68）。现改为 **前 8 个景点的指数加权和
  （λ=0.9，**第 1 名 ×1.5、第 2/3 名 ×1.2**）**，结果预存进 `trip_city.rank_score`，`/cities` 按 `rank_score` 排序（仍是 1 条 SQL）。
  并新增 `seed --topup` 增量补景点（**只走地理编码**，不碰 POI 搜索），补了 **74 条**（35 个目的地），
  **不足 8 景的目的地归零**。ρ(名次, 景点数量) **+0.858 → +0.538**；
  洛阳 21→8、北疆 53→15、拉萨 9→5。工具：`scripts/rank_cities.py`（重算落库）、`scripts/check_rank.py`（只读体检）。

- **目的地从 9 个扩到 18 个**：新增深圳 / 青岛 / 厦门 / 三亚 / 桂林 / 长沙 / 武汉 + 泰安 / 黄山，
  全部 `kind=city`。这批 + 后来补的 30 城 + 20 区域，现在库里共 **899 景 / 1724 条子景点**。景点名单由 seed 走 DeepSeek 现生成后
  **回写进了离线种子**（带 `coord_source=amap`，下次 seed 不再重复打高德接口）。
- **老 9 城的种子订正**：原来停留在 T 级迁移**之前**（`heat` 是 0~100 旧尺度），
  配上 `MUST_HEAT=500` 会让离线灌数据一个「必去」都判不出来。已按「种子 = 库里现状」对齐，
  并补进 14 条当年手校却从没入库的景点（含**西安华山**）。
- **修掉 `deepseek-flash` 的空返回 bug**：它默认思考，而思考内容**也计入 `max_tokens`**；
  预算被吃光时返回的是**空 content**，报错却是「模型返回内容为空」，完全指错方向。
  原来传的 `enable_thinking: False` 是 DashScope 风格、**DeepSeek 不认**，已改成
  `thinking: {"type": "disabled"}`。这一处同时救活了阶段 ①.5 / ② / ④ —— 详见 `CHANGELOG.md`。
- 子景点从「只有成都有」变成覆盖 61 个目的地（1724 条）；**并已接进规划链路** ——
  `planner.spot_advice()` 按 `order_index` 硬编码拼 advice（73% 的景点走这条路径，不花 token），
  提示词只给没有子景点的景点写。见 `CHANGELOG.md` 的 2026-09-17。

**还没做的**（优先级自上而下，动手前先和用户对齐）：

1. 子景点 `guide` 接进 `materialize_day` 的 advice（按 `order_index` 硬编码拼串，省 token）
2. 高德定稿校准（未做）：用户确认最终行程后，再用高德驾车/步行/骑行/公交路径规划替换分档估算；**生成规划阶段不调用**（用户已定过时机：「最后确定行程再用高德」）
3. `estimate_days_needed` 补转场成本；`check_planner.py` 补 `drive` / 首末天时间窗 / region 用例
4. 新疆拆北疆/伊犁/南疆三个 region 目的地（用户已确认拆法，数据未做）

**详细变更史**：`CHANGELOG.md` 的 `2026-09-16` 两节是最近的大动作（目的地扩容 / 首页排序 / UI 优化）。

---

## 一句话架构

> **硬编码分天 → LLM 并行写内容 → 硬编码物化时间轴 → LLM 终检。**

```
① 硬编码分天        瞬时、确定、不花钱、可回归测试
   大景点/远郊独占一天 → 其余按直线距离聚成「片区簇」→ 簇分配到天
   地理顺路 + 容量裁剪 + 时段硬知识都在这里生效
   ✅ 片区簇是原子、不可拆 —— 紧邻景点必然同日（锦里↔武侯祠 0.24km 就是这条保住的）

①.5 LLM 审阅分天   把**量化通行时间矩阵**给模型提调整建议
                   输出过 5 道硬校验才采纳；不过就沿用 ① 的结果（纯增益，无新增失败面）

② LLM 并行写内容    每天一路，ThreadPoolExecutor 并发（MAX_WORKERS=4）
   每路只产出：当天主题 + 各景点建议 + 餐饮区域与本地特色

③ 硬编码物化        瞬时、确定
   materialize_day  ← 全项目唯一产出时间的地方
   含弹性游玩时长填空、跨午饭拆分、目标返回时间的硬约束

④ LLM 终检          读最终时间轴，提意见 + 润色总述
```

**为什么不让 LLM 自己编排**：第一版用 10 个工具做 function calling，能跑但**26 次工具调用、1.1 分钟**，而且模型会把分天改坏（实测把熊猫基地挪到 14:20——熊猫在午睡；把同一景点排进两天；把晚餐合并成 14:05 的「下午茶」）。提示词写两遍都没拦住。改成并行流水线后 **17.7 秒，快 3.7 倍**（2026-09-16 关掉模型思考后降到 **6~7 秒**），且分天质量由硬编码保证。

细节 → [`ARCHITECTURE.md`](ARCHITECTURE.md)

---

## 5 分钟读懂请求链路

```
用户操作                      HTTP                        后端
─────────────────────────────────────────────────────────────────────
① 打开首页
   TripHome.vue ──────────▶ GET /api/trip/cities ──────▶ service.list_cities
                                                          └─ trip_city 表
   TripHome.vue ──────────▶ GET /api/health ────────────▶ 提示「AI 已就绪 / 走规则引擎」

② 勾景点（实时预估）
   TripPick.vue ──────────▶ GET /api/trip/attractions ──▶ service.list_attractions
   TripPick.vue ──────────▶ POST /api/trip/preview ─────▶ service.preview_plan
              ↑ 纯硬编码，毫秒级；前端防抖 350ms，每次改勾选/天数/节奏都刷新紧凑度提示

③ 开始规划（核心链路）
   TripPick.vue ──────────▶ POST /api/trip/plan ────────▶ service.generate_plan
                                                          ├─ planner.plan_fallback()   硬编码基线（永远先算）
                                                          ├─ llm.run_pipeline()        ① 分天 ② 并行写内容 ③ 物化
                                                          ├─ llm.review_plan()         ④ 终检
                                                          └─ 落库 trip_plan（入参 + 出参 + 终检 + 轨迹 JSON 快照）

④ 重新生成
   TripPlan.vue ──────────▶ POST /plan/{id}/adjust ─────▶ service.adjust_plan   复用原分天，纯硬编码，秒级
   TripPlan.vue ──────────▶ POST /api/trip/plan ────────▶ service.generate_plan  全部重做，走 LLM

⑤ 历史回看
   TripMe.vue ──── localStorage（V1）───────────────────  GET /plan/{id} 只在刷新兜底时才用
```

---

## 目录地图：改哪里的代码

> 完整符号级地图见 [`BACKEND.md`](BACKEND.md) / [`FRONTEND.md`](FRONTEND.md)。这张表只回答「我要改 X，动哪个文件」。

| 我想改…… | 动这里 | 风险 |
|---|---|---|
| 分天逻辑、容量裁剪、时段规则、饭点插入 | `backend/app/trip/planner.py` | 🔴 高 |
| 时间轴字段/结构（会同步影响前端） | `backend/app/trip/schemas.py` | 🔴 高 |
| LLM 提示词、并行流水线、终检 | `backend/app/trip/llm.py` | 🟠 中（提示词=生成质量的全部） |
| 接口编排、落库、倾向微调 | `backend/app/trip/service.py` | 🟠 中 |
| HTTP 路由、参数校验、错误码 | `backend/app/trip/routers.py` | 🟢 低 |
| 数据表结构 | `backend/app/trip/models.py` | 🟠 中（改完要重灌） |
| 配置项（**新增只加这里**） | `backend/app/core/config.py` | 🟢 低 |
| LLM 底层调用/模型注册/多 provider | `backend/app/core/_llm/` | 🔴 高（所有 LLM 调用的地基） |
| 灌数据、高德补坐标 | `backend/app/trip/seed.py` + `amap.py` | 🟠 中（会打外部 API） |
| 页面布局、响应式 | `frontend/src/views/trip/*.vue` + `style.css` | 🟠 中 |
| 设计系统（配色/圆角/阴影） | `frontend/src/style.css` 顶部 `:root` | 🟠 中 |
| 前端请求封装 | `frontend/src/trip-api.js` | 🟢 低 |
| localStorage 历史（上限 30 条） | `frontend/src/trip-store.js` | 🟢 低 |
| 路由表、全局错误兜底 | `frontend/src/main.js` | 🟠 中 |
| 地图（高德 / SVG 降级） | `frontend/src/components/trip/TripMap.vue` | 🟠 中 |
| 小程序端任何东西 | `miniprogram/` —— 与 `frontend/` **各写各的**，见 [`MINIPROGRAM.md`](MINIPROGRAM.md) | 🟢 低 |

---

## 关键概念

| 概念 | 含义 | 定义位置 |
|---|---|---|
| **基线 / baseline** | 硬编码规则引擎产出的完整方案。**每次请求都先算它**——有 Key 时它是参考实现，没 Key 时它就是最终答案 | `planner.plan_fallback()` |
| **物化 / materialize** | 把「景点顺序 + 大纲文案」变成带具体时间的节点序列 | `planner.materialize_day()` |
| **紧凑度 / tightness** | 选景点阶段的实时预估：`轻松 / 适中 / 紧凑 / 超载`。判定靠**日均游览量 vs 节奏目标**，不是时间占用率 | `service.preview_plan()` |
| **节奏倾向 / pace** | `relaxed / balanced / packed` 三选。决定「每天目标游玩时长」（240 / 360 / 450 分钟）与弹性填充强度。**不影响时间预算** —— 出发/返回是硬约束 | `planner.PACE_TARGET_MINUTES`、`PACE_FLEX` |
| **出行方式 / transport** | `drive / mixed / transit` 三选（历史值 `taxi` ≡ `mixed`），按距离自动挑具体交通工具 | `planner.leg()` |
| **微调 / tweak** | 复用原分天、只重排时间轴，秒级、不花 token，**生成一条新记录** | `service.adjust_plan()` |
| **一键 AI / auto-plan** | 只给城市 + 天数 + 节奏，服务端自动挑景点，再走**同一条**生成链路 | `service.generate_auto_plan()` |
| **降级 / fallback** | 无 Key 或流水线失败时切规则引擎，**不静默**：`source=fallback` + summary 注明原因 + 前端打标签 | `service.generate_plan()` |
| **独占型景点** | 游览 ≥4h 或单程 ≥45min，各自占一整天（否则远郊会把市区线挤崩） | `planner._is_standalone()` |
| **片区簇 / cluster** | 按**直线距离**（≤1200m）把相邻景点聚成的一组，**分天时不可拆** | `planner.cluster_attractions()` |
| **弹性填充 / flex** | 景点排得少时把余量还给景点本身：`clamp(可用×r, 基础, 基础×m)`，r/m 按节奏取；大景点与远郊不受限 | `planner._flex_scale()` |
| **严格返回 / strict_return** | 只有「最后一天 **且** 用户显式设了返回时间」才硬卡点（意味着赶高铁/飞机）；中间几天允许小幅超时并给提示 | `planner.materialize_day()` |
| **需要天数 / days_needed** | 「按片区排最舒服要几天」= 独占型个数 + 簇数。**仅作提示，不参与判定**（天数不够时簇会合并） | `planner.estimate_days_needed()` |
| **目的地 / kind** | `city`（城市+周边）/ `region`（区域游环线，如贵州）。同一张表同一引擎，只是住宿策略与首页 tab 不同 | `City.kind` |
| **转场日 / transit day** | region 目的地里「昨晚住 A、今晚住 B」的一天：从 A 退房出发，转场车程真实计入时间轴与预算。**出发地 ≠ 收尾点** | `planner.materialize_day(depart_from=)` |
| **必去 / must** | `must_visit` 字段 **或** 全国 T 级热度 ≥500（`MUST_HEAT`）。容量裁剪时两级丢弃：先丢普通，普通丢完才轮到必去 | `planner._is_must()` |
| **热度 T 级 / heat** | **全国统一 0~1000**（非城市内相对值）：T0 世界级 600+（故宫 990）/ T1 350~599 / T2 200~349 / T3 80~199 / T4 <80 | `seed` 提示词 |
| **周边 / 远郊标记** | `distance_km`（离目的地中心直线）：≥30 前端标「🚗 周边」、≥45 标「🚌 远郊·可就近住」——45 与 `OVERNIGHT_KM` 对齐，标记和引擎行为一致 | `service.list_attractions()` |
| **GCJ-02** | 火星坐标系。全库统一，混坐标系会整体偏移几百米且**不报错** | `Attraction.coord_source` 可追溯 |

---

## 三条不变量（改代码不能破坏）

```
① 时间自洽   prev.time + prev.stay_minutes + cur.travel_minutes == cur.time
② 饭点唯一   每天恰好 1 个午餐（time < 15:00）+ 1 个晚餐（time >= 15:00）
③ 景点唯一   同一个 attraction_id 在整份行程里只出现一次
```

由三处守着：`planner.rule_violations()`（时段硬约束）、`service.audit_plan()`（运行时复核）、`scripts/check_planner.py`（15 用例回归，改完必跑）。

---

## 最容易踩的 5 个坑

1. **别给 `<router-view>` 套 `<transition mode="out-in">`** —— 离场过渡不完成会导致新页面永不挂载。表现是**白屏，但硬刷新同一 URL 完全正常**，编译不报错、接口全 200。踩过一次，见 `ARCHITECTURE.md#踩过的坑`。
2. **别让 LLM 产出任何时间** —— 想加「模型参与决策」的能力就加提示词字段，不要放开时间。原因见上面的架构说明。
3. **`materialize_day()` 的 `push()` 是唯一时间推进点** —— 绕过它直接 append 节点，时间轴立刻不自洽。
4. **坐标一律 GCJ-02** —— 高德返回的 `location` 就是，可直接入库。
5. **样式桌面优先** —— 先写宽屏，再补 `@media (max-width: 640px)`。反过来会让宽屏变成「拉大的手机」。
6. **region 目的地的 trim/materialize 必须传当天过夜点** —— `trim_day(origin=, depart=)` / `materialize_day(depart_from=)`。缺省回落到 `city.center`，对贵州这种 300km 跨度的目的地会严重低估路程、把可排的转场日误判成装不下。
7. **城市宫格数据来自 `GET /cities`，两 tab 靠 `kind` 过滤** —— 新增目的地只需 seed JSON 里 `kind: "region"`，前端零改动。

---

## 文档导航

| 文档 | 讲什么 | 什么时候读 |
|---|---|---|
| **本页** `README.md` | 索引、速览、目录地图、关键概念 | 第一步 |
| [`CHANGELOG.md`](CHANGELOG.md) | **按日期记录影响架构的改动**（含每次踩坑的原因与解法） | 想知道「为什么现在是这样」 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 分层与设计取舍：为什么这么切、交通模型、紧凑度、踩过的坑 | 想改架构前 |
| [`BACKEND.md`](BACKEND.md) | 后端符号级地图：每个文件导出什么、**四张表**结构 | 改后端前 |
| [`FRONTEND.md`](FRONTEND.md) | 前端符号级地图：路由、组件、状态、设计系统变量表、响应式 | 改前端前 |
| [`OFFLINE.md`](OFFLINE.md) | **离线单文件版**：为什么有两份 planner、两份接口实现怎么切、一致性怎么验 | 做单文件 HTML / 改排程算法时 |
| [`MINIPROGRAM.md`](MINIPROGRAM.md) | **微信小程序端**：为什么另起目录、与 Web 的重复清单、本地开发三档、页面映射 | 做小程序时 |
| [`API.md`](API.md) | **9 个** HTTP 接口的契约、错误码、紧凑度阈值推导 | 对接口时 |
| [`PLAN_SCHEMA.md`](PLAN_SCHEMA.md) | 时间轴数据结构 `PlanResult` 全字段说明 | 改数据结构和前端渲染前 |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | 环境搭建、常用命令、测试、排查表 | 准备跑起来时 |
| [`testing-prompt.md`](testing-prompt.md) | **交给其他模型写单元测试用的提示词**（自包含，可整段复制） | 需要补测试时 |
| `screenshots/` | 界面截图 | 了解成品长相 |

> **找代码请用函数名，别用行号** —— 行号每次改代码都会漂移。

根目录两份：[`../README.md`](../README.md)（面向人）、[`../AGENTS.md`](../AGENTS.md)（面向 AI agent 的操作手册与铁律）。
