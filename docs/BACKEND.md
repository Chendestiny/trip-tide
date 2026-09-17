# 后端地图

> 符号级导航。**行号会随改动漂移 —— 查找代码请以函数名为准**，顺手改这份文档。
>
> ✅ **行号基准：2026-09-15 校准**。同日的大改（分天模型 v2 / 弹性时长 / 一键 AI / `best_time` /
> `trip_spot` 子表 / LLM 分天审阅）已全部反映进本文件。
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

### `core/config.py`（122 行）

**唯一配置来源**。环境变量 > `backend/.env` > 代码默认值（`pydantic-settings`，`extra="ignore"`、大小写不敏感）。

| 符号 | 行号 | 说明 |
|---|---|---|
| `BACKEND_DIR` / `PROJECT_DIR` / `FRONTEND_DIR` / `DATA_DIR` | 18–22 | 路径常量 |
| `class Settings` | 25 | 配置模型，字段见下表 |
| `Settings.resolved_db_url` | 90 | property。`db_url` 为空 → 退化为 SQLite `backend/data/triptide.db` |
| `Settings.is_sqlite` | 98 | property |
| `Settings.has_llm` | 102 | `bool(model_api_key.strip())` |
| `Settings.has_amap` | 106 | `bool(amap_key.strip())` |
| `Settings.cors_origin_list` | 110 | 逗号切分；`"*"` 直接返回 `["*"]` |
| `get_settings()` | 118 | `@lru_cache(maxsize=1)` |
| `settings` | 122 | 模块级单例 |

关键字段（行号 → 字段 = 默认值）：

| 行号 | 字段 | 默认值 | 环境变量别名 |
|---|---|---|---|
| 33–35 | `app_name` / `app_version` / `debug` | `AI 旅行搭子 API` / `0.1.0` / `True` | — |
| 39 | `db_url` | `""`（→SQLite） | `DATABASE_URL` / `DB_URL` |
| 44 | `db_echo` | `False` | `DB_ECHO` |
| 48 | `model_api_key` | `""` | `MODEL_API_KEY` / `DEEPSEEK_API_KEY` |
| 52 | `model_base_url` | `https://api.deepseek.com/v1` | `MODEL_BASE_URL` / `DEEPSEEK_BASE_URL` |
| 56 | `model_name` | `deepseek-flash` | `MODEL_NAME` / `DEEPSEEK_MODEL` |
| 62–64 | `private_llm_api_key` / `_base_url` / `_model` | `""` | `PRIVATE_LLM_*` |
| 66–69 | `llm_timeout` / `llm_max_retry` / `llm_temperature` / `llm_max_tokens` | `180.0` / `1` / `0.7` / `8192` | 同名 |
| 72–74 | `amap_key` / `amap_base_url` / `amap_timeout` | `""` / `https://restapi.amap.com` / `15.0` | `AMAP_KEY` 等 |
| 77 | `cors_origins` | `http://localhost:5176,http://127.0.0.1:5176` | `CORS_ORIGINS` |
| 80–82 | `default_start_time` / `default_return_time` / `max_days` | `09:00` / `19:30` / `7` | — |
| 86 | `cities_cache_ttl` | `300.0` | `CITIES_CACHE_TTL`。首页 `/cities` 结果的进程内缓存秒数，**0 = 关闭缓存** |

> ⚠️ **死配置**：`llm_max_retry`(67)、`llm_temperature`(68)、`llm_max_tokens`(69)、`default_start_time`(80)、`default_return_time`(81)、`max_days`(82) 在 `app/` 内**零引用**。实际温度/上限硬编码在各调用点（`llm.py:239` 用 `0.6/2000`；`llm.py:620` 用 `0.3/1200`；`llm.py:186` 默认 `0.8/8192`）；时间与天数默认值写死在 `schemas.py`。改这些配置项**不会生效**。

### `core/db.py`（131 行）

| 符号 | 行号 | 说明 |
|---|---|---|
| `class Base(DeclarativeBase)` | 25 | ORM 基类 |
| `_engine_kwargs()` | 30 | SQLite → `check_same_thread=False`；MySQL → `pool_pre_ping` + `pool_recycle=3600` + `pool_size=5` + `max_overflow=10` |
| `engine` | 37 | 全局引擎（模块级创建） |
| `SessionLocal` | 44 | `sessionmaker(autoflush=False, expire_on_commit=False, future=True)` |
| `ensure_database()` | 47 | MySQL 下查 `information_schema.SCHEMATA`，不存在则 `CREATE DATABASE ... utf8mb4_unicode_ci` |
| `_ADDED_COLUMNS` | 85–98 | **轻量迁移**：只处理加列。`trip_city.rank_score`(94) 就是这么补进老库的 —— 补列后值全 0，**必须跑 `scripts/rank_cities.py` 重算** |
| `init_db()` | 115 | 建库 + `from app.trip import models`（**必须保留这行**，否则 `create_all` 看不到表）+ `create_all` + `_ensure_columns()` |
| `get_db()` | 126 | FastAPI 依赖，`yield` 后 `close()` |

### `core/__init__.py`（83 行）

业务代码的**唯一入口**，用 PEP 562 惰性导入打破 `config ←→ _llm` 的循环依赖。

```python
from app.core import llm, settings, SessionLocal, get_db   # ✅ 这样用
from app.core._llm._native import _native_chat             # ❌ 禁止
```

`__all__`(16–33) 共 17 个名字；`_LAZY`(51–68) 是 `名字 → (模块, 属性)` 映射表；`__getattr__`(71) 惰性 import 后写入 `globals()` 缓存。

### `core/_llm/` LLM 私有包

| 文件 | 行数 | 职责 |
|---|---|---|
| `__init__.py` | 214 | `LlmClient` + `llm` 单例；三种调用形态 |
| `_native.py` | 284 | OpenAI SDK 直调 + httpx 裸协议兜底 |
| `_registry.py` | 38 | 模型别名 → 真实模型名 |
| `_schemas.py` | 64 | `LLMResponse` / `ToolCall` / `LLMFormatError` |

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

`dispatch_chat(plugin, **kwargs)`(275)：`plugin="native"` 且未装 `openai` SDK 时自动降级到 `_httpx_chat()`(196) 裸 HTTP 实现。本机 venv 已装 `openai`，运行时走 SDK 分支。

关键实现细节：

- `deployment != "private"` 且 `enable_thinking` 为假时注入 `extra_body={"thinking": {"type": "disabled"}}`（89–90）—— **这是关掉模型思考的唯一有效写法**。原先写的 `enable_thinking: False` 是 DashScope（通义）风格，**DeepSeek 官方端点不认**（实测传了照样思考：reasoning 1721 字 / content 0 字）。不关思考的后果见 `llm.py` 小节的 `max_tokens` 警告。
- httpx 分支需把 `extra_body` **平铺到顶层**（220–221）。
- 异常统一转中文：限流 / 超时 / `API失败 (HTTP xxx)` / 空 choices → `"模型返回空结果"`（163）。
- `_get_openai_client`(47) **未传 `max_retries`**，重试次数沿用 SDK 默认值。

