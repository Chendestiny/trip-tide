# TripTide 文档索引

> **给接手项目的 AI / 新人**：先读完这一页（约 8 分钟）建立全局认知，再按需跳转。
> 本页只讲「是什么、怎么跑、改哪里」，细节在下面的分册里。

---

## 30 秒速览

**TripTide 是一个国内自由行行程规划工具**：选城市 → 勾景点 → 设置天数/节奏/出行方式 → AI 出按天时间轴（含动线、饭点、住宿片区与取舍理由）。

| 维度 | 事实 |
|---|---|
| 前端 | Vue 3 + Vite + vue-router，**无 UI 框架**（自绘 4 个页面），`frontend/` |
| 后端 | Python 3.13 + FastAPI + SQLAlchemy 2.0，`backend/` |
| 数据库 | MySQL 8（`DATABASE_URL` 留空则自动退化 SQLite，零配置可跑） |
| 大模型 | DeepSeek（OpenAI 兼容端点），默认别名 `deepseek-flash` |
| 坐标源 | 高德开放平台 **Web 服务** Key（seed 阶段补 GCJ-02 坐标） |
| 地图渲染 | 高德 **JS API** Key（`frontend/.env`），缺 Key 自动降级 SVG 示意图 |
| 规模 | 8 城 × 20 景离线种子 = 160 条（有 Key 的 LLM 模式每城 26 条） |
| 响应式 | **桌面优先**，`≤640px` 才切手机形态，同一 DOM 靠媒体查询切换 |

**不是**什么：没有账号体系、没有支付、没有真实路径规划（通行时间用分档经验系数）、没有生产部署产物。

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

**为什么不让 LLM 自己编排**：第一版用 11 个工具做 function calling，能跑但**26 次工具调用、1.1 分钟**，而且模型会把分天改坏（实测把熊猫基地挪到 14:20——熊猫在午睡；把同一景点排进两天；把晚餐合并成 14:05 的「下午茶」）。提示词写两遍都没拦住。改成并行流水线后 **17.7 秒，快 3.7 倍**，且分天质量由硬编码保证。

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
| localStorage 历史与偏好 | `frontend/src/trip-store.js` | 🟢 低 |
| 路由表、全局错误兜底 | `frontend/src/main.js` | 🟠 中 |
| 地图（高德 / SVG 降级） | `frontend/src/components/trip/TripMap.vue` | 🟠 中 |

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
| **GCJ-02** | 火星坐标系。全库统一，混坐标系会整体偏移几百米且**不报错** | `Attraction.coord_source` 可追溯 |

---

## 三条不变量（改代码不能破坏）

```
① 时间自洽   prev.time + prev.stay_minutes + cur.travel_minutes == cur.time
② 饭点唯一   每天恰好 1 个午餐（time < 15:00）+ 1 个晚餐（time >= 15:00）
③ 景点唯一   同一个 attraction_id 在整份行程里只出现一次
```

由三处守着：`planner.rule_violations()`（时段硬约束）、`service.audit_plan()`（运行时复核）、`scripts/check_planner.py`（8 用例回归，改完必跑）。

---

## 最容易踩的 5 个坑

1. **别给 `<router-view>` 套 `<transition mode="out-in">`** —— 离场过渡不完成会导致新页面永不挂载。表现是**白屏，但硬刷新同一 URL 完全正常**，编译不报错、接口全 200。踩过一次，见 `ARCHITECTURE.md#踩过的坑`。
2. **别让 LLM 产出任何时间** —— 想加「模型参与决策」的能力就加提示词字段，不要放开时间。原因见上面的架构说明。
3. **`materialize_day()` 的 `push()` 是唯一时间推进点** —— 绕过它直接 append 节点，时间轴立刻不自洽。
4. **坐标一律 GCJ-02** —— 高德返回的 `location` 就是，可直接入库。
5. **样式桌面优先** —— 先写宽屏，再补 `@media (max-width: 640px)`。反过来会让宽屏变成「拉大的手机」。

---

## 文档导航

| 文档 | 讲什么 | 什么时候读 |
|---|---|---|
| **本页** `README.md` | 索引、速览、目录地图、关键概念 | 第一步 |
| [`CHANGELOG.md`](CHANGELOG.md) | **按日期记录影响架构的改动**（含每次踩坑的原因与解法） | 想知道「为什么现在是这样」 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 分层与设计取舍：为什么这么切、交通模型、紧凑度、踩过的坑 | 想改架构前 |
| [`BACKEND.md`](BACKEND.md) | 后端符号级地图：每个文件导出什么、**四张表**结构 | 改后端前 |
| [`FRONTEND.md`](FRONTEND.md) | 前端符号级地图：路由、组件、状态、设计系统变量表、响应式 | 改前端前 |
| [`API.md`](API.md) | **9 个** HTTP 接口的契约、错误码、紧凑度阈值推导 | 对接口时 |
| [`PLAN_SCHEMA.md`](PLAN_SCHEMA.md) | 时间轴数据结构 `PlanResult` 全字段说明 | 改数据结构和前端渲染前 |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | 环境搭建、常用命令、测试、排查表 | 准备跑起来时 |
| [`testing-prompt.md`](testing-prompt.md) | **交给其他模型写单元测试用的提示词**（自包含，可整段复制） | 需要补测试时 |
| `screenshots/` | 界面截图 | 了解成品长相 |

> **找代码请用函数名，别用行号** —— 行号每次改代码都会漂移。

根目录两份：[`../README.md`](../README.md)（面向人）、[`../AGENTS.md`](../AGENTS.md)（面向 AI agent 的操作手册与铁律）。
