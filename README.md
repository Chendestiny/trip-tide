# AI 旅行搭子 — AI 行程规划

**中文** ｜ [English](README.en.md)

国内旅游自助规划工具：**选城市 → 勾景点 → AI 出按天方案**（地理顺路排序、时间轴、餐饮区域、住宿片区与取舍理由）。

V1 交付形态是 **响应式网页版**（Vue 3 + Vite，桌面优先，宽度 ≤640px 才切手机 App 形态）。
微信小程序端**已另起一个目录** `miniprogram/`，与网页版**各写各的、不共享代码**，
页面一一对应，详见 [`docs/MINIPROGRAM.md`](docs/MINIPROGRAM.md)。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + Vite（`vue-router`，**无 UI 框架**，4 个页面自绘） |
| 后端 | Python 3.11+ / FastAPI + SQLAlchemy 2.0 |
| 数据库 | MySQL 8（`DATABASE_URL` 留空时自动退化为 SQLite，零配置可跑） |
| 大模型 | DeepSeek（默认别名 `deepseek-flash`），OpenAI-compatible 端点 |
| 坐标源 | 高德开放平台 **Web 服务** Key（POI 文本搜索，返回 GCJ-02） |
| 地图渲染 | 高德 **JS API** Key（`frontend/.env`）；缺 Key 自动降级 SVG 示意图 |

---

## 界面

| 宽屏 · 首页：城市 / 区域 两个 tab | 宽屏 · 景点池：左设置面板 + 实时紧凑度预估 |
|---|---|
| ![首页](docs/screenshots/wide-home.png) | ![景点池](docs/screenshots/wide-pick.png) |

| 宽屏 · 行程结果：时间轴 + 地图 + 说明卡 | 手机 · 行程结果（同一 DOM，媒体查询切换） |
|---|---|
| ![结果页](docs/screenshots/wide-plan.png) | ![手机](docs/screenshots/mobile-plan.png) |

视觉是一套自建的设计系统（`style.css` 顶部的 35 个 CSS 变量）：偏暖的中性底色、多层叠加阴影、
8 色循环的城市图标、按天配色的时间轴（7 色循环）。没有引 UI 框架——这个界面只有 4 页，自绘比配 Element Plus 更轻也更可控。

---

## 页面流程

```
① /trip                首页：「城市 | 区域」两个 tab（kind 过滤）+ AI 引擎状态提示
      ↓ 点城市
② /trip/pick?city=成都  景点按 heat 降序，多选高亮
      右侧设置面板（手机是底部浮条）：
        · 行程天数 1~7        · 节奏倾向：宽松 / 平衡 / 紧凑
        · 出行方式：🚗 自驾 / 🚕 打车+公共 / 🚇 公共
        · 每天出发 → 目标返回时间
        · ☐ 首末天单独设置（第一天可能中午才到，最后一天要赶飞机）
        · 实时紧凑度预估：轻松 / 适中 / 紧凑 / 超载 + 会舍弃哪些景点
      ↓ 开始规划 → POST /plan → loading（通常 15~30 秒）
③ /trip/plan           按天时间轴 + 折叠区（AI 复核意见 / 舍弃景点 / 必去理由 / 酒店建议）+ 半屏路线地图
      ↻ 重新生成 → 弹窗改参数，可选「按倾向微调」（秒级、不花 token）或「全部重做」（走 LLM）
④ /trip/me             历史规划（localStorage，上限 30 份，点开可回看）
```

---

## 快速开始

### 1. 后端

```bash
cd backend
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt   # Windows
# source venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

cp .env.example .env      # 填 DATABASE_URL / DEEPSEEK_API_KEY / AMAP_KEY
```

`.env` 三个关键项：

| 变量 | 不填的后果 |
|---|---|
| `DATABASE_URL` | 自动退化为 `backend/data/triptide.db`（SQLite），功能完全一致 |
| `DEEPSEEK_API_KEY` | 规划接口走本地规则引擎兜底，仍能出完整方案（`source=fallback`） |
| `AMAP_KEY` | 初始化用离线种子里手校的坐标，不校准 |

### 2. 灌数据（首次必跑）

```bash
cd backend
venv\Scripts\python -m app.trip.seed              # 全部 68 个目的地（很慢，见下）
venv\Scripts\python -m app.trip.seed --city 成都   # 只灌一个城市（推荐）
venv\Scripts\python -m app.trip.seed --list       # 看库里现状
```

