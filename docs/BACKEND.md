# 后端地图

> 符号级导航。**行号会随改动漂移 —— 查找代码请以函数名为准**，顺手改这份文档。
>
> ⚠️ **2026-09-15 有一次大改**（分天模型 v2 / 弹性时长 / 一键 AI / `best_time` / `trip_spot` 子表 /
> LLM 分天审阅），本文件里的部分行号与细节可能滞后。
> 想知道「为什么现在长这样」先看 [`CHANGELOG.md`](CHANGELOG.md)。
>
> 需要先建立全局认知请看 [`README.md`](README.md)；要改代码前请先读 [`../AGENTS.md`](../AGENTS.md) 的铁律。

---

## 分层

```
backend/
├── run.py                    本地启动（uvicorn + 热重载）
└── app/
    ├── main.py               FastAPI 实例：CORS + 路由挂载 + 启动建表
    ├── core/                 基础设施层（与业务无关，可整目录搬走）
    │   ├── config.py         配置中心
    │   ├── db.py             引擎 / 会话 / 建表 / MySQL 自动建库
    │   └── _llm/             LLM 私有包（`_` 前缀 = 私有实现）
    └── trip/                 业务包
        ├── models.py         4 张表
        ├── schemas.py        请求/响应 + LLM 输出契约
        ├── routers.py        7 条 HTTP 路由
        ├── service.py        业务编排
        ├── planner.py        ★ 地理/交通/容量/时段规则 + 时间轴物化
        ├── llm.py            ★ 两阶段并行流水线 + 终检
        ├── tools.py          function calling 工具集（**当前无调用方**，见文末）
        ├── amap.py           高德客户端
        └── seed.py           一次性数据初始化
```

依赖方向**严格单向**，不要破坏：

```
routers → service → { planner, llm }  →  core
                ↘  models  ↙
llm → planner + core._llm        tools → planner（仅 tools 单向）
core 不 import trip
```

---

## `core/` 基础设施层

### `core/config.py`（119 行）

**唯一配置来源**。环境变量 > `backend/.env` > 代码默认值（`pydantic-settings`，`extra="ignore"`、大小写不敏感）。

| 符号 | 行号 | 说明 |
|---|---|---|
| `BACKEND_DIR` / `PROJECT_DIR` / `FRONTEND_DIR` / `DATA_DIR` | 18–22 | 路径常量 |
| `class Settings` | 25 | 配置模型，字段见下表 |
| `Settings.resolved_db_url` | 86 | property。`db_url` 为空 → 退化为 SQLite `backend/data/triptide.db` |
| `Settings.is_sqlite` | 94 | property |
| `Settings.has_llm` | 98 | `bool(model_api_key.strip())` |
| `Settings.has_amap` | 102 | `bool(amap_key.strip())` |
| `Settings.cors_origin_list` | 106 | 逗号切分；`"*"` 直接返回 `["*"]` |
| `get_settings()` | 114 | `@lru_cache(maxsize=1)` |
| `settings` | 118 | 模块级单例 |

关键字段（行号 → 字段 = 默认值）：

| 行号 | 字段 | 默认值 | 环境变量别名 |
|---|---|---|---|
| 33–35 | `app_name` / `app_version` / `debug` | `TripTide API` / `0.1.0` / `True` | — |
| 39 | `db_url` | `""`（→SQLite） | `DATABASE_URL` / `DB_URL` |
| 44 | `db_echo` | `False` | `DB_ECHO` |
| 48 | `model_api_key` | `""` | `MODEL_API_KEY` / `DEEPSEEK_API_KEY` |
| 52 | `model_base_url` | `https://api.deepseek.com/v1` | `MODEL_BASE_URL` / `DEEPSEEK_BASE_URL` |
| 56 | `model_name` | `deepseek-flash` | `MODEL_NAME` / `DEEPSEEK_MODEL` |
| 62–64 | `private_llm_api_key` / `_base_url` / `_model` | `""` | `PRIVATE_LLM_*` |
| 66–69 | `llm_timeout` / `llm_max_retry` / `llm_temperature` / `llm_max_tokens` | `180.0` / `1` / `0.7` / `8192` | 同名 |
| 72–74 | `amap_key` / `amap_base_url` / `amap_timeout` | `""` / `https://restapi.amap.com` / `15.0` | `AMAP_KEY` 等 |
| 77 | `cors_origins` | `http://localhost:5173,http://127.0.0.1:5173` | `CORS_ORIGINS` |
| 80–82 | `default_start_time` / `default_return_time` / `max_days` | `09:00` / `19:30` / `7` | — |

> ⚠️ **死配置**：`llm_max_retry`(67)、`llm_temperature`(68)、`llm_max_tokens`(69)、`default_start_time`(80)、`default_return_time`(81)、`max_days`(82) 在 `app/` 内**零引用**。实际温度/上限硬编码在各调用点（`llm.py:213` 用 `0.6/2000`；`llm.py:408` 用 `0.3/1200`；`llm.py:178` 默认 `0.8/4096`）；时间与天数默认值写死在 `schemas.py`。改这些配置项**不会生效**。

### `core/db.py`（98 行）

| 符号 | 行号 | 说明 |
|---|---|---|
| `class Base(DeclarativeBase)` | 25 | ORM 基类 |
| `_engine_kwargs()` | 29 | SQLite → `check_same_thread=False`；MySQL → `pool_pre_ping` + `pool_recycle=3600` + `pool_size=5` + `max_overflow=10` |
| `engine` | 36 | 全局引擎（模块级创建） |
| `SessionLocal` | 43 | `sessionmaker(autoflush=False, expire_on_commit=False, future=True)` |
| `ensure_database()` | 46 | MySQL 下查 `information_schema.SCHEMATA`，不存在则 `CREATE DATABASE ... utf8mb4_unicode_ci` |
| `init_db()` | 82 | 建库 + `from app.trip import models`（**必须保留这行**，否则 `create_all` 看不到表）+ `create_all` |
| `get_db()` | 92 | FastAPI 依赖，`yield` 后 `close()` |

