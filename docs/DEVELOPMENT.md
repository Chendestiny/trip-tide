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
venv\Scripts\python -m app.trip.seed              # 全部 8 城
venv\Scripts\python -m app.trip.seed --city 成都   # 只灌一个城市
venv\Scripts\python -m app.trip.seed --list       # 看库里现状
```

有 Key 时走「LLM 出名单 → 高德 POI 搜索补坐标」；无 Key 时用离线种子（`data/seed_attractions.json`，8 城 × 20 景 = 160 条）。
一次全量约 160–200 次高德请求，受 `AMAP_SLEEP=0.25s` 串行节流，**约 40 秒以上**。

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
npm run dev        # → http://localhost:5173
```

`frontend/.env` 里可选配 `VITE_AMAP_JS_KEY`（高德 **JS API** Key）。不填则地图用 `TripMap.vue` 的 SVG 示意图，其余功能不受影响。

### 4. 一条命令起前后端（推荐）

```bash
npm install        # 根目录，装 concurrently
npm run dev        # 前端 5173 + 后端 8000
npm run seed       # 等价于 backend 里的初始化
```

浏览器打开 http://localhost:5173，`/api` 由 Vite 自动代理到 8000。

---

## 配置项

全部在 `backend/app/core/config.py`，值写在 `backend/.env`。变量名与 my-website 对齐，未来并库可直接复用。

| 变量 | 不填/留空的后果 |
|---|---|
| `DATABASE_URL` | 退化为 `backend/data/triptide.db`（SQLite） |
| `DEEPSEEK_API_KEY`（或 `MODEL_API_KEY`） | 规划走本地规则引擎兜底，仍能出完整方案、`source=fallback` |
| `MODEL_BASE_URL` / `MODEL_NAME` | 默认 DeepSeek 官方 + `deepseek-flash` |
| `AMAP_KEY` | seed 阶段不校准坐标，用种子里手校的值 |
| `LLM_TIMEOUT` | ✅ **有效**，用于 httpx / OpenAI 客户端超时 |
| `PRIVATE_LLM_API_KEY` / `_BASE_URL` / `_MODEL` | 私有化部署用；配了之后 `deployment="private"` 才生效 |
| `CORS_ORIGINS` | 默认只放行 `localhost:5173` / `127.0.0.1:5173` |

> ⚠️ **六个配置项是死的**：`LLM_MAX_RETRY`、`LLM_TEMPERATURE`、`LLM_MAX_TOKENS`、`DEFAULT_START_TIME`、`DEFAULT_RETURN_TIME`、`MAX_DAYS` 在代码里**零引用**。实际值硬编码在各调用点（`llm.py` 的 0.6/2000、0.3/1200、0.8/4096），时间与天数默认值写死在 `schemas.py`。改 `.env` 里这几项**不会生效**——要么改代码，要么先把配置接通。

前端地图 Key 走 `frontend/.env` 的 `VITE_AMAP_JS_KEY`（高德 **JS API** 类型，与后端的 Web 服务 Key **不通用**）。若控制台给 Key 绑了「安全密钥」，还要填 `VITE_AMAP_SECURITY_CODE`（`TripMap.vue:295` 会在加载脚本前注入 `window._AMapSecurityConfig`，漏了地图不渲染）。

---

## 常用命令

| 任务 | 命令 |
|---|---|
| 起前后端 | 根目录 `npm run dev` |
| 只起后端 | `cd backend && venv/Scripts/python run.py`（支持 `--host` / `--port` / `--no-reload`） |
| 灌数据 | `cd backend && venv/Scripts/python -m app.trip.seed` |
| 看库里现状 | `venv/Scripts/python -m app.trip.seed --list` |
| 后端规则引擎回归 | `cd backend && venv/Scripts/python ../scripts/check_planner.py` |
| 前端冒烟测试 | `python scripts/smoke_ui.py`（需先起前后端） |
| 健康检查 | `curl -s -x "" localhost:8000/api/health` |
| 模型别名总表 | `curl -s -x "" localhost:8000/api/health/models` |
| 交互式接口文档 | http://localhost:8000/docs |

> Windows 下 `curl` 会走系统代理，访问 localhost **必须加 `-x ""`** 绕过，否则报「积极拒绝」。

---

## 回归测试

### 后端：`scripts/check_planner.py`

**不启服务**，直接调 `planner.plan_fallback()` 跑 8 个用例，逐节点校验 6 项。

```bash
cd backend && venv/Scripts/python ../scripts/check_planner.py
```