---

## `trip/` 业务层

### `models.py`（170 行）— 4 张表

四张表全部 `trip_` 前缀，与同库的 my-website 表互不干扰。**经纬度全库统一 GCJ-02。**

#### `trip_city` → `City`(35)

| 字段 | 行号 | 类型 / 约束 |
|---|---|---|
| `id` | 40 | Integer PK autoincrement |
| `name` | 41 | String(32) **unique** not null |
| `pinyin` | 42 | String(32) |
| `emoji` / `tagline` | 43–44 | String(8) / String(64) |
| `heat` | 45 | Integer，**仅作排序并列时的兜底**（首页真正用的是 `rank_score`） |
| `rank_score` | 46–52 | Integer default 0，**首页排序分**＝「前 8 个高热度景点」的指数加权和（λ=0.9、**第 1 名 ×1.5、第 2/3 名 ×1.2**）。**预存、派生值** —— 口径在 `service.RANK_*`；`recompute_rank_scores()` / `scripts/rank_cities.py` 写，seed 自动跑。⚠️ 改了景点忘重算 → 首页顺序陈旧且不报错，`scripts/check_rank.py` 负责抓 |
| `kind` | 53–56 | String(10) default `"city"`：`city`（城市+周边）/ `region`（区域游环线）。⚠️ **判定标准是「住宿基准」，不是行政区划** |
| `region` | 57–59 | String(16) 所属大区（西南/华东/华南/华中/华北/西北），首页搜索按它过滤 |
| `center_lat` / `center_lng` | 60–61 | Float，市中心 GCJ-02 |
| `hotel_areas` | 62–66 | **JSON**，元素 `{area, lat, lng, pros, cons}` |
| `created_at` | 67 | DateTime server_default now() |
| `attractions` | 69 | relationship，`cascade="all, delete-orphan"` |

#### `trip_attraction` → `Attraction`(74)

`__table_args__`(78–81)：`UniqueConstraint("city_id","name")` + `Index("ix_attraction_city_heat", "city_id", "heat")`

| 字段 | 行号 | 类型 / 约束 |
|---|---|---|
| `id` | 83 | Integer PK |
| `city_id` | 84 | FK `trip_city.id` **ondelete=CASCADE** |
| `name` | 87 | String(64) not null |
| `intro` | 88 | String(255) |
| `district` | 89 | String(32) |
| `lat` / `lng` | 90–91 | Float **not null** |
| `visit_minutes` | 92 | Integer default 90 |
| `heat` | 93 | Integer default 50，**全国统一 T 级 0~1000**（不是城市内相对值） |
| `must_visit` | 94 | Boolean default False |
| `best_time` | 95–100 | String(12)：`morning` / `night` / `museum` / 空。`planner.time_rule()` 优先读它，换城市不用改代码 |
| `tags` | 101 | **JSON** 数组 |
| `coord_source` | 102 | String(16)：`amap` / `seed`（注释里的 `manual` **代码从未写入**） |
| `created_at` | 105 | DateTime |
| `city` / `spots` | 107 / 108 | relationship；`spots` 带 `order_by="Spot.order_index"` |

#### `trip_spot` → `Spot`(115) — 景点内部的子景点

存在的意义：**把「内部路线攻略」这类 2~3 年不变的知识预存下来**，规划时直接读，不必每次让 LLM 现写。

`__table_args__`(125–128)：`UniqueConstraint("attraction_id","name")` + `Index("ix_spot_attraction_order", "attraction_id", "order_index")`

| 字段 | 行号 | 类型 / 约束 |
|---|---|---|
| `id` | 130 | Integer PK |
| `attraction_id` | 131 | FK `trip_attraction.id` **ondelete=CASCADE** |
| `name` | 134 | String(64) not null |
| `lat` / `lng` | 135 / 138 | Float **可空** —— 高德对小众子景点常搜不到，这时只保留攻略文字 |
| `guide` | 139 | Text，**怎么写**（哪个门进 / 几点人少 / 有什么坑）而不是「是什么」 |
| `order_index` | 140 | Integer，建议游览顺序，从 1 开始 |
| `stay_minutes` | 143 | Integer，建议停留分钟；`0` = 未标注 |
| `created_at` | 146 | DateTime |
| `attraction` | 148 | relationship back_populates |

#### `trip_plan` → `TripPlan`(151)

`__table_args__`(155)：`Index("ix_trip_plan_created", "created_at")`

| 字段 | 行号 | 说明 |
|---|---|---|
| `id` | 157 | PK，**就是对外暴露的 `plan_id`** |
| `city` / `days` / `attraction_count` | 158–160 | 冗余字段，便于列表展示 |
| `source` | 161 | String(16)：`llm` / `fallback` / `tweak`（注释只写了前两个，第三个是 `adjust_plan` 写的） |
| `title` | 164 | String(128) |
| `request_json` | 165 | **JSON**，`PlanRequest.model_dump()` |
| `result_json` | 166 | **JSON**，三元包装 `{"plan": {...}, "review": {...}\|null, "trace": [...]}` |
| `created_at` | 167 | DateTime |

> **`trip_plan` 无外键、不做关系化**：入参出参整体存 JSON 快照。理由见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。读取时由 `service._unwrap()`(651) 兼容老格式（裸 `PlanResult`）。
>
> ⚠️ **给 ORM 模型加字段，记得往 `core/db.py` 的 `_ADDED_COLUMNS` 加一行**（AGENTS.md 铁律 17）——
> 项目没引 alembic，靠那个轻量迁移在 `init_db()` 里自动 `ALTER TABLE`。

---

### `schemas.py`（385 行）— 双重身份

**既是对外 HTTP 契约，又是 LLM 输出的强校验 schema**（校验失败即触发重试）。

#### 枚举

| 符号 | 行号 | 取值 |
|---|---|---|
| `NodeType` | 16 | `depart` / `attraction` / `meal` / `hotel` / `transit` |
| `Pace` | 17 | `轻松` / `适中` / `紧凑`（前端展示用，中文） |
| `Transport` | 23 | `drive` / `mixed` / `transit` / `taxi`（taxi 为历史值） |
| `PacePref` | 25 | `relaxed` / `balanced` / `packed`（入参用，英文） |
| `TRANSPORT_LABELS` | 120 | `taxi` 与 `mixed` 都映射到「打车+公共」 |
| `PACE_LABELS` | 121 | 中英映射 |

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
`_norm_hhmm`(124) 兼容全角冒号并做范围校验。

**`AutoPlanRequest`(85)**：一键 AI 的入参（`/auto-plan`）。与 `PlanRequest` 的差别是**不要 `attraction_ids`**，
但 `days` / `pace` 变成**必填**。拆成两个模型是为了让「勾选生成」与「一键生成」的契约各自清楚。

