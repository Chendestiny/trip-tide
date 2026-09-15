# AGENTS.md — TripTide 操作手册（给 AI agent 看）

本项目是「选城市 → 勾景点 → AI 出按天行程」的规划工具。

**先记住一句话架构**：硬编码分天 → LLM 并行写内容 → 硬编码物化时间轴 → LLM 终检。
改动前先确认你要改的是哪一层——改错层的代价很大（见 §2 铁律 1、2）。

**先读什么**：
- 建立全局认知 → [`docs/README.md`](docs/README.md)（5 分钟速览 + 目录地图 + 关键概念）
- 改后端 → [`docs/BACKEND.md`](docs/BACKEND.md)（符号级地图，含行号）
- 改前端 → [`docs/FRONTEND.md`](docs/FRONTEND.md)
- 改数据结构 → [`docs/PLAN_SCHEMA.md`](docs/PLAN_SCHEMA.md)
- 对接口 → [`docs/API.md`](docs/API.md)

> ⚠️ **本仓库有多处注释与代码不符**（共 8 处，清单在 `docs/BACKEND.md` 文末）。
> **一律以代码为准**，尤其是「11 个工具」「`LLM_MODE=tools` 开关」「routers 4 个端点」这三条，全是错的。

---

## 0. 一条命令自检

```bash
cd backend
venv/Scripts/python -m app.trip.seed --list        # 看库里有没有数据（空=还没初始化）
curl -s -x "" localhost:8000/api/health            # 看 Key 与库是否就绪
venv/Scripts/python ../scripts/check_planner.py    # 规则引擎回归（8 个用例）
```

三个都 OK 才算环境就绪。`llm_ready=false` 不是错误——规划会走规则引擎，功能完整。

> Windows 上 `curl` 会走系统代理，访问 localhost 必须加 `-x ""`，否则报「积极拒绝」。

---

## 1. 目录职责（改代码前先看这张表）

| 路径 | 职责 | 改动风险 |
|---|---|---|
| `core/config.py` | 全部配置项。**新增配置只加这里**，其它模块只读 | 低 |
| `core/db.py` | 引擎/会话/建表/MySQL 自动建库 | 低 |
| `core/_llm/` | LLM 私有包：`LlmClient` / 模型注册表 / provider 兜底 | **高**，是所有 LLM 调用的地基 |
| `trip/schemas.py` | 请求出参 + 时间轴契约（`PlanResult`）+ 终检契约（`PlanReview`） | **高**，改了要同步前端 |
| `trip/planner.py` | 地理/交通/容量/时段规则 + **时间轴物化（唯一产时间的地方）** | **高**，见铁律 2 |
| `trip/llm.py` | 两阶段并行流水线 + 提示词 + 终检 | 中，提示词是生成质量的全部 |
| `trip/service.py` | 业务编排：基线 → 流水线 → 终检 → 落库；紧凑度预估；倾向微调 | 中 |
| `trip/routers.py` | **7 条** HTTP 路由，只做协议与参数校验 | 低 |
| `trip/amap.py` | 高德客户端。**只有 `search_poi()` 在用**（seed 补坐标），`search_around`/`search_restaurants` 无调用方 | 中 |
| `trip/seed.py` | 一次性数据初始化（会打外部 API，有 0.25s 串行节流） | 低 |
| `trip/tools.py` | ⚠️ **旧的 function calling 工具集，当前无任何调用方**。只有 `audit_day()` 还在被 `service.audit_plan` 使用 | **小心**，别以为它在跑 |
| `frontend/src/trip-api.js` | 接口封装。**前端所有请求都从这里出** | 低 |
| `frontend/src/trip-store.js` | localStorage 历史 + 上次勾选偏好 | 低 |
| `frontend/src/trip-theme.js` | 按天配色（7 色循环）、节点图标、节奏标签（地图与时间轴共用） | 低 |
| `frontend/src/use-media.js` | 视口断点（641px，`matchMedia`）。只在「设置面板形态」与「地图按钮位置」两处需要 | 低 |
| `frontend/src/main.js` | 路由表 + **4 层全局错误兜底**（异常渲染成可见面板而非白屏） | 中 |
| `frontend/src/App.vue` | 站点骨架。**不要给 router-view 套 transition**，见铁律 10 | 中 |
| `frontend/src/style.css` | 全局设计系统（32 个 CSS 变量），**桌面优先** | 中 |
| `frontend/src/views/trip/*.vue` | 4 个页面，与小程序 4 个 page 一一对应 | 中 |

