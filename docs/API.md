# 接口契约

> 配套：[`README.md`](README.md) 索引 · [`PLAN_SCHEMA.md`](PLAN_SCHEMA.md) 时间轴字段 · [`BACKEND.md`](BACKEND.md) 后端地图

Base：`/api/trip`（dev 下由 Vite 把 `/api` 代理到 `127.0.0.1:8000`）
交互式文档：http://localhost:8000/docs

统一约定：

- 成功直接返回数据体，不包 `{code, data}` 壳。
- 失败返回 FastAPI 标准错误体 `{"detail": "中文错误文案"}`，前端 `trip-api.js` 会把 `detail` 提出来直接展示。
- 所有经纬度均为 **GCJ-02**。

---

## 1. GET /api/trip/cities

热门城市列表，按 `heat` 降序。首页宫格用。

**响应**

```json
[
  {
    "id": 1,
    "name": "成都",
    "pinyin": "chengdu",
    "emoji": "🐼",
    "tagline": "巴适慢生活 · 熊猫与麻辣",
    "heat": 98,
    "center_lat": 30.657,
    "center_lng": 104.0657,
    "attraction_count": 20
  }
]
```

`attraction_count = 0` 说明该城市还没灌景点，前端点进去会是空列表。

---

## 2. GET /api/trip/attractions?city=成都

景点池，按 `heat` 降序（次键 `visit_minutes` 降序）。

**查询参数**

| 参数 | 必填 | 说明 |
|---|---|---|
| `city` | 是 | 城市名或拼音，如 `成都` / `chengdu` |

**响应**

```json
[
  {
    "id": 12,
    "name": "大熊猫繁育研究基地",
    "intro": "看熊猫要趁早，7:30 开园时熊猫最活跃。",
    "district": "成华区",
    "lat": 30.738,
    "lng": 104.1469,
    "visit_minutes": 180,
    "heat": 98,
    "must_visit": true,
    "tags": ["亲子", "必去"]
  }
]
```

**错误**

| 状态码 | 场景 |
|---|---|
| 400 | 没传 `city` |
| 404 | 城市不存在，`detail` 里会列出当前已开放的城市 |

---

## 2.1 GET /api/trip/attractions/{attraction_id}/spots

**某景点内部的子景点**（坐标 + 攻略）。数据在 seed 阶段预存（`trip_spot` 表）——
景点内部路线这类信息 2~3 年不变，不该每次规划都让模型现写。

做成**单独接口**而不是塞进 `AttractionOut`：景点列表页用不到它，
带上会让响应体积翻几倍（20 景点 × 4 子景点）。

```json
[
  {
    "id": 31,
    "name": "安澜索桥",
    "lat": 31.0034,
    "lng": 103.6161,
    "guide": "走桥时靠内侧，晃得轻一些；桥上人多时别停步拍照，容易被后面的人推着走。",
    "order_index": 3,
    "stay_minutes": 20
  }
]
```

| 字段 | 说明 |
|---|---|
| `lat` / `lng` | **可能为 `null`** —— 高德对小众子景点常搜不到，这时只保留攻略文字 |
| `order_index` | 建议游览顺序（从 1 开始） |
| `stay_minutes` | 建议停留分钟；`0` 表示未标注 |

**错误**：404 —— 景点不存在。

---

## 3. POST /api/trip/preview

**紧凑度预估，不调 LLM，毫秒级返回。** 选景点时实时调用（前端防抖 350ms），
让用户点「开始规划」之前就知道结果。

请求体与 `/plan` 相同（只有 `city` / `attraction_ids` / `days` / `transport` / `pace` 等会影响结果）。

```json
{
  "city": "成都",
  "days": 2,
  "pace": "balanced",
  "transport": "mixed",
  "selected_count": 10,
  "scheduled_count": 7,
  "total_visit_minutes": 735,
  "total_travel_minutes": 210,
  "capacity_minutes": 990,
  "load_ratio": 0.85,
  "tightness": "超载",
  "per_day": [
    { "day": 1, "count": 4, "visit_minutes": 420, "budget_minutes": 495,
      "start_time": "09:00", "names": ["成都大熊猫繁育研究基地", "..."] }
  ],
  "will_drop": ["青城山", "都江堰景区", "成都大熊猫繁育研究基地"],
  "suggestion": "10 个景点排 2 天装不下，约 3 个会被舍弃（青城山、都江堰景区…）。想都去建议加到 3 天。"
}
```

**判定指标：把「勾选景点的总游览时长」摊到每天，再和节奏目标比。**

```
日均游览 = Σ(勾选景点的 visit_minutes) ÷ 天数
节奏目标 = 轻松 240 / 平衡 360 / 紧凑 450 分钟   ← planner.PACE_TARGET_MINUTES
ratio    = 日均游览 ÷ 节奏目标

ratio ≥ 1.5 → 超载     ratio ≥ 1.0 → 紧凑
ratio ≥ 0.65 → 适中    否则        → 轻松
```