#### LLM 输出契约

| 模型 | 行号 | 要点 |
|---|---|---|
| `TimelineNode` | 136 | `time`(141) / `type` / `name`(≤64) / `advice`(≤200) / `attraction_id` / `stay_minutes`(0–600) / `travel_minutes`(0–600) / `travel_mode`(≤16) / `must_visit` / `moved_to_day` / `moved_from_day` / `move_reason`(≤120) |
| `HotelAlternative` / `HotelAdvice` | 170 / 177 | `area`(≤64) / `reason`(≤300) / `alternatives: [{area, tradeoff}]` |
| `DayPlan` | 187 | `day`(1–7) / `theme`(≤80) / `pace`(默认适中，`_norm_pace`(196) 归一英文近义词) / `nodes` / `hotel` |
| `DroppedItem` | 210 | `name` / `reason`(≤200) / `attraction_id` |
| `MustVisitReason` | 220 | 同构；取勾选里热度最高的 3 个 |
| `PlanResult` | 228 | `city` / `days` / `summary`(≤300) / `day_plans`(`min_length=1`) / `must_visit_reasons` / `dropped`；`_check_days`(240) 强制 `day` 从 1 连续递增 |
| `ReviewIssue` | 252 | `day`(可空) / `level: info\|warn\|error` / `text`(≤200) |
| `PlanReview` | 260 | `ok` / `summary`(≤300) / `issues: [ReviewIssue]` |

> 所有这些模型都带 `model_config = ConfigDict(extra="ignore")`——**LLM 多输出的字段会被静默丢弃**，这是有意的容错设计。

#### 响应契约

| 模型 | 行号 | 要点 |
|---|---|---|
| `CityOut` | 272 | 含 `kind` / `region`（首页两 tab 按 `kind` 过滤）；`attraction_count` 由 service 现算，不是表字段 |
| `SpotOut` | 288 | 子景点出参：`lat`/`lng` **可为空**（高德搜不到时只留攻略文字） |
| `AttractionOut` | 302 | 前端画地图所需全部字段；含 `spot_count` 与 `distance_km`（前端 30/45km 分档标「周边 / 远郊」） |
| `PlanResponse` | 323 | `plan_id` / `created_at`(str) / `source: llm\|fallback\|tweak` / `title` / `request` / `result` / `attractions` / `review` / `trace` |
| `PlanBrief` | 343 | 历史列表轻量条目 |
| `PlanPreview` | 355 | 紧凑度预估出参，字段见 [`API.md`](API.md)#3 |
| `ApiError` | 382 | **定义但无人使用**（`main.py` 异常处理器直接返回裸 dict） |

---

### `routers.py` — 9 条路由

挂载前缀 `/api/trip`（`main.py:49`）。`MAX_ATTRACTIONS = 40`(39)，`_guard()`(42) 超限直接 `HTTPException(400)`。

| # | 方法 + 路径 | 行号 | 处理函数 | 出参 | 调用的 service |
|---|---|---|---|---|---|
| 1 | `GET /cities` | 50 | `get_cities` | `list[CityOut]` | `list_cities` |
| 2 | `GET /attractions?city=` | 55 | `get_attractions` | `list[AttractionOut]` | `list_attractions` |
| 3 | `GET /attractions/{id}/spots` | 63 | `get_spots` | `list[SpotOut]` | `list_spots` |
| 4 | `POST /preview` | 77 | `post_preview` | `PlanPreview` | `preview_plan` |
| 5 | `POST /plan` | 84 | `post_plan` | `PlanResponse` | `generate_plan` |
| 6 | `POST /auto-plan` | 101 | `post_auto_plan` | `PlanResponse` | `generate_auto_plan` |
| 7 | `POST /plan/{plan_id}/adjust` | 128 | `post_adjust` | `PlanResponse` | `adjust_plan` |
| 8 | `GET /plans?limit=` | 153 | `get_plans` | `list[PlanBrief]` | `list_plans` |
| 9 | `GET /plan/{plan_id}` | 161 | `get_plan` | `PlanResponse` | `get_plan` |

错误处理：`post_plan`(84–98) 的 `HTTPException` 直抛、`llm.LLMError` → **502**、其它 → **500**
（`detail` 带异常类型）；`post_auto_plan`(101–125) 与 `post_adjust`(128–152) 同理。
`get_cities` / `get_attractions` / `get_spots` 无 try/except，靠 `main.py:76` 的全局处理器兜 500。

> `routers.py:11-12` 有一条关于 `/plan/preview` 必须早于 `/plan/{plan_id}` 的顺序警告——**该路径不存在**
> （preview 是 `POST /preview`），是历史遗留注释。

---

### `service.py`（711 行）— 业务编排

| 函数 | 行号 | 说明 |
|---|---|---|
| `resolve_city(db, key)` | 59 | 先按 `name` 再按 `pinyin.lower()` 查；404 时 `detail` 里列出已开放目的地 |
| `clear_cities_cache()` | 77 | 清空首页宫格缓存。`recompute_rank_scores()` 末尾会自动调 |
| `RANK_TOP / RANK_DECAY / RANK_FIRST_BOOST / RANK_TOP3_BOOST` | 114–117 | **首页排序口径常量**：前 8 个高热度景点的指数加权和，λ=0.9，**第 1 名 ×1.5、第 2/3 名 ×1.2**（更看重「有没有一张顶级名片」）。为什么（基数问题 + 实测 ρ 对照表）见 114 行上方的大段注释。⚠️ λ 故意不做配置项（0.85~0.95 对前 20 名单无影响，而本仓库已有 6 个零引用死配置） |
| `rank_score(heats)` | 120 | 单个目的地的排序分，**纯函数**（不碰库）。`recompute_rank_scores` 与 `scripts/rank_cities.py` 共用 |
| `recompute_rank_scores(db)` | 133 | 重算所有目的地的 `City.rank_score` 并落库，返回改动条数。**seed 完自动调**；手工改过景点热度后必须重跑（不跑不报错，只是首页顺序陈旧） |
| `list_cities(db)` | 156 | 首页宫格。**1 条 SQL**（`LEFT JOIN` 数景点数）+ 按**预存的** `rank_score` 排序 + 进程内缓存（`cities_cache_ttl`，默认 300s） |
| `list_attractions(db, key)` | 191 | `order_by(heat.desc(), visit_minutes.desc())`；顺带一条 `group by` 取子景点数、算 `distance_km`（前端 30/45km 分档标「周边 / 远郊」） |
| `list_spots(db, attraction_id)` | 220 | 某景点的子景点。**单独接口**，避免景点列表响应变肥 |
| `_all_of_city` / `_load_selected` | 232 / 243 | 一键 AI 的候选池 / 按 id 取景点并校验属于该城 |
| **`preview_plan(db, req)`** | 257–385 | 紧凑度预估（见下） |
| `audit_plan(plan, req)` | 386 | 逐天 `tools.audit_day` + **跨天**重复检测，**只打 warning 日志**，不阻断。⚠️ **同一天内出现两次不算重复**（景点跨午饭会被有意拆成「上午段 + 下午继续」） |
| `_store(...)` | 412 | 落库，返回 `TripPlan`（其 `.id` 即 `plan_id`） |
| **`generate_plan(db, req)`** | 435–477 | 主链路（见下） |
| **`generate_auto_plan(db, payload)`** | 480 | 一键 AI：`planner.pick_attractions` 自动挑景点 → 转成 `PlanRequest` → **复用 `generate_plan`**，所以产物结构与降级行为完全一致 |
| **`adjust_plan(db, plan_id, patch)`** | 514–650 | 倾向微调（见下） |
| `_unwrap(raw)` | 651 | 兼容 `{"plan","review","trace"}` 包装与老裸 `PlanResult` |
| `_to_response(...)` | 658 | 组装 `PlanResponse`；`created_at` 格式 `%Y-%m-%d %H:%M:%S` |
| **`get_plan(db, plan_id)`** | 681 | `db.get(TripPlan, plan_id)`，缺失 404；从 `request_json["attraction_ids"]` 反查景点 |
| `list_plans(db, limit=20)` | 693 | `order_by(id.desc())`，limit 夹在 1–100 |