### `core/__init__.py`（84 行）

业务代码的**唯一入口**，用 PEP 562 惰性导入打破 `config ←→ _llm` 的循环依赖。

```python
from app.core import llm, settings, SessionLocal, get_db   # ✅ 这样用
from app.core._llm._native import _native_chat             # ❌ 禁止
```

`__all__`(16–33) 共 17 个名字；`_LAZY`(51–68) 是 `名字 → (模块, 属性)` 映射表；`__getattr__`(71) 惰性 import 后写入 `globals()` 缓存。

### `core/_llm/` LLM 私有包

| 文件 | 行数 | 职责 |
|---|---|---|
| `__init__.py` | 215 | `LlmClient` + `llm` 单例；三种调用形态 |
| `_native.py` | 276 | OpenAI SDK 直调 + httpx 裸协议兜底 |
| `_registry.py` | 39 | 模型别名 → 真实模型名 |
| `_schemas.py` | 65 | `LLMResponse` / `ToolCall` / `LLMFormatError` |

#### `LlmClient` 三种调用形态（`__init__.py`）

| 方法 | 行号 | 用途 |
|---|---|---|
| `chat(messages, *, model="", plugin="native", deployment="default", **kwargs)` | 92 | 裸对话，返回 `LLMResponse` |
| `with_structured_output(messages, schema, ...)` | 110 | **强制 `response_format={"type":"json_object"}`**(131) + `extract_json` + schema 校验；失败抛 `LLMFormatError(raw=...)` |
| `run_tools(messages, tools, dispatch, *, max_rounds=12, stop_when=None, on_tool=None, ...)` | 145 | function calling 编排循环（**当前无业务调用方**） |

`run_tools` 循环（171–211）：`chat(tools=..., tool_choice="auto")` → 追加 assistant 消息 → 无 `tool_calls` 则 break → 逐个 `dispatch(名, 参数)`（异常吞成 `{"ok":False,"error":...}`）→ 以 `role="tool"` 回灌 → `stop_when()` 命中则 break。撞满 `max_rounds=12` 会打 warning。

#### JSON 容错抽取 `extract_json(text)`（`__init__.py:66`）

