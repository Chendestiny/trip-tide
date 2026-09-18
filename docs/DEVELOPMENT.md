# 开发与调试

> 环境搭建、常用命令、回归测试、排查表、待办清单。
> 架构背景看 [`ARCHITECTURE.md`](ARCHITECTURE.md)，代码地图看 [`BACKEND.md`](BACKEND.md) / [`FRONTEND.md`](FRONTEND.md)。

---

## 环境要求

| 项 | 版本 | 说明 |
|---|---|---|
| Python | 3.11+（实测跑在 3.13.12） | 后端 |
| Node.js | ≥18（`package.json` 的 `engines` 要求） | 前端 |
| MySQL | 8（可省） | 不配则自动退化 SQLite，**功能完全一致** |
| 浏览器 | 系统自带 Edge 或 Chrome | 只有跑 `smoke_ui.py` 需要 |
| 操作系统 | 命令示例为 Windows（`venv\Scripts\python`），macOS/Linux 换 `venv/bin/python` | |

---

## 首次搭建

### 1. 后端

```bash
cd backend
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
cp .env.example .env      # 填 DATABASE_URL / DEEPSEEK_API_KEY / AMAP_KEY
```

### 2. 灌数据（首次必跑）

```bash
cd backend
venv\Scripts\python -m app.trip.seed              # 全部 18 个目的地（很慢，见下）
venv\Scripts\python -m app.trip.seed --city 成都   # 只灌一个城市（推荐）
venv\Scripts\python -m app.trip.seed --list       # 看库里现状
```

有 Key 时走「LLM 出名单 → 高德**地理编码**补坐标（POI 搜索只作兜底）」；无 Key 时用离线种子
（`data/seed_attractions.json`，**18 个目的地都与库逐字段一致**）。

种子里的条目都带 `coord_source=amap` 与子景点坐标，所以 `--source offline`
**不打任何高德请求**（实测北京 26 景 / 5.2 秒 / 0 次校准）。

> 📌 **种子必须与库保持同步。** 曾经漂移过一次：老 7 城的种子停留在 T 级迁移之前
> （`heat` 是 0~100 旧尺度，故宫种子 99 / 库里 990），配上 `MUST_HEAT=500`
> **离线灌数据一个「必去」都判不出来**。改完库里的景点数据后，记得把种子一起对齐
> （做法与教训见 [`CHANGELOG.md`](CHANGELOG.md) 2026-09-16 第 7 节）。

单城约 22 景 + 60~80 条子景点 ≈ 100 次高德请求，受 `AMAP_SLEEP=0.8s` 串行节流
加上高德自身约 0.8s 的响应延迟，**实测 2~3 分钟**。当前坐标调用顺序是：

1. `GET /v3/geocode/geo`：地理编码主路径，基础 LBS 配额 **15 万/月**；
2. 地理编码查不到时，才 `GET /v3/place/text`：POI 关键字兜底，配额 **5 千/月**；
3. 两者都失败才跳过该条坐标；如果高德明确返回额度耗尽，会抛 `AmapQuotaExceeded` 并熔断对应接口，**不再静默丢整批数据**。

> ⚠️ **给老城补数据必须逐个 `--city`**：`main()` 不带参数时会遍历种子里**所有**城市，
> 每城都调一次 DeepSeek 出名单 —— 那会把手工校对过的老城数据覆盖掉。
>
> ⚠️ **别把两个城市并行跑**：`AMAP_SLEEP=0.8s` 是为了避开高德 QPS 限流，
> 并行会增加 `10004` / `10021` 等 QPS 错误；QPS 限流和月/日额度耗尽是两类问题，代码分别处理。
>
> **规划阶段不走上述任何接口**：`POST /api/trip/plan` 直接使用数据库里的 GCJ-02 坐标，
> `planner.py` 本地计算 haversine 距离与分档通行时间；高德路径规划只留给未来「用户确认行程后的定稿校准」。

| 参数 | 作用 |
|---|---|
| `--city 成都` | 只处理一个城市（支持拼音） |
| `--reset` | 先删掉该城已有景点再灌 |
| `--no-amap` | 不调高德，用种子里手校的坐标 |
| `--only-cities` | 只更新城市信息与候选住宿片区，不动景点 |
| `--source offline` | 强制用离线种子，不走 LLM |
| `--list` | 只打印现状 |