---

## 2. 铁律

1. **不要让 LLM 产出任何时间。** 时间、停留、路程、饭点位置只能由 `planner.materialize_day()` 算。
   曾经让模型直接吐时间轴，实测出现：12:10 到景点却 12:30 吃午饭、晚餐合并成 14:05 的下午茶、
   同一个景点排进两天、熊猫基地排到 14:20（熊猫在午睡）。提示词写两遍都没拦住。
   想增加「模型参与决策」的能力，就在阶段②的提示词里加**文案字段**，不要放开时间。

2. **`materialize_day()` 里的 `push()` 是唯一的时间推进点。** 任何新节点都必须走它，
   `cur_time` 只能在那一处更新。这样 `prev.time + prev.stay + cur.travel == cur.time` 恒成立。
   绕过 `push()` 直接 append 节点，时间轴立刻不自洽。

3. **运行时复核只记日志，不拦截。** `service.audit_plan()`（内部调 `tools.audit_day()`）
   发现时间不自洽/饭点不对只会打 warning，**不会阻断请求**。别误以为它是安全网——
   真正拦住错误的是 `materialize_day` 的算法本身和 `check_planner.py` 回归。
   （`tools.py` 里那三道「返回 `ok:false` 打回」的逻辑属于旧工具链，当前不在执行路径上。）

4. **坐标一律 GCJ-02。** 高德返回的 `location` 就是 GCJ-02，可直接入库。
   混入 WGS-84 或 BD-09 会让地图整体偏移几百米，而且**不会报错**，只能靠肉眼发现。
   `Attraction.coord_source` 记录了每条的来源，排查时先看它。

5. **高德接口只在 seed 阶段用。** 规划本身**完全不依赖地图接口**（距离用 haversine 本地算），
   运行时也不搜餐厅（餐饮改成「区域 + 本地特色小吃」后已废弃）。
   别为了「更准」去接路径规划 API——一次规划十几条腿，打接口既慢又费配额。
   `amap.py` 里的 `search_around` / `search_restaurants` 是无调用方的遗留代码。

6. **降级必须留痕。** 切规则引擎时要往 `summary` 里写降级原因、把 `source` 置为 `fallback`、
   前端打标签。静默降级会让人误判生成质量。

7. **`core/_llm/` 只从 `app.core` 入口用。** 业务代码写 `from app.core import llm`，
   不要 `from app.core._llm._native import ...`。