LLM 返回不可靠，四步兜底：剥 ```` ```json ```` 围栏 → `find("{")`/`rfind("}")` 截取 → `json.loads` → 失败则修尾逗号 + 全角引号重试。**任何改 LLM 输出的地方都要走它。**

#### 模型注册表（`_registry.py`）

| 别名 | 真实模型名 |
|---|---|
| `""`（空）/ `default` / `deepseek` / `deepseek-flash` / `private` | `deepseek-flash` |
| `deepseek-chat` / `deepseek-v3` | `deepseek-chat` |
| `deepseek-reasoner` / `deepseek-r1` | `deepseek-reasoner` |

`resolve_model_name(key, deployment)`(27)：`deployment=="private"` 时优先 `settings.private_llm_model`。
**未登记的别名原样透传**（33 行），不会报错——打错模型名会直接把错的字符串发给 API。

#### provider 兜底（`_native.py`）

`dispatch_chat(plugin, **kwargs)`(266)：`plugin="native"` 且未装 `openai` SDK 时自动降级到 `_httpx_chat()`(187) 裸 HTTP 实现。本机 venv 已装 `openai`，运行时走 SDK 分支。

关键实现细节：

- `deployment != "private"` 时才注入 `extra_body={"enable_thinking": 8192 if enable_thinking else False}`（80–81），**8192 是思考 token 预算**。
- httpx 分支需把 `extra_body` **平铺到顶层**（212）。
- 异常统一转中文：限流 / 超时 / `API失败 (HTTP xxx)` / 空 choices → `"模型返回空结果"`（144–154）。
- `_get_openai_client`(47) **未传 `max_retries`**，重试次数沿用 SDK 默认值。

---

## `trip/` 业务层

### `models.py` — 4 张表

四张表全部 `trip_` 前缀，与同库的 my-website 表互不干扰。**经纬度全库统一 GCJ-02。**

#### `trip_city` → `City`(34)

| 字段 | 行号 | 类型 / 约束 |
|---|---|---|
| `id` | 39 | Integer PK autoincrement |
| `name` | 40 | String(32) **unique** not null |
| `pinyin` | 41 | String(32) |
| `emoji` / `tagline` | 42–43 | String(8) / String(64) |
| `heat` | 44 | Integer |
| `center_lat` / `center_lng` | 45–46 | Float |
| `hotel_areas` | 47 | **JSON**，元素 `{area, lat, lng, pros, cons}` |
| `created_at` | 52 | DateTime server_default now() |
| `attractions` | 54 | relationship，`cascade="all, delete-orphan"` |

#### `trip_attraction` → `Attraction`(59)

`__table_args__`(63–66)：`UniqueConstraint("city_id","name")` + `Index("ix_attraction_city_heat", "city_id", "heat")`

| 字段 | 行号 | 类型 / 约束 |
|---|---|---|
| `id` | 68 | Integer PK |
| `city_id` | 69 | FK `trip_city.id` **ondelete=CASCADE** |
| `name` | 72 | String(64) not null |
| `intro` | 73 | String(255) |
| `district` | 74 | String(32) |
| `lat` / `lng` | 75–76 | Float **not null** |
| `visit_minutes` | 77 | Integer default 90 |
| `heat` | 78 | Integer default 50 |
| `must_visit` | 79 | Boolean default False |
| `tags` | 80 | **JSON** 数组 |
| `coord_source` | 81 | String(16)：`amap` / `seed`（注释里的 `manual` **代码从未写入**） |

#### `trip_plan` → `TripPlan`(89)

`__table_args__`(93)：`Index("ix_trip_plan_created", "created_at")`

| 字段 | 行号 | 说明 |
|---|---|---|
| `id` | 95 | PK，**就是对外暴露的 `plan_id`** |
| `city` / `days` / `attraction_count` | 96–98 | 冗余字段，便于列表展示 |
| `source` | 99 | String(16)：`llm` / `fallback` / `tweak`（注释只写了前两个，第三个是 `adjust_plan` 写的） |
| `title` | 102 | String(128) |
| `request_json` | 103 | **JSON**，`PlanRequest.model_dump()` |
| `result_json` | 104 | **JSON**，三元包装 `{"plan": {...}, "review": {...}\|null, "trace": [...]}` |
| `created_at` | 105 | DateTime |

> **`trip_plan` 无外键、不做关系化**：入参出参整体存 JSON 快照。理由见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。读取时由 `service._unwrap()`(420) 兼容老格式（裸 `PlanResult`）。

---

### `schemas.py`（325 行）— 双重身份

**既是对外 HTTP 契约，又是 LLM 输出的强校验 schema**（校验失败即触发重试）。

#### 枚举

| 符号 | 行号 | 取值 |
|---|---|---|
| `NodeType` | 16 | `depart` / `attraction` / `meal` / `hotel` / `transit` |
| `Pace` | 17 | `轻松` / `适中` / `紧凑`（前端展示用，中文） |
| `Transport` | 23 | `drive` / `mixed` / `transit` / `taxi`（taxi 为历史值） |
| `PacePref` | 25 | `relaxed` / `balanced` / `packed`（入参用，英文） |
| `TRANSPORT_LABELS` | 85 | `taxi` 与 `mixed` 都映射到「打车+公共」 |
| `PACE_LABELS` | 86 | 中英映射 |

#### `PlanRequest`(29) — 入参

| 字段 | 行号 | 约束 |
|---|---|---|
| `city` | 32 | 必填，城市名或拼音 |
| `attraction_ids` | 33 | 必填，`min_length=1`，自动保序去重(66) |
| `days` | 34 | `ge=1, le=7`，默认 1 |
| `start_time` / `return_time` | 35–36 | `HH:MM`，默认 `09:00` / `19:30` |
| `first_day_start_time` / `last_day_return_time` | 38–43 | 可空；空则回落到 `start_time` / `return_time` |
| `transport` | 44 | 默认 `mixed`；`_norm_transport`(60) 把 `taxi` 归一为 `mixed` |
| `pace` | 45 | 默认 `balanced` |
| `plan_id_hint` | 46 | 仅前端标记用 |

`transport_label`(76) / `pace_label`(80) 两个 property 供提示词直接引用。
`_norm_hhmm`(89) 兼容全角冒号并做范围校验。

#### LLM 输出契约

| 模型 | 行号 | 要点 |
|---|---|---|
| `TimelineNode` | 101 | `time`(106) / `type` / `name`(≤64) / `advice`(≤200) / `attraction_id` / `stay_minutes`(0–600) / `travel_minutes`(0–600) / `travel_mode`(≤16) / `must_visit` / `moved_to_day` / `moved_from_day` / `move_reason`(≤120) |
| `HotelAdvice` | 142 | `area`(≤64) / `reason`(≤300) / `alternatives: [{area, tradeoff}]` |
| `DayPlan` | 152 | `day`(1–7) / `theme`(≤80) / `pace`(默认适中，`_norm_pace`(161) 归一英文近义词) / `nodes` / `hotel` |
| `PlanResult` | 193 | `city` / `days` / `summary`(≤300) / `day_plans`(`min_length=1`) / `must_visit_reasons` / `dropped`；`_check_days`(205) 强制 `day` 从 1 连续递增 |
| `DroppedItem` | 175 | `name` / `reason`(≤200) / `attraction_id` |
| `PlanReview` | 225 | `ok` / `summary`(≤300) / `issues: [ReviewIssue]` |
| `ReviewIssue` | 217 | `day`(可空) / `level: info\|warn\|error` / `text`(≤200) |

> 所有这些模型都带 `model_config = ConfigDict(extra="ignore")`——**LLM 多输出的字段会被静默丢弃**，这是有意的容错设计。

#### 响应契约

| 模型 | 行号 | 要点 |
|---|---|---|
| `CityOut` | 237 | 含 `attraction_count`（由 service 现算，不是表字段） |
| `AttractionOut` | 251 | 前端画地图所需全部字段 |
| `PlanResponse` | 266 | `plan_id` / `created_at`(str) / `source: llm\|fallback\|tweak` / `title` / `request` / `result` / `attractions` / `review` / `trace` |
| `PlanPreview` | 298 | 紧凑度预估出参，字段见 [`API.md`](API.md)#3 |
| `PlanBrief` | 286 | 历史列表轻量条目 |
| `ApiError` | 323 | **定义但无人使用**（`main.py` 异常处理器直接返回裸 dict） |

---

### `routers.py` — 9 条路由

挂载前缀 `/api/trip`（`main.py:49`）。`MAX_ATTRACTIONS = 40`(37)，`_guard()`(40) 超限直接 `HTTPException(400)`。

| # | 方法 + 路径 | 行号 | 处理函数 | 出参 | 调用的 service |
|---|---|---|---|---|---|
| 1 | `GET /cities` | 48 | `get_cities` | `list[CityOut]` | `list_cities` |
| 2 | `GET /attractions?city=` | 53 | `get_attractions` | `list[AttractionOut]` | `list_attractions` |
| 3 | `POST /preview` | 61 | `post_preview` | `PlanPreview` | `preview_plan` |
| 4 | `POST /plan` | 68 | `post_plan` | `PlanResponse` | `generate_plan` |
| 5 | `POST /plan/{plan_id}/adjust` | 85 | `post_adjust` | `PlanResponse` | `adjust_plan` |
| 6 | `GET /plans?limit=` | 110 | `get_plans` | `list[PlanBrief]` | `list_plans` |
| 7 | `GET /plan/{plan_id}` | 118 | `get_plan` | `PlanResponse` | `get_plan` |

错误处理：`post_plan`(71–82) 的 `HTTPException` 直抛、`llm.LLMError` → **502**、其它 → **500**（`detail` 带异常类型）；`post_adjust`(99–107) 同理。`get_cities` / `get_attractions` 无 try/except，靠 `main.py:77` 的全局处理器兜 500。

> `routers.py:11-12` 有一条关于 `/plan/preview` 必须早于 `/plan/{plan_id}` 的顺序警告——**该路径不存在**（preview 是 `POST /preview`），是历史遗留注释。

---

### `service.py`（481 行）— 业务编排

| 函数 | 行号 | 说明 |
|---|---|---|
| `resolve_city(db, key)` | 47 | 先按 `name` 再按 `pinyin.lower()` 查；404 时 `detail` 里列出已开放城市 |
| `list_cities(db)` | 65 | `order_by(City.heat.desc())`，用 `len(c.attractions)` 填 `attraction_count` |
| `list_attractions(db, key)` | 73 | `order_by(heat.desc(), visit_minutes.desc())` |
| `_load_selected(db, city, ids)` | 83 | 按 `city_id + id.in_(ids)` 查；空则 400；返回按 `-heat` 排序 |
| **`preview_plan(db, req)`** | 96 | 紧凑度预估（见下） |
| `audit_plan(plan, req)` | 198 | 逐天 `tools.audit_day` + 跨天重复检测，**只打 warning 日志**，不阻断 |
| `_store(db, req, result, source, review, trace, title)` | 216 | 落库，返回 `TripPlan`（其 `.id` 即 `plan_id`） |
| **`generate_plan(db, req)`** | 239 | 主链路（见下） |
| **`adjust_plan(db, plan_id, patch)`** | 285 | 倾向微调（见下） |
| `_unwrap(raw)` | 420 | 兼容 `{"plan","review","trace"}` 包装与老裸 `PlanResult` |
| `_to_response(record, attractions, review, trace)` | 427 | 组装 `PlanResponse`；`created_at` 格式 `%Y-%m-%d %H:%M:%S` |
| **`get_plan(db, plan_id)`** | 450 | `db.get(TripPlan, plan_id)`，缺失 404；从 `request_json["attraction_ids"]` 反查景点 |
| `list_plans(db, limit=20)` | 462 | `order_by(id.desc())`，limit 夹在 1–100 |

#### `generate_plan` 十步（239–281）

```python
resolve_city → _load_selected
baseline = planner.plan_fallback(...)          # 244  永远先算硬编码基线
source, trace, review, result = "fallback", [], None, baseline
if llm.is_available():                         # 253
    result, trace = llm.run_pipeline(...)      # 失败抛 LLMError
    source = "llm"