#### `generate_plan`（351–393）

```python
resolve_city → _load_selected
baseline = planner.plan_fallback(...)          # 356  永远先算硬编码基线
source, trace, review, result = "fallback", [], None, baseline
if llm.is_available():                         # 365
    result, trace = llm.run_pipeline(...)      # 367  失败抛 LLMError
    source = "llm"
else:
    fallback_note = "（未配置 DeepSeek Key，由本地规则引擎生成）"
# except LLMError(369) → result = baseline，source 保持 fallback
if source == "fallback": result.summary += note        # 376  降级留痕
if source == "llm":      review = llm.review_plan(...)  # 380  终检润色 summary
audit_plan(result, req)                        # 384  运行时复核（只打日志）
record = _store(...)                           # 387
```

#### `preview_plan` 紧凑度（173–299）

判定**不看时间占用率**，而是「勾选景点的总游览时长摊到每天 vs 节奏目标」：

```python
kept, will_drop = planner.prune_to_capacity(...)                                   # 190
groups          = planner.assign_days(city, kept, days, req.transport, req.pace)   # 191

used, day_load = sum(游览), planner._group_load(...)   # 207–210  travel_total = Σ(day_load − 游览)
capacity   = sum(planner.day_budget(req, i, days) for i in range(1, days+1))       # 222  逐天预算求和
wanted     = sum(a.visit_minutes for a in attractions)   # 勾选总量，不是排入总量
avg_visit  = wanted / max(1, days)
target     = planner.PACE_TARGET_MINUTES[req.pace]       # 轻松 240 / 平衡 360 / 紧凑 450
ratio      = avg_visit / max(1, target)                                            # 234
```

**tightness 四档（236–250）**：

| 档位 | 条件 |
|---|---|
| `超载` | `ratio ≥ 1.5`，或 `drop_ratio > 0.25` |
| `紧凑` | `ratio ≥ 1.0`，或有景点被丢（`drop_ratio > 0` 时至少「紧凑」） |
| `适中` | `ratio ≥ 0.65` |
| `轻松` | 其余 |

`travel_total` 含**每天末站返回住宿片区的那一段**（`_group_load` 内部含返程）。

`will_drop` 来自两处：① `prune_to_capacity` 按**「(热度 + 必去加成) ÷ 耗时」由低到高**丢的；② 各天 `trim_day` 溢出的（原因写「当天时间装不下」）。
`suggestion`（258 起）按档位给建议，超载时提示加到 `min(7, days+1)` 天。