校验项：① 时间自洽（`prev.time + prev.stay + cur.travel == cur.time`）② 午餐恰好 1 个且落在 11:00~16:00 ③ 晚餐恰好 1 个且落在 17:30~21:30 ④ 出发时间等于用户设定、最后一个节点是 hotel ⑤ 主题里提到的景点必须真的在当天时间轴 ⑥ 每个勾选景点要么排入要么进 `dropped`（不能凭空消失）。

8 个用例（`CASES`，121–130 行）：

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

> ⚠️ **这个脚本需要连数据库**（要读景点数据），所以严格说不是纯离线。另外它的 `transport` 还在用历史值 `taxi`（会被 `PlanRequest` 归一为 `mixed`），**没有覆盖 `drive` / `transit` 新档位**——建议补用例。

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
- `BASE = http://localhost:5173`，默认城市成都

> 本机环境提示：`agent-browser` 类工具在 Windows 上不可用，`smoke_ui.py` 借系统 Edge（`channel="msedge"`）是本机唯一可行的浏览器自动化路径。

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

---

## 日志信号

后端日志里这几行最能说明问题（流水线版）：

```
plan_days days=4 kept=10 dropped=2          # ① 分天完成
gen_day day=1 ok / gen_day day=3 ✗           # ② 各天并行生成结果（✗ = 该天回退规则文案）
materialize days=4 elapsed=17.7s            # ③ 物化耗时（这是整条链路的总耗时）
落库前复核发现 K 处问题：...                  # audit_plan 抓到的问题（正常应为 0）
LLM 流水线失败，降级到规则引擎：...            # 整体降级
```

`trace` 字段（`PlanResponse.trace`）就是上面第一段的原始数组，排查时直接看接口返回，不用翻日志：

| trace 内容 | 来源 |
|---|---|
| `plan_days days=N kept=X dropped=Y` | `llm.py:287` |
| `gen_day day=N ok` / `gen_day day=N ✗` | `llm.py:318 / 321` |
| `materialize days=N elapsed=X.Xs` | `llm.py:377` |
| `adjust mode=tweak pace=P days=D restored=N` | `service.py:408` |

---

## 待办与建议

按性价比排序，**都不影响当前运行**，但会误导接手的人。

### 高优先：会直接误导的

1. **清掉或标注死代码**。`tools.py` 整套工具链（10 个工具 + `PlanSession` + `dispatch`）与 `amap.py` 的 `search_around` / `search_restaurants` 都**没有调用方**。`llm.py:35` 声称「用 `LLM_MODE=tools` 打开」——该开关从未实现。
   → 建议：要么补上 `LLM_MODE` 开关让它真能用，要么在文件头明确标注「预留能力，当前未启用」。**保留 `audit_day()`，它在用。**
2. **修注释与实际不符的地方**（共 8 处，清单见 [`BACKEND.md`](BACKEND.md#代码与注释不一致的地方写文档改代码时以代码为准)）。其中 `trip/__init__.py:6`「4 个端点」、`README`/`AGENTS` 里的「11 个工具」影响最大。
3. **决定那 6 个死配置的去留**：接通，或者删掉，或者注释说明「暂不生效」。

### 中优先：影响正确性/一致性

4. **`preview` 的判定指标有两个缺陷**，都在 `service.preview_plan`（96–194）：
   - **容量口径不一致**：`capacity = per_day_budget × days`，**不含** `CAPACITY_SLACK=1.10`；而 `planner.prune_to_capacity` 内部乘了。预估口径比实际裁剪**紧 10%**。
   - **`load` 对天数不敏感**：分子（游览 + 路程）与分母（容量）随天数近似同比例增长，实测 1/2/3/4 天的 `load` 分别是 0.83 / 0.82 / 0.89 / 0.90，几乎不动。档位实际由 `drop_ratio` 单方面驱动。→ 用户加天数时，`load` 不降，容易让人误解为「加了天数没用」。
     若要修，可考虑用「单天平均负载」（`load / days` 归一化后取峰值天）替代总负载。
   - 附带观察：**必去景点在容量裁剪时仍可能被丢**（`_value` 里有 +30 热度加成，但抵不过远郊景点的高耗时）。成都「前 10 景 × 1 天」实测把必去的大熊猫基地丢了——产品上是否合理需要定夺。
5. **前端重复定义**：`TripPlan.vue:255` 本地定义的 `PACE_LABEL` 与 `trip-theme.js:45` 的重复。`trip-theme` 里的 `DAY_COLORS` 与 `PACE_LABEL` 零引用。→ 统一到 `trip-theme.js`。
6. **`check_planner.py` 补用例**：现在只覆盖 `taxi`（=`mixed`）和 `transit` 两种，缺 `drive`；也没覆盖首末天不同时间窗（`first_day_start_time` / `last_day_return_time`）。
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