else:
    fallback_note = "（未配置 DeepSeek Key，由本地规则引擎生成）"
# except LLMError → result = baseline，source 保持 fallback
if source == "fallback": result.summary += note        # 264  降级留痕
if source == "llm":      review = llm.review_plan(...)  # 267  终检润色 summary
audit_plan(result, req)                        # 272  运行时复核（只打日志）
title = f"{city.name} · {req.days} 天 {len(attractions)} 景"
record = _store(...)                           # 275
```

#### `preview_plan` 紧凑度（96–194）

```python
per_day_budget = min(planner.day_budget(req, i, days) for i in range(1, days+1))   # 105
capacity       = per_day_budget * days                                            # 106  ⚠️ 不乘 CAPACITY_SLACK
kept, will_drop = planner.prune_to_capacity(...)                                  # 107
groups          = planner.assign_days(city, kept, days, req.transport)            # 108

load       = (visit_total + travel_total) / max(1, capacity)                      # 143
drop_ratio = len(will_drop) / max(1, len(attractions))                            # 144
```

`travel_total` 含**每天末站返回市中心的那一段**（127–129）。

**tightness 四档（146–153，严格 if/elif 顺序）**：

| 档位 | 条件 |
|---|---|
| `超载` | `drop_ratio > 0.25` 或 `load > 1.05` |
| `紧凑` | `drop_ratio > 0.08` 或 `load > 0.88` |
| `适中` | `load > 0.6` |
| `轻松` | 其余 |

`will_drop` 来自两处：① `prune_to_capacity` 按**「(热度+必去加成30) ÷ (游览+2×单程)」由低到高**丢的（`planner.py:307-310`）；② 各天 `trim_day` 溢出的（原因写「当天时间装不下」）。
`suggestion`(159–177) 按档位给建议（超载时建议加到 `min(7, days+1)` 天）。

> ⚠️ 预估口径与裁剪口径不同：`preview` 的 `capacity` **不含** `CAPACITY_SLACK=1.10`，而 `prune_to_capacity` 内部乘了。所以预估显示的容量比实际裁剪更紧 10%。

#### `adjust_plan`（285–416）— 秒级微调

| 步骤 | 行号 | 做什么 |
|---|---|---|
| 1 | 293–300 | 取记录、`_unwrap` 出旧 `PlanResult`、反序列化旧 `PlanRequest` |
| 2 | 307–315 | **复用原分天**：从旧 `day_plans[:patch.days]` 提取景点 id 顺序；天数不够补空组 |
| 3 | 319–339 | `pace == "packed"` 时把旧 `dropped` 里的景点**加回**：对每天试算 `trim_day`，选余量（`slack`）最大的一天放回 |
| 4 | 342–351 | 沿用旧 `advice` 文案与旧餐饮名 |
| 5 | 353 | `planner.plan_hotels(city, groups)` |
| 6 | 356–388 | 逐天 `trim_day` → 找旧主题（按景点 id 交集）→ `materialize_day` |
| 7 | 390–396 | 拼 `note`（restored / relaxed）与 `summary` |
| 8 | 410 | `_store(..., source="tweak", ...)` —— **生成新记录，不改原记录** |

---

### `planner.py`（971 行）★ 最核心

**地理工具 + 规则规划器**。无 Key 时独立出完整方案；有 Key 时为 LLM 提供距离/时间能力，也是提示词规则的**参考实现**。

#### 常量表

**交通方式**（`Mode` frozen dataclass，50–56：`key, label, speed_kmh, overhead_min, max_km`）

| 常量 | 行号 | label | 速度 km/h | 固定开销 min | 距离上限 km |
|---|---|---|---|---|---|
| `WALK` | 59 | 步行 | 4.5 | 0 | 1.5 |
| `METRO` | 60 | 地铁/公交 | 22.0 | 9 | 40 |
| `TAXI` | 61 | 打车 | 26.0 | 5 | 40 |
| `DRIVE_CITY` | 62 | 自驾 | 30.0 | 8 | 30 |
| `DRIVE_HWY` | 63 | 自驾·高速 | 62.0 | 8 | 80 |
| `DRIVE_FAR` | 64 | 自驾·高速 | 85.0 | 8 | ∞ |
| `CARPOOL` | 65 | 顺风车 | 50.0 | 14 | 90 |
| `COACH` | 66 | 城际大巴 | 42.0 | 22 | 150 |
| `RAIL` | 67 | 城际铁路 | 130.0 | 40 | ∞ |

**几何与时间阈值**

| 常量 | 行号 | 值 | 含义 |
|---|---|---|---|
| `CITY_RADIUS_KM` | 69 | 20.0 | 距市中心 > 20km 算「城区外」 |
| `URBAN_LEG_KM` | 70 | 30.0 | 城区腿上限 |
| `SUBURB_LEG_KM` | 71 | 80.0 | 近郊腿上限 |
| `SHORT_TAXI_KM` | 72 | 6.0 | 短于 6km 直接打车 |
| `MEAL_LUNCH_MINUTES` / `MEAL_DINNER_MINUTES` | 74–75 | 60 / 75 | 用餐时长 |
| `LUNCH_FROM` / `LUNCH_AFTER` / `LUNCH_MAX_WAIT` | 76–78 | `11:20` / `11:40` / 75 | 午饭锚点 / 吃完即走阈值 / 最大等待 |
| `DINNER_FROM` / `DINNER_TO_HOTEL` | 79–80 | `18:00` / 10 | 晚饭锚点 / 饭后走回酒店 |
| `STANDALONE_MINUTES` / `STANDALONE_TRAVEL_MIN` | 81–82 | 240 / 45 | 独占型景点判定（≥4h 或单程 ≥45min） |
| `CAPACITY_SLACK` | 83 | 1.10 | 容量裁剪松弛 |
| `OVERRUN_TOLERANCE` | 84 | 30 | 超目标返回时间多少分钟算跑长 |
| `OVERNIGHT_KM` | 85 | 45.0 | 当天景点离常住片区超此值 → 建议就近过夜 |
| `PACE_FACTOR` | 87 | `{relaxed:0.82, balanced:1.0, packed:1.15}` | 倾向系数 |
| `_MORNING_KEYS` | 240 | 熊猫/动物园/植物园/繁育研究基地 | 早场关键词 |
| `_NIGHT_KEYS` | 241 | 洪崖洞/不夜城/九眼桥/夜景/夜游/酒吧/灯光秀/兰桂坊 | 夜景关键词 |
| `_MUSEUM_KEYS` | 242 | 博物馆/博物院/纪念馆/美术馆 | 博物馆关键词 |

#### 交通模型 `leg()`(117–164)

```python
km = haversine_m(...) / 1000
if km <= 1.5:  return WALK                      # 任何 transport 都步行
from_out / to_out = 端点距市中心 > 20km
both_outside = from_out and to_out
urban = km <= 30 and not both_outside
```

| transport | 条件 | 选用 | 速度 | 开销 |
|---|---|---|---|---|
| `drive` | `urban` | 自驾 | 30 | 8 |
| `drive` | 非 urban 且 ≤80km | 自驾·高速 | 62 | 8 |
| `drive` | >80km | 自驾·高速 | 85 | 8 |
| `mixed` | `urban` 且 ≤6km | **打车** | 26 | 5 |
| `mixed` | `urban`（6~30km） | 地铁/公交 | 22 | 9 |
| `mixed` | 非 urban 且 ≤80km | **顺风车** | 50 | 14 |
| `mixed` | >80km | 城际铁路 | 130 | 40 |
| `transit` | `urban` | 地铁/公交 | 22 | 9 |
| `transit` | 非 urban 且 ≤80km | 城际大巴 | 42 | 22 |
| `transit` | >80km | 城际铁路 | 130 | 40 |

耗时公式（`_mk_leg` 110–114）：

```python
minutes = km / speed * 60 + overhead_min
minutes = max(5.0, minutes)                     # 下限 5 分钟
Leg.minutes = int(round(minutes / 5) * 5)       # 对齐到 5 分钟
```

**「近郊」这一档是关键**：都江堰↔青城山 11.9km，两端都在城区外——地铁不通、打车不划算，真实选择是顺风车（30min）或城际大巴（40min）。只看距离会误判成「打车 35 分钟」，只看是否跨城又会误判成「高铁」。

#### 分天五步

| 步骤 | 函数 | 行号 | 算法 |
|---|---|---|---|
| ① 容量裁剪 | `prune_to_capacity` | 329 | `capacity = per_day_budget × days × 1.10`；循环丢 `min(_value)` 者（`_value = (heat + 30 if must_visit) / (visit + 2×travel)`）；**条件是 `len(kept) > 1`，永远不裁到 0** |
| ② 独占隔离 | `assign_days` 内部 | 417–424 | `_is_standalone`（≥4h 或单程 ≥45min）各占一天；独占数超天数时多余的回归普通池 |
| ③ 成链均分 | `_nn_chain`(358) + `_split_balanced`(371)，由 `assign_days`(406) 编排 | 406 | 最近邻贪心成链 → 按游览总时长均分；有「翻转」优化（442–450：若反向更短则反转当天链） |
| ④ 逐天裁剪 | `trim_day` | 457 | 按当天真实预算（含末站回市中心的返程）再裁；**条件带 `kept` 短路，第一个景点无条件保留**（保证每天不空）；溢出顺延次日 |
| ⑤ 时段排序 | `apply_time_rules`(273)，`assign_days` 末行调用(453) | 273 | 早场提前、夜景推后（稳定排序） |

编排入口 `plan_fallback`(882)：`per_day_budget = min(day_budget)` → `prune_to_capacity` → `assign_days` → `plan_hotels` → 逐天 `trim_day` + `build_day`，溢出顺延，最后一天溢出进 `dropped`。

#### 时段硬知识 `time_rule()`(253)

| 命中关键词 | kind | `latest_start` | `earliest_start` | 理由 |
|---|---|---|---|---|
| 熊猫/动物园/植物园/繁育研究基地 | `morning` | `12:00` | — | 看动物要赶早，午后多在半睡 |
| 洪崖洞/不夜城/夜景… | `night` | — | `15:00` | 夜景要等亮灯 |
| 博物馆/博物院/纪念馆/美术馆 | `museum` | `15:30` | — | 一般 17:00 闭馆 |

匹配文本 = `name + " " + " ".join(tags)`；**顺序 morning → night → museum，首个命中即返回**。
`rule_violations()`(278) 用字符串比较校验已物化节点。

#### 住宿 `plan_hotels()`(522)

**一次定全程，不每天换**（频繁搬行李是自助游最烦的事）。取「全程景点的几何中心」最近的片区（`pick_hotel` 484）；某天景点离它 > `OVERNIGHT_KM=45` 才建议就近过夜，且**换宿条件是 `local.area != base.area and local_km < avg_km × 0.6`**（556 行的 0.6）。

#### `materialize_day()`(625–819) — 全项目唯一产出时间的地方

```python
def push(node):
    nodes.append(node)
    cur_time = parse_hhmm(node.time) + node.stay_minutes     # ← 唯一的时间推进点