另有**舍弃兜底**：有景点被丢 → 至少「紧凑」；丢弃比例 > 25% → 「超载」。

### 为什么不用「时间占用率」

`load = (游览+路程) ÷ (逐天预算之和)` 的**分子分母同随天数增长**，加天数几乎不动 ——
以成都 9 个景点为例，`load` 只在 0.82~1.02 之间浮动，反映不出松紧。
这正是它算不出「6 天只排了 4 天的量」的原因。

### 为什么用「勾选总量」而不是「排入总量」

「排入总量」会随天数增加而**增加**（天数多 → 容量裁剪丢得少 → 排入更多），
用它算日均会导致「加天数时日均不降反平」。用勾选总量，日均随天数**单调下降**，
才符合「多加一天就更松」的直觉。

### 实测（成都 9 个景点，含都江堰、青城山两个远郊）

| 天数 | 日均游览 | 判定（平衡） | 备注 |
|---|---|---|---|
| 2 天 | 735 分 | 超载 | 丢 3 个 |
| 3 天 | 490 分 | 紧凑 | 丢 2 个 |
| 4 天 | 368 分 | 紧凑 | 丢 1 个 |
| 5 天 | 294 分 | 适中 | 全部排入 |
| 6 天 | 245 分 | 适中 | 全部排入 |
| 7 天 | 210 分 | 轻松 | 全部排入 |

### `days_needed`（仅提示，不参与判定）

响应还会返回 `days_needed` —— **按片区排最舒服需要几天**（地理下限 = 大景点/远郊各一天 +
每个片区簇一天）。它**不是**判定依据：天数不够时 `assign_days` 会把簇合并、每天跨片区跑，
所以它只是「舒适下限」。

---

## 4. POST /api/trip/plan

生成按天方案。**核心接口，耗时 15~30 秒**（硬编码分天 + 每天并行生成内容 + 一次终检）。

**请求体**

```json
{
  "city": "成都",
  "attraction_ids": [12, 1, 4, 5, 8],
  "days": 3,
  "start_time": "09:00",
  "return_time": "19:30",
  "first_day_start_time": null,
  "last_day_return_time": null,
  "transport": "mixed",
  "pace": "balanced"
}
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `city` | string | 必填 | 城市名或拼音 |
| `attraction_ids` | int[] | 1~40 个，自动去重 | 勾选的景点 id |
| `days` | int | 1~7，默认 1 | 行程天数 |
| `start_time` | string | `HH:MM`，默认 `09:00` | 每天出发时间 |
| `return_time` | string | `HH:MM`，默认 `19:30` | 目标返回时间（可跨零点，如 `01:00`） |
| `first_day_start_time` | string \| null | `HH:MM` | 第一天到达时间（不填用 `start_time`） |
| `last_day_return_time` | string \| null | `HH:MM` | 最后一天返回时间（不填用 `return_time`） |
| `transport` | string | `drive` \| `mixed` \| `transit`，默认 `mixed` | 自驾 / 打车+公共 / 公共交通。历史值 `taxi` 等价于 `mixed` |
| `pace` | string | `relaxed` \| `balanced` \| `packed`，默认 `balanced` | 宽松会舍弃更多景点、早点收工；紧凑尽量多塞 |

**响应**

```json
{
  "plan_id": 3,
  "created_at": "2026-09-14 18:52:07",
  "source": "llm",
  "title": "成都 · 3 天 7 景",
  "request": { "...": "入参原样回传" },
  "result": { "...": "时间轴，见 PLAN_SCHEMA.md" },
  "attractions": [ { "id": 1, "name": "成都大熊猫繁育研究基地", "lat": 30.7406, "lng": 104.138, "...": "" } ],
  "review": {
    "ok": true,
    "summary": "润色后的方案总述",
    "issues": [ { "day": 2, "level": "warn", "text": "熊猫基地 14:20 才到，熊猫午后多在休息，建议提到上午" } ]
  },
  "trace": [
    "plan_days days=3 kept=7 dropped=1",
    "gen_day day=1 ok",
    "gen_day day=2 ok",
    "gen_day day=3 ok",
    "materialize days=3 elapsed=17.7s"
  ]
}
```

| 字段 | 说明 |
|---|---|
| `source` | `llm` = LLM 流水线；`tweak` = 倾向微调；`fallback` = 本地规则引擎（summary 末尾会注明原因） |
| `attractions` | 一并回传，前端画地图不需要再请求一次 |
| `review` | LLM 终检结果。`issues[].level` 取 `info`/`warn`/`error`；`fallback` 时为 `null` |
| `trace` | **流水线轨迹**：`plan_days`（分天）→ `gen_day day=N ok\|✗`（每天一路的并行结果，`✗` 表示该天回退到规则文案）→ `materialize ... elapsed=Xs`（物化耗时，即整条链路总耗时）。微调时是 `adjust mode=tweak pace=... days=... restored=N`。前端可不展示 |

**错误**

| 状态码 | 场景 |
|---|---|
| 400 | 勾选超过 40 个；或景点不属于该城市 |
| 404 | 城市不存在 |
| 500 | 生成失败，`detail` 带异常类型 |
| 502 | 模型不可用（`llm.LLMError`）——实际上很少触发，因为 `service.generate_plan` 内部已把 `LLMError` 捕获并降级 |

> 流水线失败**不会**返回 5xx —— 会自动降级到规则引擎并返回 200（`source=fallback`，summary 末尾注明原因）。
> 只有「降级也失败」（例如数据库写不进去）才会报 500。
>
> 耗时参考：4 天 10 个景点实测 **17.7 秒**（4 路并行）。旧的 function calling 工具编排方案要 1.1 分钟。

---

## 4.1 POST /api/trip/auto-plan

**一键 AI**：只给城市 + 天数 + 节奏，景点由服务端自动挑。

与 `POST /plan` 的**唯一区别**是不需要传 `attraction_ids`。内部流程是：

```
planner.pick_attractions()   必去优先 → 热度其次，累计游览时长逼近「天数 × 节奏目标」
        ↓