> ⚠️ **`capacity_minutes` 与 `load_ratio` 仍在响应里，但只作参考、不参与判定**。
> 它们对天数不敏感 —— `load` 的分子分母同随天数增长，加天数几乎不动，算不出「6 天只排了 4 天的量」。
> 判定依据只有 `ratio` 与 `drop_ratio`。完整推导见 [`API.md`](API.md#3-post-apitrippreview)。

#### `adjust_plan`（430–563）— 秒级微调

| 步骤 | 行号 | 做什么 |
|---|---|---|
| 1 | 438–450 | 取记录、`_unwrap` 出旧 `PlanResult`、反序列化旧 `PlanRequest` |
| 2 | 452–462 | **复用原分天**：从旧 `day_plans[:patch.days]` 提取景点 id 顺序；天数不够补空组 |
| 3 | 464–485 | `pace == "packed"` 时把旧 `dropped` 里的景点**加回**：对每天试算 `trim_day`，选余量（`slack`）最大的一天放回 |
| 4 | 487–496 | 沿用旧 `advice` 文案与旧餐饮名 |
| 5 | 498 | `planner.plan_hotels(city, groups)` |
| 6 | 500–527 | 逐天 `trim_day` → 找旧主题（按景点 id 交集）→ `materialize_day` |
| 7 | 529–535 | 拼 `note`（restored / relaxed）与 `summary` |
| 8 | 557 | `_store(..., source="tweak", ...)` —— **生成新记录，不改原记录** |

---

### `planner.py`（1581 行）★ 最核心

**地理工具 + 规则规划器**。无论有没有高德 Key 都能独立出完整方案；规划阶段的距离/时间能力来自本地 GCJ-02 坐标、haversine 与交通分档，也是提示词规则的**参考实现**。高德 Key 只在 seed 补坐标时参与，真实路径规划尚未接入生成链路。

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
| `DINNER_FROM` / `EARLIEST_DINNER` / `DINNER_TO_HOTEL` | 79–81 | `18:00` / `17:00` / 10 | 晚饭锚点 / 最早开饭 / 饭后走回酒店 |
| `MIN_STAY_MINUTES` | 82 | 20 | 为按时返回，单景点最少保留的停留 |
| `STANDALONE_MINUTES` / `STANDALONE_TRAVEL_MIN` | 83–84 | 240 / 45 | 独占型景点判定（≥4h 或单程 ≥45min） |
| `CAPACITY_SLACK` | 85 | 1.10 | 容量裁剪松弛 |
| `OVERRUN_TOLERANCE` | 86 | 30 | 超目标返回时间多少分钟算跑长 |
| `OVERNIGHT_KM` | 87 | 45.0 | 当天景点离常住片区超此值 → 建议就近过夜 |
| `OVERPACK_RATIO` | 88 | 1.5 | 单个景点耗时超当天预算这么多倍 → 放弃它、**留成机动日**（转场日常见）。见 `trim_day` |
| `GRADE_SMALL_MAX` / `GRADE_MEDIUM_MAX` | 95–96 | 90 / 180 | 景点分级阈值（≤90 小 / 91~180 中 / >180 大） |
| `PACE_TARGET_MINUTES` | 99 | `{relaxed:240, balanced:360, packed:450}` | **每天目标游玩时长**——倾向的差异只体现在这里和 `PACE_FLEX` |
| `PACE_FLEX` | 103 | 见「弹性游玩时长」 | 填充率 / 倍数上限 |
| `CLUSTER_MAX_METERS` | 110 | 1200 | **片区簇阈值**（直线 ≤1.2km，簇内不可拆） |
| `MUST_HEAT` | 365 | 500 | 「必去」的热度线（全国统一 T 级 0~1000） |
| `_MORNING_KEYS` | 284 | 熊猫/动物园/植物园/繁育研究基地 | 早场关键词 |
| `_NIGHT_KEYS` | 285 | 洪崖洞/不夜城/九眼桥/夜景/夜游/酒吧/灯光秀/兰桂坊 | 夜景关键词 |
| `_MUSEUM_KEYS` | 286 | 博物馆/博物院/纪念馆/美术馆 | 博物馆关键词 |

> ⚠️ **`PACE_FACTOR` 已删除**（曾为 `{relaxed:0.82, balanced:1.0, packed:1.15}`）。
> **时间预算不能乘任何倾向系数**——出发 / 返回时间是用户定的硬约束，物理时间不会因为选了「紧凑」就多出 15%。
> 曾经 420 分钟的时间窗被乘成 483，直接把「最后一天 16:00 返回」排成了 19:25（**超时 205 分钟**）。
> 见 [`../AGENTS.md`](../AGENTS.md) 铁律 15。

#### 交通模型 `leg()`(141–188)

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

耗时公式（`_mk_leg` 134–138）：

```python
minutes = km / speed * 60 + overhead_min
minutes = max(5.0, minutes)                     # 下限 5 分钟
Leg.minutes = int(round(minutes / 5) * 5)       # 对齐到 5 分钟
```

**「近郊」这一档是关键**：都江堰↔青城山 11.9km，两端都在城区外——地铁不通、打车不划算，真实选择是顺风车（30min）或城际大巴（40min）。只看距离会误判成「打车 35 分钟」，只看是否跨城又会误判成「高铁」。

#### 分天五步

| 步骤 | 函数 | 行号 | 算法 |
|---|---|---|---|
| ① 容量裁剪 | `prune_to_capacity` | 402 | `capacity = per_day_budget × days × 1.10`；循环丢 `min(_value)` 者（`_value = (heat + 30 if must_visit) / (visit + 2×travel)`）；**条件是 `len(kept) > 1`，永远不裁到 0** |
| ② 独占隔离 | `assign_days` 内部 | — | `_is_standalone`（≥4h 或单程 ≥45min）各占一天；独占数超天数时多余的回归普通池 |
| ③ **片区簇分配** | `cluster_attractions`(501) → `_split_cluster`(535) / `_group_load`(568) / `_alloc_clusters_to_days`(587)，由 `assign_days`(637) 编排 | 637 | **按直线距离 ≤1200m 聚类（`CLUSTER_MAX_METERS`），簇是分天的原子、不可拆**——这是「锦里古街↔武侯祠（0.24km）必然同日」的保证。簇太多就合并最近的两簇，太少只在「真断点」处拆。分配成本用 `_group_load` = 游览 + 路程（远郊景点游览 1h、往返却要 3h，只看游览时长会低估） |
| ④ 逐天裁剪 | `trim_day` | 756 | 按当天真实预算（含末站回住宿片区的返程）再裁；溢出顺延次日。⚠️ 「第一个景点无条件保留」**有个例外**：单个景点耗时 > `预算 × OVERPACK_RATIO`（1.5）时也放弃 —— 否则 628km 的转场日会排到凌晨 1:25（见 `CHANGELOG.md` 2026-09-17 第 2 节） |
| ⑤ 时段排序 | `apply_time_rules`(333)，`assign_days` 末行调用 | 333 | 早场提前、夜景推后（稳定排序） |

编排入口 `plan_fallback`(1478)：`per_day_budget = min(day_budget)` → `prune_to_capacity` → `assign_days` → `plan_hotels`(841) → 逐天 `trim_day` + `build_day`(1376)，溢出顺延，最后一天溢出进 `dropped`。

> ⚠️ **别用通行时间做聚类阈值**：速度模型有「1.5km 内一律步行」+「耗时取整到 5 分钟」两个噪音，
> 会让阈值在 8~15 分钟之间出现悬崖。见 [`../AGENTS.md`](../AGENTS.md) 铁律 16。
>
> `_nn_chain`(446) 仍在文件里（`pick_attractions` 用），但**已不在分天主链路**上。

#### 时段硬知识 `time_rule()`(305)

| 命中关键词 | kind | `latest_start` | `earliest_start` | 理由 |
|---|---|---|---|---|
| 熊猫/动物园/植物园/繁育研究基地 | `morning` | `12:00` | — | 看动物要赶早，午后多在半睡 |
| 洪崖洞/不夜城/夜景… | `night` | — | `15:00` | 夜景要等亮灯 |
| 博物馆/博物院/纪念馆/美术馆 | `museum` | `15:30` | — | 一般 17:00 闭馆 |

匹配文本 = `name + " " + " ".join(tags)`；**顺序 morning → night → museum，首个命中即返回**。
`rule_violations()`(338) 用字符串比较校验已物化节点。

#### 住宿 `plan_hotels()`(841)

**一次定全程，不每天换**（频繁搬行李是自助游最烦的事）。取「全程景点的几何中心」最近的片区（`pick_hotel` 791）；某天景点离它 > `OVERNIGHT_KM=45` 才建议就近过夜，且**换宿条件是 `local.area != base.area and local_km < avg_km × 0.6`**（881 行的 0.6）。

#### `spot_advice()`(1070) — 用预存的子景点攻略拼 advice（**硬编码，不花 token**）

**899 个景点里 603 个（67%）有子景点**（2026-09-17 `--topup` 补的 74 条**没有**子景点），所以这条路径覆盖绝大多数。拼接只做截断、不改写
（攻略是人工校对过的原文）。取值优先级：**子景点拼串 > LLM 写的 > 库里的 `intro`**。

#### `materialize_day()`(1098–1412) — 全项目唯一产出时间的地方

```python
def push(node):
    nodes.append(node)
    cur_time = parse_hhmm(node.time) + node.stay_minutes     # ← 唯一的时间推进点
```

流程：取 `day_window` → 推 `depart` → 逐景点循环：

| 逻辑 | 行号 | 条件 |
|---|---|---|
| 先吃午饭 | 1214–1228 | `arrive + visit > 14:00` 且 `11:20 - arrive ≤ 75` → 先吃，之后进景区 `travel` 重置为 **5 分钟**（只需步行进景区，不重算城际路程） |
| 出来立刻吃 | 1256–1257 | `cur_time >= 11:40` |
| 午饭兜底 | 1263 | `max(cur_time, "12:00")` |
| 晚餐锚点 | 1295–1332 | 先算回住宿片区的 `back_leg`，`dinner_at = max(arrive_back, "18:00")` |
| 留白节点 | 1318–1327 | 若 `dinner_at - arrive_back > 90` → 插 `type="transit"` 的「返回住宿片区休整 · 自由活动」，避免时间轴凭空缺几小时 |
| 超时提示 | 1337 | `overrun > 30` 时在 advice 追加说明 |
| 丰富度 | 1357 | `active = Σstay + Σtravel`；`≤300 → 轻松`，`≤450 → 适中`，否则 `紧凑` |

插餐饮时传 `gap`（从上一站到餐厅的耗时）；不传则按「目标时刻 − cur_time」反推。**所以即使为凑饭点等了 60 分钟，时间轴依然自洽。**

`empty_day()`(1424)：无景点的机动日，pace 固定「轻松」，**有午餐**。⚠️ `materialize_day([])` 只有晚餐、没有午餐 —— 所以机动日必须走 `empty_day`（`plan_fallback` 与 `llm.run_pipeline` 都已对齐）
`build_day()`(1418)：`materialize_day` 的薄封装（兼容旧名）。

---

### `llm.py`（636 行）★ 流水线

| 符号 | 行号 | 说明 |
|---|---|---|
| `MAX_WORKERS` | 71 | **4**（运行时 `max(1, min(4, days))`，347 行） |
| `DAY_RETRY` | 72 | **1** → `_gen_day` 循环共 **2 次**尝试（237） |
| `LLMError` | 75 | 模型不可用或流水线产不出方案 |
| `DAY_SYSTEM` | 80–102 | 阶段②系统提示词（**含 1 个完整 JSON few-shot**）。⚠️ 硬性要求 1 是「notes **只写标了「要 advice」的**景点」—— 有子景点的不用写，省 token |
| `REVIEW_SYSTEM` | 103–120 | 终检提示词（**无示例**） |
| `_day_user_prompt` | 124–155 | 拼单天 user 提示词。每个景点会标「已有内部攻略，不要写 advice」或「要 advice」(142) |
| `_review_user_prompt` | 153–174 | 把时间轴文本化给终检 |
| `MAX_TOKENS_CEILING` | 183 | **16384**，空返回自动加倍重试的上限 |
| `chat_json` | 186 | 轻量 JSON 调用（seed / 分天审阅用），默认 `0.8 / 8192`。**拿到空 content 会自动加倍重试一次** |
| `is_available` | 222 | `settings.has_llm` |
| `_gen_day` | 232 | 单天生成 + 重试 |
| `_apply_day_content` | 265 | 把模型产出**只覆盖文案**，不碰时间 |
| **`run_pipeline`** | 305–428 | 主入口 |
| `PLAN_DAYS_SYSTEM` | 427 | 阶段①.5 分天审阅提示词 |
| `_items_text` / `_matrix_text` | 450 / 467 | 喂给模型的景点清单 / **量化通行时间矩阵** |
| `_plan_days_prompt` | 485 | 拼审阅提示词（含系统当前分天，供参考） |
| `_validate_days` | 503 | 分天结果的 **5 道硬校验** |
| `plan_days_with_llm` | 543 | 阶段①.5 主函数（两次不过就返回 `None`，沿用硬编码分天） |
| `_compose_summary` | 595 | 规则兜底总述（终检会覆盖） |
| **`review_plan`** | 610 | 阶段④终检 |

#### 五阶段（`run_pipeline`，300–423）

| 阶段 | 行号 | 做什么 | trace 记录 |
|---|---|---|---|
| ① 分天 | 311–315 | `prune_to_capacity` → `assign_days` → `plan_hotels` | `plan_days days=N kept=X dropped=Y` |
| 逐天裁剪 | 317–332 | `trim_day`（传当天过夜点为 `origin`），溢出以固定原因进 `dropped` | — |
| ①.5 审阅分天 | 334–342 | `plan_days_with_llm`，通过则**重算 `plan_hotels`** | `review_days ok` / `review_days skipped` |
| ② 并行写内容 | 344–366 | `ThreadPoolExecutor(max_workers=...)` + `pool.submit(_gen_day, ...)` + `as_completed` | `gen_day day=N ok` / `gen_day day=N ✗` |
| ③ 物化 | 368–399 | 先 `materialize_day` 出时间轴，有 LLM 产出则 `_apply_day_content` **再物化一次**带上文案 | — |
| ④ 收尾 | 401–423 | `must_reasons` 取热度前 3；`_compose_summary` | `materialize days=N elapsed=Xs` |

**单天失败不拖垮整份方案**（② 的 except 分支回退该天的规则文案）；**只有所有天都失败**才 `raise LLMError`(366)。

#### 调用参数（硬编码，不走 `settings`）

| 调用点 | temperature | max_tokens | response_format |
|---|---|---|---|
| `_gen_day`(239–243) | 0.6 | 2000 | `{"type":"json_object"}` |
| `plan_days_with_llm`(569–570) | 0.3 | 2000 | 同上（经 `chat_json`） |
| `review_plan`(620–621) | 0.3 | 1200 | 走 `with_structured_output`（内部强制 json_object） |
| `chat_json`(186) | 0.8 | **8192** | `{"type":"json_object"}` |

> ⚠️ **`max_tokens` 别给太小。** `deepseek-flash` 是**混合思考模型**，思考内容也计入预算 ——
> 给 2000 会让「空 content」变成常态（`finish_reason=length`），而报错是「模型返回内容为空」，
> 完全指错方向。上面三处 1200~2000 的值是在**思考被正确关掉**的前提下才成立的。
> `chat_json` 额外做了兜底：拿到空 content 会**自动加倍重试一次**（上限 16384）。
> 详见 [`CHANGELOG.md`](CHANGELOG.md) 的 2026-09-16 第 3 节。

#### 降级的三层条件

1. 无 Key：`service.py:365` 的 `is_available()` 为假 → 直接用基线；
2. 全部天失败：`run_pipeline` 366 行 `raise LLMError`；
3. 终检失败：`review_plan` 返回 `None`（**不重试**，函数末尾的 except 分支），不影响主方案。

`service.py:369` 捕获 `LLMError` → `result = baseline` + 写入降级说明。**降级不静默。**

---

### `tools.py`（748 行）— function calling 工具集 ⚠️

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

### `amap.py` — 高德 Web 服务客户端

> **当前实际边界（2026-09-17）**：高德只参与「seed 补坐标」和前端地图渲染；**生成规划阶段不调用高德路径规划 API**。
> 规划中的路程/通行时间由 `planner.py` 本地按 GCJ-02 坐标、haversine 直线距离和交通方式分档估算。

| 符号 / 接口 | 状态 | 实际用途 |
|---|---|---|
| `get_client()` | ✅ 在用 | httpx 单例，使用 `settings.amap_timeout` |
| `search_poi(keyword, city, offset=5)` | ✅ 在用 | `seed_city` 全量重灌用的统一入口；**先调地理编码，失败后才调 POI 关键字搜索**。⚠️ **`--topup` 不走它**（topup 只调 `search_address`） |
| `search_address(address, city)` | ✅ 主路径 | `GET /v3/geocode/geo`，景点名/地址 → `(lat, lng, district)`；基础 LBS 配额 **15 万/月**。⚠️ POI 搜索（5 千/月）额度见底，**补坐标一律走这条**（铁律 23） |
| `_query_geo` | ⚙️ 内部 | 解析 `geocodes[].location`（高德返回 `lng,lat`），优先选 `level=兴趣点` |
| `_query_text` | ⚙️ 兜底 | `GET /v3/place/text` + `citylimit=true`；POI 关键字搜索配额 **5 千/月** |
| `_strip_suffix` | ⚙️ 内部 | 去掉「博物馆/景区/古镇」等描述性后缀后重试 |
| `search_around` | ⚠️ 遗留，无调用方 | `GET /v3/place/around`；当前规划不搜真实餐厅 |
| `search_restaurants` | ⚠️ 遗留，无调用方 | 旧的按餐别搜餐厅方案；当前只输出「区域 + 本地特色」 |
| `/v3/direction/*` | ❌ 当前未调用 | 驾车/步行/骑行/公交路径规划；只保留为「行程定稿后校准」的后续能力 |

#### 高德接口与配额

| 调用阶段 | 实际接口 | 额度 / Key | 是否影响 `POST /api/trip/plan` |
|---|---|---|---|
| seed 补景点坐标 | `/v3/geocode/geo` | Web 服务 Key；基础 LBS **15 万/月** | **不影响**；规划读取已落库坐标 |
| seed 失败兜底 | `/v3/place/text` | Web 服务 Key；POI 搜索 **5 千/月** | **不影响**；只在地理编码失败时发生 |
| 前端打开地图 | 高德 JS API | `VITE_AMAP_JS_KEY`，与 Web 服务 Key 不通用 | **不影响**；缺 Key 自动降级 SVG |
| 生成规划 | **无高德接口** | 不消耗高德配额 | **只用本地规则计算** |
| 未来行程定稿校准 | `/v3/direction/driving` 等路径规划接口 | 尚未接入；需要按天/路段缓存 | 计划在用户确认方案后再调用，不能写成当前已实现 |

**坐标与返回格式**：高德 `location` 统一为 `lng,lat`，代码转换成 `(lat, lng)`；坐标直接按 **GCJ-02** 入库，不做 WGS-84/BD-09 转换。
`city` / `citylimit=true` 用来限制 POI 关键字搜索范围；region 目的地不能直接把「北疆/西疆」当城市，要传真实过夜基地或用 `_too_far` 做坐标校验。

**额度保护**：`_QUOTA_GONE_GEO` 与 `_QUOTA_GONE_POI` 两个熔断独立；10003/10044/40000 视为额度耗尽并停止对应接口，10004/10014/10015/10019/10020/10021 视为瞬时 QPS 限流，不把另一条接口一起熔断。

> ⚠️ `amap_key` 作为 GET 参数发送。`search_around` / `search_restaurants` 是历史遗留代码，当前餐饮不写具体店名（店铺会变化，写死会误导）。

### `seed.py`（748 行）— 数据初始化 + 增量补景点

| 符号 | 行号 | 说明 |
|---|---|---|
| `SEED_FILE` | 37 | `app/trip/data/seed_attractions.json` |
| `TARGET_PER_CITY` | 38 | **22**（LLM 出名单时的目标条数） |
| `AMAP_SLEEP` | 40 | **0.8s**，节流。⚠️ 注释里写的 0.25 是**历史值**，实测会撞 `CUQPS_HAS_EXCEEDED_THE_LIMIT` |
| `SPOT_MAX_KM` | 41 | 3.0，子景点离母景点超过这个距离判为同名地点、丢弃。⚠️ **对「面积型景区」会误杀**：西湖风景名胜区的 POI 是整片 5km 景区的**质心**，断桥在 4.9km 外被丢掉坐标 —— 见 `CHANGELOG.md` 2026-09-16 第 9 节 |
| `MAX_ATTRACTION_KM` | 51 | **400.0**，景点命中的坐标离**最近的落脚点**超过这个距离 → 判为同名地点丢弃。⚠️ 按「落脚点」而不是「中心」判，因为 region 本身就跨上千公里 |
| `_too_far` | 54 | 上面那道校验的实现 |
| `LIST_USER` | 83 | 名单提示词。含 `{bases}`（region 过夜基地）与 `{exclude}`（**补景点时点名排除的已入库景点**，全量重灌传空串） |
| `_bases_hint` | 124 | region 专用：把**过夜基地**报给模型，它才知道「西疆」= 伊犁河谷而不是整个新疆 |
| `NEIGHBOR_KM` / `NEIGHBOR_MAX` / `NEIGHBOR_NAMES_MAX` | 143–145 | 600km / 8 个 / 30 个名字 —— 「邻居」的定义 |
| `_neighbors_hint` | 148 | 把**同区域其它目的地**及其**已收走的景点名**报给模型。⚠️ 只写「别写它们的景点」不够，**必须点名** |
| `EXCLUDE_NAMES_MAX` / `_exclude_hint` | 201–218 | 「补景点」点名排除已有景点（上限 40 个）。⚠️ 只点名还不够，入库前还有一层 `_dup_with_existing` 的「互相包含」兜底（拦 `晋祠` vs `晋祠博物馆`） |
| `load_offline_seed` | 227 | 读 JSON |
| `llm_attraction_list` | 233 | 让 LLM 出名单（不含坐标）。**条数由 `--per` 传入**（默认 `TARGET_PER_CITY`）。提示词里明确要求**核心近郊景区必须包含**（桂林→阳朔这类） |
| `upsert_city` | 271 | UPSERT 城市元数据（`kind` / `region` / `center` / `hotel_areas`） |
| `_fill_spot_coords` | 292 | 给子景点补坐标：多轮尝试 + 距离校验；**已有坐标的直接跳过**（省配额） |
| `upsert_attractions` | 365 | UPSERT 景点。`coord_source=="amap"` 且带坐标的**直接信任、不重复打接口**（省配额）——「补景点」就是靠这个做到只花地理编码配额 |
| `seed_city` | 444 | 单城流程（全量） |
| `_dup_with_existing` | 485 | 候选名与已有景点「互相包含」判重 |
| **`topup_city`** | 490 | **增量补景点**：只补缺的、已有的绝不动；坐标**只走地理编码**（region 名字不是行政区划 → city 参数留空，靠 400km 距离闸）；**不补子景点**。返回 (现有数, 新增数, 新增明细) |
| `_sync_offline_seed` | 554 | 把新增景点**回写离线种子**（铁律 21：种子 = 库里现状） |
| `main` | 616 | CLI：`--city` / `--reset` / `--no-amap` / `--list` / `--only-cities` / `--source` / **`--per N`** / **`--only-empty`** / **`--topup`**（配合 `--per`：把景点数不足 `--per` 的补到 `--per`） |

**节流**：`time.sleep(AMAP_SLEEP)` 在**每次调用后无条件执行**（299、370），
QPS 上限约 **1.25 req/s**，**串行逐条**，无并发。加上高德自身约 0.8s 的响应延迟，
实测**单城约 1.6s/请求、总耗时 2~3 分钟**（22 景 + 60~80 条子景点）。
⚠️ **别为了提速把两个城市并行跑** —— 会撞 QPS 限流，而失败是静默的：
`search_poi` 返回 None → 那条景点被「跳过无坐标」丢掉，等于数据缺失。

**`--city` 与全量的区别（很重要）**：`main()` 不带参数时会遍历种子里**所有**目的地，
每城都调一次 DeepSeek 出名单。给老城补数据时**必须逐个 `--city`**，
否则会拿 LLM 现生成的新名单去覆盖那些手工校对过的老城数据。

**坐标来源标记**：命中高德 → `coord_source="amap"`；否则 `"seed"`。

#### 种子数据（`data/seed_attractions.json`）

**68 个目的地（47 city + 21 region）/ 899 景 / 1724 条子景点**，
**与库逐字段一致**（名字集合 / heat / `visit_minutes` /
`must_visit` / `best_time` / `district` / `intro` / 坐标 / `tags` / `coord_source` / 子景点）。
⚠️ 2026-09-17 用 `--topup` 补的 74 条**没有子景点**（详情弹窗空态）。

| 项 | 内容 |
|---|---|
| `kind=city`（47 个） | 老 17 个 + 新增 30 个：天津、石家庄、太原、呼和浩特、沈阳、大连、长春、哈尔滨、济南、合肥、苏州、无锡、扬州、宁波、绍兴、福州、泉州、南昌、景德镇、郑州、洛阳、海口、昆明、贵阳、南宁、拉萨、兰州、西宁、银川、乌鲁木齐 |
| `kind=region`（21 个） | 贵州（老）+ **20 个新增**：川西、北疆、西疆、南疆、云南、河西走廊、青海湖环线、藏东南、呼伦贝尔、黑龙江雪乡·漠河、长白山、山西、陕北、豫北太行、鲁南、赣东北、闽西北、湘西、鄂西、桂西南 |
| 有子景点（61 个） | 共 **1724 条**；少数老城（北京/上海/西安/重庆/广州/南京）为空 |
| 热度 | **全国统一 T 级 0~1000**（故宫 990 / 熊猫基地 560 / 华山 560） |
| 坐标 | 全部 GCJ-02、全部来自高德；条目都带 `coord_source="amap"` 与子景点坐标 → **离线灌数据不打任何高德请求** |

> ⚠️ **种子必须与库保持同步**（AGENTS.md 铁律 21）。漂移过一次：老城的种子停留在 T 级迁移
> **之前**（`heat` 是 0~100 旧尺度），配上 `MUST_HEAT=500` **离线灌数据一个「必去」都判不出来**。
> 机制是 `upsert_attractions` **只 upsert、不删除** —— 旧名匹配不上新名就两边各留一份。
> 对齐方式与教训见 `CHANGELOG.md` 2026-09-16 第 7 节。

---

## 运行时数据流（一次 `POST /plan`）

```
routers.post_plan
  └─ _guard(payload)                        景点 ≤40
  └─ service.generate_plan
       ├─ resolve_city → _load_selected     查 trip_city / trip_attraction
       ├─ planner.plan_fallback             ← 基线（必算）
       │    ├─ prune_to_capacity            ① 容量裁剪
       │    ├─ assign_days                  ②③ 独占隔离 + 片区簇分配 + ⑤ 时段排序
       │    ├─ plan_hotels                  住宿片区
       │    └─ trim_day → materialize_day   ④ 逐天裁剪 + 物化
       ├─ llm.run_pipeline                  （有 Key 时覆盖基线）
       │    ├─ 重新算一遍分天               不复用基线结果（prune + assign + plan_hotels）
       │    ├─ plan_days_with_llm           ①.5 审阅分天，过 5 道硬校验才采纳
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
| 1 | `llm.py:35` / `README` 说「11 个工具，`LLM_MODE=tools` 打开」 | 实际 **10 个** `_tool_*` 函数；`LLM_MODE` **从未实现**，整条工具链**无调用方** |
| 2 | `trip/__init__.py:6` 说「routers.py 4 个 HTTP 端点」 | 实际 **9 条** |
| 3 | `trip/__init__.py:3` 说「ORM **三表**：city / attraction / trip_plan」 | 实际 **4 张表**（多 `trip_spot` 子景点表） |
| 4 | `routers.py:11-12` 警告 `/plan/preview` 顺序 | 该路径不存在（preview 是 `POST /preview`） |
| 5 | `models.py:95` 注释说 `coord_source` 可为 `manual` | 代码只写过 `amap` / `seed` |
| 6 | `planner.py:12-17` 模块文档说分天「四步」 | 实际 **五步**，且第 ③ 步已是「**片区簇分配**」而非「成链均分」 |
| 7 | `config.py` 的 6 个配置项（`LLM_MAX_RETRY` / `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` / `DEFAULT_START_TIME` / `DEFAULT_RETURN_TIME` / `MAX_DAYS`） | **全部零引用**——改 `.env` 里这几项不会生效（见 `config.py` 小节） |
| 8 | `schemas.py:382` 的 `ApiError` | 定义但无人使用 |
| 9 | `amap.py` 的 `search_around`(126) / `search_restaurants`(191) | 无调用方（只有 `search_poi` 在用） |
| 10 | `README` / `DEVELOPMENT` / `AGENTS` 里的 `AMAP_SLEEP` 转述 | 文档都写 **0.25s**，实际是 **0.8**。代码注释是「0.25 实测会撞 `CUQPS_HAS_EXCEEDED_THE_LIMIT`」—— 那是在解释**为什么放弃 0.25**，容易被误读成当前值。据此推的「一次全量约 40 秒」也是错的，实际单城就要 2~3 分钟。**文档已订正** |

**要不要清理？** 这些死代码/注释不影响运行，但会误导接手的人。建议的处理顺序见 [`DEVELOPMENT.md`](DEVELOPMENT.md#待办与建议)。