### 3. 前端

```bash
cd frontend
npm install
npm run dev        # → http://localhost:5176
```

`frontend/.env` 里可选配 `VITE_AMAP_JS_KEY`（高德 **JS API** Key）。不填则地图用 `TripMap.vue` 的 SVG 示意图，其余功能不受影响。

### 4. 一条命令起前后端（推荐）

```bash
npm install        # 根目录，装 concurrently
npm run dev        # 前端 5176 + 后端 8002
npm run seed       # 等价于 backend 里的初始化
```

浏览器打开 http://localhost:5176，`/api` 由 Vite 自动代理到 8002。

---

## 配置项

全部在 `backend/app/core/config.py`，值写在 `backend/.env`。变量名用通用命名，与其它项目并库时可直接复用。

| 变量 | 不填/留空的后果 |
|---|---|
| `DATABASE_URL` | 退化为 `backend/data/triptide.db`（SQLite） |
| `DEEPSEEK_API_KEY`（或 `MODEL_API_KEY`） | 规划走本地规则引擎兜底，仍能出完整方案、`source=fallback` |
| `MODEL_BASE_URL` / `MODEL_NAME` | 默认 DeepSeek 官方 + `deepseek-flash` |
| `AMAP_KEY` | seed 阶段不校准坐标，用种子里手校的值 |
| `LLM_TIMEOUT` | ✅ **有效**，用于 httpx / OpenAI 客户端超时 |
| `PRIVATE_LLM_API_KEY` / `_BASE_URL` / `_MODEL` | 私有化部署用；配了之后 `deployment="private"` 才生效 |
| `CORS_ORIGINS` | 默认只放行 `localhost:5176` / `127.0.0.1:5176` |