有 Key 时走「LLM 出名单 → 高德**地理编码**补真实坐标（POI 搜索只作兜底）」；无 Key 时用离线种子
（`data/seed_attractions.json`，**68 个目的地都与库逐字段一致**）。种子里的条目都带
`coord_source=amap` 与子景点坐标，所以 `--source offline` **不打任何高德请求**。

> 📌 **种子必须与库保持同步**：曾经漂移过一次（老城的 `heat` 停留在 T 级迁移之前的 0~100 旧尺度，
> 离线灌数据一个「必去」都判不出来）。改完库里的景点数据后记得一起对齐。

单城约 22 景 + 60~80 条子景点 ≈ 100 次高德请求，受 `AMAP_SLEEP=0.8s` 串行节流，**实测 2~3 分钟**。
**给老城补数据必须逐个 `--city`**：不带参数会遍历所有城市、每城调一次 DeepSeek，
把手工校对过的老城名单覆盖掉。

### 3. 前端

```bash
cd frontend
npm install
npm run dev        # → http://localhost:5176
```

### 4. 一条命令起前后端（推荐）

```bash
npm install        # 根目录，装 concurrently
npm run dev        # 前端 5176 + 后端 8002
npm run seed       # 等价于 backend 里的初始化
```

浏览器打开 http://localhost:5176 即可，`/api` 由 Vite 自动代理到 8002。

### 5. 回归检查

```bash
# 后端：规则引擎 15 个用例，逐节点校验时间自洽、饭点、主题一致性、景点不凭空消失
cd backend && venv/Scripts/python ../scripts/check_planner.py

# 首页排序体检：排序分是否过期、顺序是否与口径一致（只读）
cd backend && venv/Scripts/python ../scripts/check_rank.py

# 前端：无头浏览器跑宽屏 + 手机两种视口，走完 4 个页面 + 真实规划（需先起前后端）
python scripts/smoke_ui.py
python scripts/smoke_ui.py --skip-plan
```

前端冒烟测试是必要的——白屏这类问题编译不报错、接口也全 200，只有真跑浏览器才发现。

---

## 设计要点

**地理优先分天 → LLM 审阅 → LLM 并行写内容 → 硬编码物化时间轴 → LLM 终检。**

```
① 硬编码分天      大景点/远郊独占一天 → 其余按直线距离聚成「片区簇」→ 簇分配到天
                  片区簇是原子、不可拆 —— 紧邻景点必然同日
①.5 LLM 审阅      把量化通行时间矩阵给模型提建议，过 5 道硬校验才采纳；不过就沿用①
② LLM 并行写内容   每天一路，ThreadPoolExecutor 并发
③ 硬编码物化       materialize_day() —— 全项目唯一产出时间的地方
④ LLM 终检         读最终时间轴，提意见 + 润色总述
```

**为什么不让 LLM 自己编排。** 第一版是让模型用 10 个工具（function calling）自己编排，
能跑，但**26 次工具调用、1.1 分钟**，而且它会把分天改坏：实测把熊猫基地挪到 14:20（熊猫午后在睡觉）、
把宽窄巷子排进两天。提示词里写了两遍都没拦住——因为「算不算得下」本来就不该由它判断。
改成并行流水线后 **17.7 秒，快 3.7 倍**；2026-09-16 关掉模型思考后进一步降到 **6~7 秒**。

**为什么后来又把 LLM 请回来审阅分天。** 因为**输入不一样了**：新方案给它的是
**预计算的通行时间矩阵**，它不用猜距离、只需读表；再加 5 道硬校验（紧邻必须同天 /
独占独占一天 / 不超预算…），**只可能改好、不可能改坏**。

**时间必须自洽。** `materialize_day()` 内部只有一个 `cur_time` 推进点，所以
`到达 = 上一站离开 + 路程` 恒成立。这条不变量由运行时复核和回归脚本双重守着。

**时间预算是硬的。** 出发 / 返回时间是用户定的硬约束，**不乘任何倾向系数**。
赶返程那天（最后一天 + 显式设了返回时间）会严格卡点：排不下晚餐就不排，
必要时压缩最后一个景点的停留。

**交通按距离分档，纯本地算。** 步行 / 地铁公交 / 打车 / 自驾 / 顺风车 / 城际大巴 / 城际铁路，
按「城区内 / 近郊互通 / 远郊」分档，且分自驾、打车+公共、公共交通三种偏好。
**不接路径规划 API**：一次规划十几条腿，打接口既慢又费配额，±10 分钟误差不影响方案可用性。