```

流程：取 `day_window` → 推 `depart` → 逐景点循环：

| 逻辑 | 行号 | 条件 |
|---|---|---|
| 先吃午饭 | 722–730 | `arrive + visit > 14:00` 且 `11:20 - arrive ≤ 75` → 先吃，之后进景区 `travel` 重置为 **5 分钟**（只需步行进景区，不重算城际路程） |
| 出来立刻吃 | 753–755 | `cur_time >= 11:40` |
| 午饭兜底 | 757–759 | `max(cur_time, "12:00")` |
| 晚餐锚点 | 761–782 | 先算回住宿片区的 `back_leg`，`dinner_at = max(arrive_back, "18:00")` |
| 留白节点 | 768 | 若 `dinner_at - arrive_back > 90` → 插 `type="transit"` 的「返回住宿片区休整 · 自由活动」，避免时间轴凭空缺几小时 |
| 超时提示 | 787–791 | `overrun > 30` 时在 advice 追加说明 |
| 丰富度 | 804–811 | `active = Σstay + Σtravel`；`≤300 → 轻松`，`≤450 → 适中`，否则 `紧凑` |

插餐饮时传 `gap`（从上一站到餐厅的耗时）；不传则按「目标时刻 − cur_time」反推。**所以即使为凑饭点等了 60 分钟，时间轴依然自洽。**

`empty_day()`(828)：无景点的机动日，pace 固定「轻松」。
`build_day()`(822)：`materialize_day` 的薄封装（兼容旧名）。

---

### `llm.py`（419 行）★ 流水线

| 符号 | 行号 | 说明 |
|---|---|---|
| `MAX_WORKERS` | 71 | **4**（运行时 `max(1, min(4, days))`，306 行） |
| `DAY_RETRY` | 72 | **1** → `_gen_day` 循环共 **2 次**尝试（208） |
| `LLMError` | 75 | 模型不可用或流水线产不出方案 |
| `DAY_SYSTEM` | 80–100 | 阶段②系统提示词（**含 1 个完整 JSON few-shot**，99–100） |
| `REVIEW_SYSTEM` | 103–120 | 终检提示词（**无示例**） |
| `_day_user_prompt` | 123–150 | 拼单天 user 提示词 |
| `_review_user_prompt` | 153–174 | 把时间轴文本化给终检 |
| `chat_json` | 178 | 轻量 JSON 调用（seed 用），默认 `0.8 / 4096` |
| `is_available` | 193 | `settings.has_llm` |
| `_gen_day` | 198 | 单天生成 + 重试 |
| `_apply_day_content` | 236 | 把模型产出**只覆盖文案**，不碰时间 |
| **`run_pipeline`** | 271–380 | 主入口 |
| `_compose_summary` | 383 | 规则兜底总述（终检会覆盖） |
| **`review_plan`** | 398 | 阶段④终检 |

#### 四阶段（`run_pipeline`）

| 阶段 | 行号 | 做什么 | trace 记录 |
|---|---|---|---|
| ① 分天 | 283–287 | `prune_to_capacity` → `assign_days` → `plan_hotels` | `plan_days days=N kept=X dropped=Y` |
| 逐天裁剪 | 290–301 | `trim_day`，溢出以固定原因进 `dropped` | — |
| ② 并行写内容 | 304–325 | `ThreadPoolExecutor(max_workers=...)` + `pool.submit(_gen_day, ...)` + `as_completed` | `gen_day day=N ok` / `gen_day day=N ✗` |
| ③ 物化 | 328–356 | 先 `materialize_day` 出时间轴，有 LLM 产出则 `_apply_day_content` **再物化一次**带上文案 | — |
| ④ 收尾 | 358–379 | `must_reasons` 取热度前 3；`_compose_summary` | `materialize days=N elapsed=Xs` |

**单天失败不拖垮整份方案**（319–322 回退该天的规则文案）；**只有所有天都失败**才 `raise LLMError`(324)。

#### 调用参数（硬编码，不走 `settings`）

| 调用点 | temperature | max_tokens | response_format |
|---|---|---|---|
| `_gen_day`(213–215) | 0.6 | 2000 | `{"type":"json_object"}` |
| `review_plan`(408–410) | 0.3 | 1200 | 走 `with_structured_output`（内部强制 json_object） |
| `chat_json`(178) | 0.8 | 4096 | `{"type":"json_object"}` |

#### 降级的三层条件

1. 无 Key：`service.py:253` 的 `is_available()` 为假 → 直接用基线；
2. 全部天失败：`run_pipeline` 324 行 `raise LLMError`；
3. 终检失败：`review_plan` 返回 `None`（**不重试**，415–419），不影响主方案。

`service.py:257` 捕获 `LLMError` → `result = baseline` + 写入降级说明。**降级不静默。**

---

### `tools.py`（749 行）— function calling 工具集 ⚠️

> **当前无任何业务调用方**（`run_tools` / `TOOL_SPECS` / `dispatch` / `is_finalized` 全仓库零引用）。
> 它是旧「LLM 自己编排」方案的遗留 + 预留能力。`llm.py:35` 的注释说「用 `LLM_MODE=tools` 打开」——**该开关从未实现**，`config.py` 里没有这个字段。
> **唯一仍在使用的部分**：`audit_day()`（被 `service.audit_plan` 调用）。

#### 工具清单（**10 个**，不是 11 个）

`_DISPATCH`(589–600) 与 `TOOL_SPECS`(638–748) 完全一致：

| # | 工具 | 实现行号 | 作用 |
|---|---|---|---|
| 1 | `get_city_info` | 87 | 城市概况 + 候选住宿片区 + 每天预算 |
| 2 | `list_attractions` | 108 | 景点池，可按 district/min_heat/limit 筛 |
| 3 | `route_time` | 145 | 两景点间通行时间（走 `planner.leg`） |
| 4 | `plan_days` | 163 | 硬编码分天骨架 + 容量评估 |
| 5 | `suggest_hotel` | 214 | 按几何中心推荐片区 |
| 6 | `note_attraction` | 228 | 给景点写建议 |
| 7 | `build_day` | 240 | **核心**：物化某天时间轴 |
| 8 | `check_day` | 372 | 复核某天 |
| 9 | `drop_attraction` | 381 | 明确舍弃并写原因 |
| 10 | `finalize_plan` | 393 | 收尾提交 + 补齐 + 复核 |

#### `PlanSession`(47–67)

设计要点：工具**改这个对象**推进方案，而不是把大 JSON 塞回模型上下文，所以多轮编排的 token 开销是常数级。

| 字段 | 行号 | 说明 |
|---|---|---|
| `city` / `req` | 55–56 | 上下文 |
| `attractions` | 57 | `dict[int, Attraction]` 勾选池 |
| `per_day_budget` | 58 | 每天可用分钟 |
| `baseline` | 59 | 硬编码基线（永远先算好） |
| `built` | 60 | `dict[int, DayPlan]` 已物化的天 |
| `notes` / `dropped` / `must_reasons` / `summary` | 61–64 | LLM 产出的文案 |
| `result` / `finalized` | 65–66 | 收尾状态 |
| `warnings` | 67 | 累积警告 |

方法：`name_of`(70) / `assigned_ids`(74) / `dropped_ids`(82)。

#### `audit_day()`(525) — **仍在用**，4 个校验点

| # | 校验 | 行号 | 阈值 |
|---|---|---|---|
| 0 | 空节点早退 | 528 | `if not dp.nodes: return [...]` |
| 1 | 饭点数量与时段 | 531–537 | 按 `time < "15:00"` 切分；午餐恰好 1、晚餐恰好 1 |
| 2 | 出发时间一致 | 538–540 | vs `day_window(req, dp.day, req.days)[0]` |
| 3 | 时间自洽 | 542–548 | `parse(prev.time) + prev.stay + cur.travel == cur.time` |
| 4 | 收尾超时 | 550–553 | `end - target_end > 60` → issue |

#### `build_day` 内的三道拦截（返回 `ok:false`，当前不生效）

1. 收尾超时 `overrun > 30` 且当天 ≥2 景点 → 打回并**指名**去掉哪个（326–340）；
2. 违反时段规则（`planner.rule_violations` 非空）→ 打回（343–355）；
3. 跨天重复景点 → 自动剔除（256–270）。

---

### `amap.py`（201 行）— 高德客户端

| 符号 | 行号 | 状态 |
|---|---|---|
| `TYPE_RESTAURANT` / `TYPE_SCENIC` | 20–21 | `050000` / `110000` |
| `get_client()` | 26 | httpx 单例，`timeout=settings.amap_timeout` |
| **`search_poi(keyword, city, offset=5)`** | 45 | ✅ **在用**（`seed.py` 补坐标）。返回 `(lat, lng, district)` |
| `_query_text` | 67 | ⚙️ 内部。`/v3/place/text` + `citylimit=true` |
| `_strip_suffix` | 118 | ⚙️ 剥掉 14 个描述性后缀后重试一次 |
| `search_around` | 126 | ❌ **无调用方**（周边搜索） |
| `search_restaurants` | 191 | ❌ **无调用方**（就近找餐厅） |

`search_poi` 的匹配打分（95–102）：`overlap = len(set(poi.name) & set(keyword))`，取最大者；**若 `overlap < 2` 且双向都不是子串，判定相似度不足返回 None**。

> ⚠️ `amap_key` 是**明文 GET 参数**传递（72、146）。`search_around` / `search_restaurants` 属于历史上「运行时搜真实餐厅」的方案，现已废弃——现在餐饮**只给区域 + 本地特色小吃**，不写店名（店随时会换，写了误导）。

### `seed.py`（281 行）— 数据初始化

| 符号 | 行号 | 说明 |
|---|---|---|
| `SEED_FILE` | 37 | `app/trip/data/seed_attractions.json` |
| `TARGET_PER_CITY` | 38 | **26**（LLM 出名单时的目标条数；离线种子是 20） |
| `AMAP_SLEEP` | 39 | **0.25s**，节流 |
| `load_offline_seed` | 65 | 读 JSON |
| `llm_attraction_list` | 71 | 让 LLM 出名单（不含坐标） |
| `upsert_city` / `upsert_attractions` | 95 / 113 | UPSERT |
| `seed_city` | 168 | 单城流程 |
| `main` | 222 | CLI：`--city` / `--reset` / `--no-amap` / `--list` / `--only-cities` / `--source` |

**节流**：`time.sleep(AMAP_SLEEP)` 在**每次调用后无条件执行**（135），QPS 上限约 4 req/s，**串行逐条**，无并发。26 景 × N 城会线性累加时间。

**坐标来源标记**：命中高德 → `coord_source="amap"`；否则 `"seed"`。

**种子数据**：8 城（成都/北京/上海/西安/杭州/重庆/广州/南京）× 20 景 = **160 条**，均含 GCJ-02 坐标与 3–4 个候选住宿片区。

---

## 运行时数据流（一次 `POST /plan`）

```
routers.post_plan
  └─ _guard(payload)                        景点 ≤40
  └─ service.generate_plan
       ├─ resolve_city → _load_selected     查 trip_city / trip_attraction
       ├─ planner.plan_fallback             ← 基线（必算）
       │    ├─ prune_to_capacity            ① 容量裁剪
       │    ├─ assign_days                  ②③ 独占隔离 + 成链均分 + ⑤ 时段排序
       │    ├─ plan_hotels                  住宿片区
       │    └─ trim_day → materialize_day   ④ 逐天裁剪 + 物化
       ├─ llm.run_pipeline                  （有 Key 时覆盖基线）
       │    ├─ 复用上面的分天结果
       │    ├─ ThreadPoolExecutor × min(4, days)   每天一路写文案
       │    └─ materialize_day              再物化一次，带上 LLM 文案
       ├─ llm.review_plan                   终检 + 润色 summary
       ├─ audit_plan                        tools.audit_day 复核（只打日志）
       └─ _store → trip_plan                存 JSON 快照，返回 plan_id