> ⚠️ **六个配置项是死的**：`LLM_MAX_RETRY`、`LLM_TEMPERATURE`、`LLM_MAX_TOKENS`、`DEFAULT_START_TIME`、`DEFAULT_RETURN_TIME`、`MAX_DAYS` 在代码里**零引用**。实际值硬编码在各调用点（`llm.py` 的 0.6/2000、0.3/1200、0.8/**8192**），时间与天数默认值写死在 `schemas.py`。改 `.env` 里这几项**不会生效**——要么改代码，要么先把配置接通。

前端地图 Key 走 `frontend/.env` 的 `VITE_AMAP_JS_KEY`（高德 **JS API** 类型，与后端的 Web 服务 Key **不通用**）。若控制台给 Key 绑了「安全密钥」，还要填 `VITE_AMAP_SECURITY_CODE`（`TripMap.vue:295` 会在加载脚本前注入 `window._AMapSecurityConfig`，漏了地图不渲染）。

---

## 常用命令

| 任务 | 命令 |
|---|---|
| 起前后端 | 根目录 `npm run dev`（前端 5176 + 后端 8002） |
| 只起后端 | `cd backend && venv/Scripts/python run.py`（支持 `--host` / `--port` / `--no-reload`） |
| 起小程序端 | 微信开发者工具导入 **`miniprogram/`** 目录（不是仓库根），详见 [`MINIPROGRAM.md`](MINIPROGRAM.md) |
| 灌数据 | `cd backend && venv/Scripts/python -m app.trip.seed`（**不带参数会遍历所有目的地**，别用来补老城） |
| 只灌新目的地 | `... -m app.trip.seed --only-empty --per 8` —— **只处理库里还没有景点的目的地**，不会碰老城 |
| 看库里现状 | `venv/Scripts/python -m app.trip.seed --list` |
| 后端规则引擎回归 | `cd backend && venv/Scripts/python ../scripts/check_planner.py` |
| **前端单元测试** | `cd frontend && npm test` —— **84 用例**（node --test，零依赖），测 `src/engine/` 排程引擎镜像 + `src/trip-api.js` 接口层；region 预估回归单独跑 `node ../local/check-preview-region.mjs`。改了 `planner.py` 或 `engine/*.js` **两边都要跑**，详见 [`OFFLINE.md`](OFFLINE.md) |
| 跨目的地边界检查 | `cd backend && venv/Scripts/python ../scripts/check_boundaries.py`（`-v` 看完整清单）—— **加了目的地之后必跑** |
| 首页排序体检 | `cd backend && venv/Scripts/python ../scripts/check_rank.py`（`--drill` 补口径对照）—— **只读**，查「首页为什么是这个顺序」+ 排序分是否过期 |
| 重算首页排序分 | `cd backend && venv/Scripts/python ../scripts/rank_cities.py`（`--dry-run` 先看）—— **改过景点热度后必跑**；seed 会自动跑 |
| 增量补景点 | `cd backend && venv/Scripts/python -m app.trip.seed --topup --per 10` —— 景点数不足 10 的补到 10，**已有的绝不动**；坐标只走地理编码（不打 POI 搜索）；完自动回写离线种子 + 重算排序分 |
| 前端冒烟测试 | `python scripts/smoke_ui.py`（需先起前后端） |
| 健康检查 | `curl -s -x "" localhost:8002/api/health` |
| 模型别名总表 | `curl -s -x "" localhost:8002/api/health/models` |
| 交互式接口文档 | http://localhost:8002/docs |

> Windows 下 `curl` 会走系统代理，访问 localhost **必须加 `-x ""`** 绕过，否则报「积极拒绝」。

---

## 测试

### 纯函数单元测试：`backend/tests/test_planner.py`

**不连数据库、不调 LLM、不起服务**，毫秒级跑完，**122 个用例**。覆盖：

| 分组 | 覆盖 |
|---|---|
| 基础 | 景点分级 / 用餐时长 / 时间预算 / 时段规则 / 地理与矩阵 / 聚类 / 独占判定 |
| 分天 | 一键挑景点 / `assign_days` / `_validate_days` 的 5 道硬校验 |
| 提示词 | 景点清单与通行矩阵的渲染 |
| **2026-09-17 第一波** | `spot_advice` 拼串（排序/截断/空攻略/缺属性）、advice 取值优先级（子景点 > LLM > intro）、`trim_day` 的超载例外、机动日两餐、`chat_json` 的空返回重试、seed 的地理上下文提示词与坐标校验 |
| **2026-09-17 第二波（amap 配额重构）** | `is_quota_error` / `is_qps_error` 错误码分类、双熔断独立（geo/poi 互不影响）、`search_address` 走地理编码主路径（POI 级优先 / count=0 兜底）、`search_poi` 内部「先 geo 后 poi」兜底链、geo 熔断后仍能回退 POI、两边都熔断时不再发请求 |

其中好几条是回归用的 —— 比如「时间预算不随 pace 变化」「紧邻景点拆到两天要报错」
「机动日必须有午餐」「POI 熔断不能误停地理编码」「geo 用尽后 search_poi 还能回退 POI」。
＊＊每条新测试都对应一个实际踩过的坑＊＊，坑的说明写在 docstring 里。

> ⚠️ amap 测试用 `patch.object(settings, "amap_key", "")` 模拟无 Key 场景 ——
> `settings.has_amap` 是 pydantic property（只读），**只能改底层 `amap_key` 字段**，
> 直接 patch `has_amap` 会报 `AttributeError: property has no setter`。

```bash
cd backend && venv/Scripts/python tests/test_planner.py
# 或
cd backend && venv/Scripts/python -m unittest tests.test_planner -v
```

改 `planner.py` / `llm.py` 的纯逻辑后**先跑它** —— 比下面的回归脚本快得多，
也不依赖数据库。

> 想扩充测试可以看 [`testing-prompt.md`](testing-prompt.md)：那份提示词是自包含的
> （含项目背景、被测函数语义、必须覆盖的场景、约束），可以直接交给另一个模型。

### 后端回归：`scripts/check_planner.py`

**不启服务**，直接调 `planner.plan_fallback()` 跑 15 个用例，逐节点校验 6 项。

```bash
cd backend && venv/Scripts/python ../scripts/check_planner.py
```

校验项：① 时间自洽（`prev.time + prev.stay + cur.travel == cur.time`）② 午餐恰好 1 个且落在 11:00~16:00 ③ 晚餐恰好 1 个且落在 17:30~21:30 ④ 出发时间等于用户设定、最后一个节点是 hotel ⑤ 主题里提到的景点必须真的在当天时间轴 ⑥ 每个勾选景点要么排入要么进 `dropped`（不能凭空消失）。

15 个用例（`CASES`，44 行起，完整清单直接看脚本）：

| # | 城市 | 景点数 | 天数 | transport | 压什么 |
|---|---|---|---|---|---|
| 1 | 成都 | 前 8 | 3 | `taxi` | 市区线 + 两处远郊，压力最大 |
| 2 | 成都 | 前 8 | 3 | `transit` | 公交地铁更慢 |
| 3 | 成都 | 前 5 | 1 | `taxi` | 一天塞 5 个 |
| 4 | 成都 | 前 3 | 2 | `taxi` | 景点比天数少 |
| 5 | 成都 | 前 14 | 7 | `taxi` | 长行程 |
| 6 | 成都 | 前 20 | 4 | `taxi` | 全选 |
| 7 | 北京 | 前 12 | 4 | `transit` | 含八达岭/十三陵远郊 |
| 8 | 北京 | 前 6 | 2 | `taxi` | — |

> ⚠️ **这个脚本需要连数据库**（要读景点数据），所以严格说不是纯离线。~~没有覆盖 `drive` / `transit`~~ —— 2026-09-17 已补：现在 15 个用例，覆盖 `mixed`（历史值 `taxi`）/ `transit` / **`drive`** 与首末天时间窗、region 链式转场（贵州/川西/南疆）。

### 前端：`scripts/smoke_ui.py`

**存在的意义**：白屏这类问题**编译不报错、接口也全 200**，只有真跑浏览器才发现。

```bash
python scripts/smoke_ui.py              # 需先起前后端
python scripts/smoke_ui.py --skip-plan  # 跳过耗时约 20s 的规划环节
```

机制：

- `playwright` 无头浏览器，浏览器用**系统自带 Edge/Chrome**（`channel`），不需要 `playwright install` 下载
- **两个视口都测**：宽屏 1280×900（多列栅格 + 右侧粘性面板 + 顶部导航）、手机 390×844（单列 + 底部浮条 + 标签栏）
- **白屏判定**：`#app` 的 `innerHTML` 长度 < `MIN_CONTENT = 500`（35 行）
- 监听 `pageerror` 与 `console.error`，任一出现即记为问题
- `BASE = http://localhost:5176`，默认城市成都

> 本机环境提示：`agent-browser` 类工具在 Windows 上不可用，`smoke_ui.py` 借系统 Edge（`channel="msedge"`）是本机唯一可行的浏览器自动化路径。

### 离线引擎：`frontend/tests/`（node --test）

**测的是 `src/engine/`（planner.py 的 JS 镜像）**，纯函数、零依赖、不需要起服务：

```bash
cd frontend && npm test                 # 84 用例（basics / planner / preview / invariants / api-boot-retry）
node ../local/check-preview-region.mjs  # region 预估回归（河西走廊+莫高窟 / 云南）
```

覆盖：pyfmt 的 Python 舍入语义、leg 各档交通选择、grade/MUST_HEAT 边界、
region 按「最近过夜基地」判独占与路程（**莫高窟误判超载的回归**，2026-09-17）、
聚类同簇、裁剪保护必去、materialize 时间自洽与饭点窗口、plan_fallback 无凭空消失、
预估与生成口径一致。改了 `planner.py` 或 `engine/*.js` **两边都要跑**，
并与 `local/offline-diff.py`（两侧逐节点对照）一起构成三重回归，详见 [`OFFLINE.md`](OFFLINE.md)。

---

## 排查表

| 现象 | 先看这里 |
|---|---|
| 首页城市宫格空 | 没灌数据 → `python -m app.trip.seed` |
| 方案带「规则引擎生成」标签 | 正常降级。看后端日志确认原因（无 Key / 流水线异常） |
| 规划超过 150s 超时 | 勾太多景点了。减少勾选，或调大 `LLM_TIMEOUT` |
| 规划慢（>40s） | 换回 `deepseek-flash`（默认）；`deepseek-reasoner` 会慢好几倍 |
| 地图点位整体偏移 | 坐标混了坐标系（必须全是 GCJ-02），查 `Attraction.coord_source` |
| 地图白屏 / 二次打开报错 | 高德实例没跟着 `v-if` 一起销毁；或 Key 缺安全密钥配置 |
| 地图一直显示「示意地图」 | `frontend/.env` 没填 `VITE_AMAP_JS_KEY`，或域名白名单没配 |
| 餐饮都是「片区·本地特色」 | **这是设计如此**（不写店名，店会换），不是降级 |
| 熊猫基地又被排到下午 | 查 `planner.time_rule()` 的关键词表（`_MORNING_KEYS`）是否被改动 |
| 点城市后白屏、**硬刷新就好** | `<router-view>` 被套了 `<transition>`，见 [`ARCHITECTURE.md`](ARCHITECTURE.md#踩过的坑) |
| 改完一堆文件后整站白屏 | 前端 HMR 模块图坏了：删 `frontend/node_modules/.vite`，重启 dev server，浏览器 `Ctrl/Cmd+Shift+R` |
| 页面显示「页面渲染出错」面板 | 这是 `main.js` 的错误兜底在工作，面板里有堆栈，比白屏好排查 |
| 从行程页点返回报「缺少 city 参数」 | `TripPlan.vue` 的 `backTo` computed 没带上 `plan.request.city` |
| 按钮永久禁用 | `:disabled="busy"` 而 `busy` 是空字符串 `''`——Vue 把 `''` 判为真。**布尔属性一律写 `!!busy`** |
| 宽屏下界面像「拉大的手机」 | 只写了手机样式，缺宽屏样式（桌面优先，见铁律） |
| MySQL 连不上 | `DATABASE_URL` 账号要有建库权限，或先手工建库 |
| `openai` 未安装 | 不致命，`core/_llm/_native.py` 会自动降级到 httpx 实现 |
| 后端日志中文乱码（`���ݱ��Ѿ�`） | `npm run dev` 经 cmd 转发输出，Python 默认按 **GBK** 写 stdout，而 concurrently 按 **UTF-8** 解码。脚本已加 `-X utf8` 修正；自己手敲 `python -m uvicorn` 时记得补上（`run.py` 直跑不受影响——终端与 Python 都是 GBK，能对上） |
| 刚启动时前端报 `ECONNREFUSED 127.0.0.1:8002` | **正常竞态，不是故障**——vite 比 uvicorn 快约 2s（后端要先连库、跑建表/轻量迁移）。页面刷新一下即可 |
| LLM 报「模型返回内容为空」 | 十有八九是**思考吃光了 `max_tokens`**：`deepseek-flash` 默认思考，而**思考内容也计入预算**，于是 `content` 为空、`finish_reason=length`，报错却完全指错方向。先看日志里有没有 `chat_json 第 N 次返回空内容（... reasoning X 字）`；再查 `_native.py` 的 `_build_body` —— 参数必须是 `thinking: {"type": "disabled"}`，**`enable_thinking: False` 是 DashScope 风格，DeepSeek 不认**（详见 `CHANGELOG.md` 2026-09-16 第 3 节） |
| 落库前复核稳定报「1 处问题」 | 检查是不是把「跨午饭拆分」当成了重复景点——同一天内两个同名节点是**有意的**。已修（`service.audit_plan` 只报跨天重复） |
| `seed` 出的名单缺该城核心景区 | 看 `seed.py` 的 `LIST_USER` 里「近郊景点」那条限制。踩过：桂林的名单被市区小公园占满、阳朔一个没进。已加「核心近郊景区必须包含」的说明 |

---

## 日志信号

后端日志里这几行最能说明问题（流水线版）：

```
plan_days days=4 kept=10 dropped=2          # ① 分天完成
gen_day day=1 ok / gen_day day=3 ✗           # ② 各天并行生成结果（✗ = 该天回退规则文案）
materialize days=4 elapsed=6.5s             # ③ 物化耗时（这是整条链路的总耗时）
落库前复核发现 K 处问题：...                  # audit_plan 抓到的问题（正常应为 0）
LLM 流水线失败，降级到规则引擎：...            # 整体降级
```

`trace` 字段（`PlanResponse.trace`）就是上面第一段的原始数组，排查时直接看接口返回，不用翻日志：

| trace 内容 | 来源 |
|---|---|
| `plan_days days=N kept=X dropped=Y` | `llm.py:315` |
| `gen_day day=N ok` / `gen_day day=N ✗` | `llm.py:359 / 362` |
| `materialize days=N elapsed=X.Xs` | `llm.py:420` |
| `adjust mode=tweak pace=P days=D restored=N` | `service.py:555` |

---

## 待办与建议

按性价比排序，**都不影响当前运行**，但会误导接手的人。

> **2026-09-15 大改之后新增的遗留项**（子景点 `guide` 接入规划、高德定稿校准、
> `check_planner.py` 覆盖不足等）记在 [`CHANGELOG.md`](CHANGELOG.md) 的「已知遗留」一节。
> 本页列的是更早的那批，其中已修的不再重复。

### 高优先：会直接误导的

1. **清掉或标注死代码**。`tools.py` 整套工具链（10 个工具 + `PlanSession` + `dispatch`）、`amap.py` 的 `search_around` / `search_restaurants`、以及 `planner.route_minutes()` 都**没有调用方**。`llm.py:35` 声称「用 `LLM_MODE=tools` 打开」——该开关从未实现。
   → 建议：要么补上 `LLM_MODE` 开关让它真能用，要么在文件头明确标注「预留能力，当前未启用」。**保留 `audit_day()`，它在用。**
   ⚠️ `route_minutes()` 尤其容易误用：它的模型比 `leg()` 粗得多 —— 同一个 105km，它按地铁 22km/h 算成 **295 分**，而 `leg()` 走城际铁路是 **90 分**（复核新景点时我自己就踩了）。主流程一律走 `leg()` / `travel_from()`。
2. **修注释与实际不符的地方**（共 10 处，清单见 [`BACKEND.md`](BACKEND.md#代码与注释不一致的地方写文档改代码时以代码为准)）。其中 `trip/__init__.py:6`「4 个端点」、`README`/`AGENTS` 里的「11 个工具」影响最大。
3. **决定那 6 个死配置的去留**：接通，或者删掉，或者注释说明「暂不生效」。

### 中优先：影响正确性/一致性

4. ~~`preview` 的判定指标缺陷~~ —— **2026-09-15 已修**：
   - 原容量口径 `capacity = 单天预算(min) × 天数`，在首末天时间窗不同时（第一天中午才到 /
     最后一天赶飞机）会低估 40% 以上 → 已改为「逐天预算求和」。
   - 原紧凑度看 `load`（时间占用率，对天数不敏感）→ 已改为「勾选总游览量摊到每天 vs 节奏目标」，
     详见 `API.md` 的「紧凑度怎么算的」。顺带修掉「约 0 个会被舍弃（）」空括号、
     days=7 时仍建议「加到 7 天」两处文案毛病。
   - **仍未处理**：必去景点在容量裁剪时仍可能被丢（`_value` 的 +30 加成抵不过远郊景点的高耗时）。
     成都「前 10 景 × 1 天」实测把必去的大熊猫基地丢了 —— 产品上是否合理仍需定夺。
5. **前端重复定义**：`TripPlan.vue:255` 本地定义的 `PACE_LABEL` 与 `trip-theme.js:45` 的重复。`trip-theme` 里的 `DAY_COLORS` 与 `PACE_LABEL` 零引用。→ 统一到 `trip-theme.js`。
6. ~~**`check_planner.py` 补用例**~~ —— **2026-09-17 已补**：现在 15 个用例，覆盖 `mixed`（历史值 `taxi`）/ `transit` / **`drive`**、首末天时间窗、以及 **region 链式转场**（贵州/川西/南疆）。顺带修了脚本里硬编码的 `city_id==1/2`（改成按名字查）与两处误报。
7. **`trip_plan.source` 字段注释**只写了 `llm/fallback`，实际还有 `tweak`。

### 低优先：能力扩展（V1 明确的取舍）

8. 通行时间接高德**路径规划** API（现在用分档经验系数，精度 ±10 分钟），需按天缓存。
9. 景点加 `best_time` 字段，把「时段硬知识」从 3 类关键词表升级为数据驱动（现在只有熊猫/夜景/博物馆三类）。
10. 历史记录接登录，从前端 `GET /plans` 取（接口已就绪）；现在只存 localStorage，换设备就没了。
11. 并发能力：现在是单进程 + 一天一路（`MAX_WORKERS=4`）。要支撑多人需要改异步队列 + 轮询/SSE。
12. 景点上限 40 个（路由层硬拦）。要放开需分批规划再合并。
13. `service.py` 里 `ApiError` schema 定义未使用，`main.py` 的异常处理器直接返回裸 dict——可统一。

---

## 前端构建产物

`frontend/dist/` 是 Vite 构建产物，**不在版本控制里**（见 `.gitignore`）。本项目 V1 不做生产部署：
后端没有静态文件托管，生产由 nginx 分担（`main.py` 顶部注释）。