转成普通 PlanRequest → 调用**同一个** service.generate_plan()
```

所以返回结构、`source` 标记、降级行为与 `/plan` **完全一致**；实际选中的景点会回填进
`request.attraction_ids`，前端和历史记录都能看到「AI 挑了哪些」。

**请求体**

```json
{
  "city": "成都",
  "days": 4,
  "pace": "balanced",
  "transport": "mixed",
  "start_time": "09:00",
  "return_time": "19:30"
}
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `city` | string | 必填 |
| `days` | int | **必填**，1~7（默认 2） |
| `pace` | string | **必填**，`relaxed` / `balanced` / `packed`（默认 `balanced`） |
| `transport` | string | `drive` / `mixed` / `transit`，默认 `mixed` |
| `start_time` / `return_time` | string | `HH:MM`，默认 `09:00` / `19:30` |
| `first_day_start_time` / `last_day_return_time` | string \| null | 同 `/plan` |

**响应**：与 `POST /plan` 完全相同。

**实测挑选结果**（成都 20 个景点）：

| 天数 / 节奏 | 选中 | 游览合计（目标） |
|---|---|---|
| 2 天 平衡 | 7 个 | 750（720） |
| 4 天 轻松 | 8 个 | 990（960） |
| 4 天 平衡 | 14 个 | 1560（1440） |
| 6 天 平衡 | 20 个 | 2265（2160） |

**错误**

| 状态码 | 场景 |
|---|---|
| 400 | 城市不存在，或该城市还没有景点数据（先跑 seed） |
| 500 | 生成失败（同 `/plan`） |

---

## 5. POST /api/trip/plan/{plan_id}/adjust

**按倾向微调已有方案，不调 LLM，毫秒级返回。** 复用原方案的景点分配，只按新参数重排时间轴。

请求体与 `/plan` 相同。与「重新生成」的区别：

| | 微调 | 重新生成 |
|---|---|---|
| 耗时 | 毫秒级 | 15~30 秒 |
| 是否花 token | 否 | 是 |
| 文案 | 沿用原来的 | 重新生成 |
| 分天 | 沿用原来的 | 可能变 |

`pace=packed` 时会把原方案里被舍弃的景点尽量塞回有余量的天；
`pace=relaxed` 时按缩小后的预算剔除塞不下的，让每天早点收工。

返回新的 `PlanResponse`（`source` 为 `tweak`），原方案保留在历史里。

---

## 6. GET /api/trip/plan/{plan_id}

按 id 读取历史方案，响应结构与 POST 完全一致（`review` 与 `trace` 从库里读回）。
前端刷新页面后回看用。

**错误**：404 —— 该 id 不存在。

---

## 7. GET /api/trip/plans?limit=20

历史列表（轻量条目）。V1 前端「我的」页用的是 localStorage，这个接口留给接入登录后切换。

```json
[
  {
    "plan_id": 3,
    "city": "成都",
    "days": 3,
    "title": "成都 · 3 天 7 景",
    "source": "llm",
    "created_at": "2026-09-14 18:52",
    "attraction_count": 7
  }
]
```

---

## 8. GET /api/health

```json
{
  "ok": true,
  "app": "TripTide API",
  "version": "0.1.0",
  "llm_ready": true,
  "amap_ready": true,
  "db": "mysql",
  "model": "deepseek-flash"
}
```

首页据此提示「AI 引擎已就绪 / 未配置 Key，走规则引擎」。

`GET /api/health/models` 返回模型别名 → 真实模型名的映射表。