```

---

## 代码与注释不一致的地方（写文档/改代码时以代码为准）

| # | 说法 | 实际情况 |
|---|---|---|
| 1 | `llm.py:35` / `README` 说「11 个工具，`LLM_MODE=tools` 打开」 | 实际 **10 个**工具；`LLM_MODE` **从未实现**，工具链**无调用方** |
| 2 | `trip/__init__.py:6` 说「routers.py 4 个 HTTP 端点」 | 实际 **7 条** |
| 3 | `routers.py:11-12` 警告 `/plan/preview` 顺序 | 该路径不存在（preview 是 `POST /preview`） |
| 4 | `models.py:82` 注释说 `coord_source` 可为 `manual` | 代码只写过 `amap` / `seed` |
| 5 | `planner.py:12-16` 模块文档说分天「四步」 | 实际 **五步**（第五步时段排序在 `assign_days` 末行） |
| 6 | `config.py` 的 6 个配置项 | 全部零引用（见 `config.py` 小节） |
| 7 | `schemas.ApiError` | 定义但无人使用 |
| 8 | `amap.py` 的 `search_around` / `search_restaurants` | 无调用方 |

**要不要清理？** 这些死代码/注释不影响运行，但会误导接手的人。建议的处理顺序见 [`DEVELOPMENT.md`](DEVELOPMENT.md#待办与建议)。