8. **所有 LLM 的结构化输出必须走 `llm.with_structured_output()` 或 `extract_json()`。**
   模型会返回带 ```` ```json ```` 围栏、尾逗号、全角引号的 JSON。自己写 `json.loads` 必崩。

9. **改完必须跑回归。** `scripts/check_planner.py`（8 个用例，逐节点校验时间自洽、饭点数量与时段、
   主题一致性、景点不凭空消失）。改提示词或 `planner.py` 后必跑。
   注意它**需要连数据库**（要读景点数据），且目前只覆盖 `taxi`（=mixed）与 `transit`，缺 `drive` 与首末天时间窗。

10. **本机个人操作放 `local/`**（已 gitignore）：一次性脚本、含真实 Key 或本机路径的清单。
    临时验证脚本用 `.tmp-*` 前缀（同样被忽略，用完即删）。

11. **不要给 `<router-view>` 套 `<transition mode="out-in">`。** 踩过一次：
    离场过渡不完成会导致新页面永不挂载——路由变了、`router-view` 渲染成空注释，
    表现是**白屏，但直接打开/硬刷新同一个 URL 完全正常**（因为不经过过渡）。
    编译不报错、接口也正常，只有真跑浏览器才发现。改动页面结构后跑 `scripts/smoke_ui.py`。

12. **页面必须单根节点。** 每个 `views/trip/*.vue` 的模板都包在 `<div class="page-root">` 里，
    这是路由切换时布局能正常工作的前提。

13. **样式桌面优先。** `style.css` 默认写宽屏布局，用 `@media (max-width: 640px)` 覆盖成手机形态。
    新增组件时先想宽屏长什么样，再补手机覆盖——反过来会让宽屏变成「拉大的手机」。

14. **布尔属性一律写 `!!busy`。** `busy` 用 `''` 表示空闲，而 Vue 对布尔属性的判断是
    `!!value || value === ''`——因为 `disabled=""` 在 HTML 里就是「禁用」。写 `:disabled="busy"` 会让按钮永久禁用。

---

## 3. 常用操作

| 任务 | 命令 |
|---|---|
| 起前后端 | 根目录 `npm run dev`（前端 5173 + 后端 8000） |
| 只起后端 | `cd backend && venv/Scripts/python run.py`（`--host` / `--port` / `--no-reload`） |
| 灌数据 | `cd backend && venv/Scripts/python -m app.trip.seed`（`--city 成都` / `--reset` / `--no-amap` / `--only-cities`） |
| 看库里现状 | `venv/Scripts/python -m app.trip.seed --list` |
| 规则引擎回归 | `cd backend && venv/Scripts/python ../scripts/check_planner.py` |
| 前端冒烟测试 | `python scripts/smoke_ui.py`（需 playwright + 系统 Edge/Chrome；`--skip-plan` 跳过规划环节） |
| 健康检查 | `curl -s -x "" localhost:8000/api/health` |
| 模型别名总表 | `curl -s -x "" localhost:8000/api/health/models` |
| 接口文档 | 浏览器开 http://localhost:8000/docs |

---

## 4. 改配置

全部配置项在 `backend/app/core/config.py`，`.env` 里写。变量名与 my-website 对齐
（`DATABASE_URL` / `DEEPSEEK_API_KEY`），未来并库可直接复用。

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | MySQL 连接串；留空 → SQLite `backend/data/triptide.db` |
| `DEEPSEEK_API_KEY` | 也可写 `MODEL_API_KEY`，两者等价 |
| `MODEL_BASE_URL` / `MODEL_NAME` | 默认 DeepSeek 官方 / `deepseek-flash` |
| `AMAP_KEY` | 高德 **Web 服务** Key（不是 JS API Key），仅 seed 用 |
| `LLM_TIMEOUT` | ✅ 有效。httpx / OpenAI 客户端超时，一次完整规划约 15~30s，别设太小 |
| `PRIVATE_LLM_*` | 私有化部署用，配了之后 `deployment="private"` 才生效 |

> ⚠️ **六个配置项是死的**：`LLM_MAX_RETRY`、`LLM_TEMPERATURE`、`LLM_MAX_TOKENS`、
> `DEFAULT_START_TIME`、`DEFAULT_RETURN_TIME`、`MAX_DAYS` 在代码里**零引用**。
> 实际温度/上限硬编码在 `llm.py` 的各调用点；时间与天数默认值写死在 `schemas.py`。
> **改 `.env` 里这几项不会生效**——要么改代码，要么先把配置接通。

前端地图 Key 走 `frontend/.env` 的 `VITE_AMAP_JS_KEY`（高德 **JS API** Key，与上面的 Web 服务 Key 不通用）。
不填则 `TripMap.vue` 用 GCJ-02 等距投影画 SVG 示意图。若控制台给 Key 绑了「安全密钥」，
还要填 `VITE_AMAP_SECURITY_CODE`——`TripMap.vue` 会在加载脚本前注入 `window._AMapSecurityConfig`，漏了地图不渲染。

---

## 5. 换模型

改 `.env` 的 `MODEL_NAME` 即可，别名在 `core/_llm/_registry.py` 的 `_MODEL_CATALOG` 里映射到真实模型名。
新增模型往那张表加一行。切私有化部署填 `PRIVATE_LLM_*` 三个变量。

| 别名 | 真实模型 |
|---|---|
| `deepseek-flash`（默认）/ `deepseek` / `default` | `deepseek-flash` |
| `deepseek-chat` / `deepseek-v3` | `deepseek-chat` |
| `deepseek-reasoner` / `deepseek-r1` | `deepseek-reasoner` |

> **未登记的别名会原样透传**，不会报错——打错模型名会直接把错的字符串发给 API。
> 现在流水线**只依赖 JSON 输出，不再依赖 function calling**，所以换模型的门槛比旧版本低。
> 但 `deepseek-reasoner` 会慢好几倍，不建议用在阶段②的并行调用上。

---

## 6. 排查

| 现象 | 先看这里 |
|---|---|
| 首页城市宫格空 | 没灌数据 → `python -m app.trip.seed` |
| 方案带「规则引擎生成」标签 | 正常降级。看后端日志确认原因（无 Key / 流水线异常） |
| 规划超过 150s 超时 | 勾太多景点了。减少勾选，或调大 `LLM_TIMEOUT` |
| 规划比预期慢 | 换回 `deepseek-flash`（默认）；`deepseek-reasoner` 会慢好几倍 |
| 地图点位整体偏移 | 坐标混了坐标系，见铁律 4 |
| 地图白屏 / **二次打开弹窗报错** | 高德实例没跟着 `v-if` 一起销毁（`TripMap.vue` 的 `watch(props.open)` + `destroyAmap`）；或 Key 缺安全密钥配置 |
| 地图一直显示「示意地图」 | `frontend/.env` 没填 `VITE_AMAP_JS_KEY`，或域名白名单没配 |
| 餐饮都是「片区·本地特色」 | **这是设计如此**（不写店名，店会换），不是降级 |
| 选景点时提示「超载」但加天数没变化 | `preview` 的 `load` 对天数不敏感（分子分母同比例增长），档位实际由舍弃率驱动。见 `docs/API.md` 的推导 |
| 熊猫基地又被排到下午 | 查 `planner.time_rule()` 的 `_MORNING_KEYS` 是否被改动 |
| 点城市后白屏、**硬刷新就好** | `<router-view>` 被套了 `<transition>`，见铁律 11 |
| 改完一堆文件后整站白屏 | 前端 HMR 模块图坏了：删 `frontend/node_modules/.vite`，重启 dev server，浏览器 `Ctrl/Cmd+Shift+R` |
| 页面显示「页面渲染出错」面板 | 这是 `main.js` 的错误兜底在工作，面板里有具体堆栈——比白屏好排查 |
| 从行程页点返回报「缺少 city 参数」 | `TripPlan.vue` 的 `backTo` computed 没带上 `plan.request.city` |
| 按钮永久禁用 | `:disabled="busy"` 而 `busy` 是空字符串，见铁律 14 |
| 宽屏下界面像「拉大的手机」 | 只写了手机样式没写宽屏样式，见铁律 13 |
| MySQL 连不上 | `DATABASE_URL` 里的账号要有建库权限，或先手工建库 |
| `openai` 未安装 | 不致命，`core/_llm/_native.py` 自动降级 httpx 实现 |

---

## 7. 日志里的关键信号

后端日志中这几行最能说明问题（流水线版）：

```
plan_days days=4 kept=10 dropped=2          # ① 分天完成
gen_day day=1 ok / gen_day day=3 ✗           # ② 各天并行生成结果（✗ = 该天回退规则文案）
materialize days=4 elapsed=17.7s            # ③ 物化耗时（≈ 整条链路总耗时）
落库前复核发现 K 处问题：...                  # audit_plan 抓到的问题（正常应为 0）
LLM 流水线失败，降级到规则引擎：...            # 整体降级
```

这些内容同时也是 `PlanResponse.trace` 数组，排查时直接看接口返回更快，不用翻日志。

---

## 8. 想动这套架构之前

改之前请先读 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)——里面记录了**每一次架构决策的实测数据**
（为什么放弃工具编排、交通分档的修正效果、分天五步各自的动机）。
这个项目里「看起来可以简化」的地方，多数都踩过坑。

另外 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#待办与建议) 的「待办与建议」列了当前已知的
死代码、注释错误、口径不一致问题，动手前扫一眼可以避免撞上。