**餐饮只给区域，不给店名。** 店铺随时会换，写了反而误导。给的是「区域 + 本地特色小吃」。

**住宿一次定全程。** 按「全程景点的几何中心」选常住片区，只有某天离它超 45km（如成都的都江堰日）才建议就近过夜。

**一键 AI。** 不想逐个勾景点时，只给**城市 + 天数 + 节奏**即可（`POST /auto-plan`）——
服务端按「必去优先 → 热度其次」自动挑景点，再走**同一条**生成链路，
所以产物结构与勾选生成完全一致。

**紧凑度看「日均游览量」。** 把勾选景点的总游览时长摊到每天，与节奏目标（轻松 240 / 平衡 360 /
紧凑 450 分钟）比 —— 比「时间占用率」直观，而且**对天数敏感**（加一天就松一点）。

**降级不静默。** 没配 Key 或流水线失败时自动切规则引擎，并在 `summary` 末尾如实注明原因，
前端也会打上「规则引擎生成」标签。

---

## 响应式设计

**桌面优先**：默认渲染宽屏网页版，只有宽度 ≤640px 才切成手机 App 形态。同一个 DOM 靠媒体查询切换，不做两套模板。

| | 宽屏（≥641px） | 手机（≤640px） |
|---|---|---|
| 容器 | 1120px 居中 | 铺满全屏 |
| 导航 | 顶部导航栏（`SiteHeader`） | 底部标签栏（`TripTabBar`） |
| 首页城市宫格 | `auto-fill minmax(238px, 1fr)`，约 4 列 | 2 列 |
| 景点池 | 左列表（多列卡片）+ 右侧粘性设置面板 | 单列列表 + 固定底部浮条 |
| 结果页 | 时间轴 + 右侧粘性「方案说明」 | 单列，说明落到时间轴下方 |
| 地图按钮 | 页头右侧 | 底部浮条 |
| 重新生成弹窗 | 居中卡片 | 底部上滑 sheet |
| 历史 | 卡片栅格 | 单列 |

**为什么桌面优先**：主场景是「在电脑上规划、在手机上看」，且宽屏信息密度要求更高。
移动优先的写法很容易让宽屏变成「拉大的手机」，反过来则只是少写几条覆盖。

绝大多数差异纯 CSS 就够，只有两处需要 JS 知道形态（`use-media.js` 的 `useIsWide()`）：
设置面板是常驻侧栏还是折叠浮条、地图按钮放页头还是浮条。

---

## 文档

| 文档 | 讲什么 |
|---|---|
| [**`docs/README.md`**](docs/README.md) | **文档索引 + 5 分钟速览 + 目录地图（先读这个）** |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | **按日期记录影响架构的改动**（含每次踩坑的原因与解法） |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 分层与设计取舍：为什么这么切、交通模型、紧凑度、踩过的坑 |
| [`docs/BACKEND.md`](docs/BACKEND.md) | 后端符号级地图：每个文件导出什么、四张表结构 |
| [`docs/FRONTEND.md`](docs/FRONTEND.md) | 前端符号级地图：路由、组件、状态、设计系统变量表、响应式 |
| [`docs/OFFLINE.md`](docs/OFFLINE.md) | **离线单文件版**：两份 planner 的镜像关系、构建开关（`@api`）、一致性验证 |
| [`docs/MINIPROGRAM.md`](docs/MINIPROGRAM.md) | **微信小程序端**：为什么另起目录、与 Web 的重复清单、本地开发三档、页面映射 |
| [`docs/API.md`](docs/API.md) | 9 个 HTTP 接口的契约、错误码、紧凑度阈值推导 |
| [`docs/PLAN_SCHEMA.md`](docs/PLAN_SCHEMA.md) | 时间轴数据结构 `PlanResult` 全字段说明 |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | 环境搭建、常用命令、测试、排查表、待办 |
| [`docs/testing-prompt.md`](docs/testing-prompt.md) | 交给其他模型写单元测试用的提示词（自包含；Python 侧基线 `backend/tests/test_planner.py` 122 用例，JS 侧 `frontend/tests/` 39 用例） |
| [`AGENTS.md`](AGENTS.md) | 给 AI agent 的操作手册与 25 条铁律 |
