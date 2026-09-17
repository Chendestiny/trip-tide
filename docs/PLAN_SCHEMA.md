# 时间轴契约（PlanResult）

> 配套：[`README.md`](README.md) 索引 · [`BACKEND.md`](BACKEND.md) 后端地图 · [`API.md`](API.md) 接口契约

**重要前提**：`PlanResult` **不是 LLM 的输出格式**，而是硬编码物化出来的结果。

流水线里 LLM 只在**阶段②**介入，且每路只能产出「当天主题 + 各景点建议 + 餐饮区域与特色小吃」这一小段 JSON；
**时间、停留、路程、饭点位置全部由 `planner.materialize_day()` 算出来**。

这条前提决定了下面所有约束都是「结构性保证」而不是「靠模型自觉」——
LLM 根本没机会产出时间字段，所以不可能算错。（为什么这么设计见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。）

---

## 完整结构

```json
{
  "city": "成都",
  "days": 3,
  "summary": "三天行程以市区及近郊为主，住宿固定在宽窄巷子/人民公园片区…",
  "day_plans": [
    {
      "day": 1,
      "theme": "都江堰全天｜离堆公园 → 宝瓶口 → 安澜索桥 → 二王庙",
      "pace": "紧凑",
      "nodes": [
        {
          "time": "09:00",
          "type": "depart",
          "name": "从「宽窄巷子/人民公园」出发",
          "advice": "早高峰建议提前 10 分钟叫车，先到最远的一站再往回走。",
          "attraction_id": null,
          "stay_minutes": 0,
          "travel_minutes": 0,
          "travel_mode": "",
          "must_visit": false,
          "moved_to_day": null,
          "moved_from_day": null,
          "move_reason": ""
        },
        {
          "time": "11:20",
          "type": "meal",
          "name": "都江堰景区外南桥/西街一带·本地特色午餐",
          "advice": "推荐本地特色：尤兔头、葱葱卷、老腊肉。就近在当天片区解决，别为了一顿饭跨区，会打乱下午的动线。",
          "attraction_id": null,
          "stay_minutes": 60,
          "travel_minutes": 140,
          "travel_mode": ""
        },
        {
          "time": "12:25",
          "type": "attraction",
          "name": "都江堰景区",
          "advice": "从南桥进景区最顺，安澜索桥人多时先走二王庙再折回。",
          "attraction_id": 7,
          "stay_minutes": 300,
          "travel_minutes": 5,
          "travel_mode": "顺风车",
          "must_visit": true
        },
        {
          "time": "18:45",
          "type": "meal",
          "name": "宽窄巷子/人民公园·本地特色晚餐",
          "advice": "晚餐安排在住宿片区附近，吃完直接回酒店，不用再折腾交通。",
          "stay_minutes": 75,
          "travel_minutes": 80,
          "travel_mode": "顺风车"
        },
        {
          "time": "20:10",
          "type": "hotel",
          "name": "入住「宽窄巷子/人民公园」",
          "advice": "到当日各景点平均单程约 56.4 km… 注意：当天路程较远，实际回到酒店约 20:10，比目标时间晚 40 分钟。",
          "stay_minutes": 0,
          "travel_minutes": 10,
          "travel_mode": "步行"
        }
      ],
      "hotel": {
        "area": "宽窄巷子/人民公园",
        "reason": "到当日各景点平均单程约 56.4 km，位置最居中。老城核心，步行可达宽窄巷子、人民公园。",
        "alternatives": [
          { "area": "春熙路/太古里", "tradeoff": "价格偏高，临街房偏吵。" }
        ]
      }
    }
  ],
  "must_visit_reasons": [
    { "name": "大熊猫繁育研究基地", "reason": "热度 98｜看熊猫要趁早，7:30 开园时熊猫最活跃。", "attraction_id": 1 }
  ],
  "dropped": [
    { "name": "青城山", "reason": "单程约 80 分钟、游览 5.0 小时，基本要占掉一整天；3 天预算装不下市区线 + 远郊。", "attraction_id": 8 }
  ]
}
```

---

## 字段说明

### 顶层

| 字段 | 类型 | 约束 |
|---|---|---|
| `city` | string | ≤32 字 |
| `days` | int | 1~7，等于请求的天数 |
| `summary` | string | ≤300 字。LLM 终检通过时会用终检润色后的版本覆盖 |
| `day_plans` | DayPlan[] | 数量恒等于 `days`，`day` 从 1 连续递增（`schemas.py:240` 强校验） |
| `must_visit_reasons` | MustVisitReason[] | 可空。取勾选里热度最高的 3 个 |
| `dropped` | DroppedItem[] | 可空 |

### DayPlan

| 字段 | 类型 | 说明 |
|---|---|---|
| `day` | int | 1~7，连续 |
| `theme` | string | ≤80 字。LLM 起名；降级时是「片区—片区 顺路线｜A → B → C」 |
| `pace` | `轻松` \| `适中` \| `紧凑` | **算出来的**：`Σ停留 + Σ路程` ≤300 → 轻松，≤450 → 适中，否则紧凑 |
| `nodes` | TimelineNode[] | 时间轴主体 |
| `hotel` | HotelAdvice | 住宿片区建议 |

### TimelineNode

| 字段 | 类型 | 说明 |
|---|---|---|
| `time` | `HH:MM` | **算出来的**，等于上一站 time + 上一站 stay + 本节点 travel |
| `type` | `depart` \| `attraction` \| `meal` \| `hotel` \| `transit` | 决定前端图标与配色（`trip-theme.js` 的 `nodeVisual`） |
| `name` | string | 景点名 / 「{区域}·本地特色午餐（晚餐）」 / 住宿片区。**餐饮不含具体店名**（店会换，写了误导） |
| `advice` | string | **优先级：子景点拼串 > LLM 写的 > 库里的 `intro`**。有子景点的景点走 `planner.spot_advice()` 硬编码拼串（**不花 token、可人工校对**）；餐饮/住宿是固定文案 |
| `attraction_id` | int \| null | 景点节点必填，且必须来自勾选清单 |
| `stay_minutes` | int | 0~600。景点用库里的 `visit_minutes`，午餐 60 / 晚餐 75，其余 0 |
| `travel_minutes` | int | 0~600。从上一站到这里的耗时，**含可能的等待**（为凑饭点而等的时间也记在这里） |
| `travel_mode` | string | 步行 / 地铁公交 / 打车 / 自驾 / 自驾·高速 / 顺风车 / 城际大巴 / 城际铁路 |
| `must_visit` | bool | `库里 must_visit` **或** `热度 ≥ MUST_HEAT=500`（全国 T 级；定义在 `planner.py:365`，判定在 `_is_must`(373)） |
| `moved_to_day` / `moved_from_day` | int \| null | 被顺延到别的天时标注，前端显示「已挪至 Day N（原 Day M）」 |
| `move_reason` | string | 顺延原因 |

### HotelAdvice

| 字段 | 类型 | 说明 |
|---|---|---|
| `area` | string | 优先从城市预置的 `City.hotel_areas` 里选 |
| `reason` | string | 为什么住这里（自动选时带「平均单程 X km」的量化描述） |
| `alternatives` | `[{area, tradeoff}]` | 备选片区 + 取舍代价 |

> **住宿一次定全程**：`plan_hotels()` 按「全程景点的几何中心」选一个常住片区，只有某天景点离它 > 45km（如成都行程里的都江堰日）才建议就近过夜。频繁换酒店是自助游最烦的事之一。

---

## 三条恒成立的不变量

这三条由 `materialize_day()` 的 `push()` 统一保证（`cur_time` 只在那一处推进）：

```
① 时间自洽   prev.time + prev.stay_minutes + cur.travel_minutes == cur.time
② 饭点唯一   每天恰好一个 lunch（time < 15:00）+ 一个 dinner（time >= 15:00）
③ 景点唯一   同一个 attraction_id 在整份行程里只出现一次
```

校验点（三处，职责不同）：

| 位置 | 行号 | 做什么 | 失败后果 |
|---|---|---|---|
| `planner.rule_violations()` | `planner.py:338` | 时段硬约束（熊猫上午 / 夜景 15:00 后 / 博物馆 15:30 前） | 旧工具链会打回；现流水线只记日志 |
| `tools.audit_day()` | `tools.py:525` | 上面 ① ② + 出发时间一致 + 收尾超时 >60 分 | 由 `service.audit_plan` 调用，**只打 warning 日志，不阻断** |
| `scripts/check_planner.py` | — | 15 个用例逐节点跑一遍 6 项校验 | 回归失败要修 |

---

## 饭点是怎么插的

午餐锚点 `11:20`、晚餐锚点 `18:00`，规则（`planner.py:1214–1332`）：

```
午餐
   到达时间 + 游览时长 > 14:00  且  等待午饭的时间 ≤ 75 分钟
       → 到了先吃（吃完饭再进景区，此时 travel 重置为 5 分钟，只算步行进景区）
   否则
       → 玩完再吃（长景点如华山，下山后再吃是常态）
   兜底：max(cur_time, 12:00)

晚餐
   先算回住宿片区的路程，dinner_at = max(到达住宿片区, 18:00)
   → 晚餐锚点定在「回到住宿片区之后」，不在返程路上吃

留白节点
   若 dinner_at - 到达住宿片区 > 90 分钟
       → 插一个 type="transit" 的「返回住宿片区休整 · 自由活动」，
         避免时间轴凭空缺几个小时（踩过这个坑）
```

插餐饮节点时传 `gap`（从上一站到餐厅的耗时）；不传就按「目标时刻 − cur_time」反推。
**所以即使为凑饭点等了 60 分钟，时间轴依然自洽。**

---

## 流水线里 LLM 实际产出什么

LLM **不产出** `PlanResult`。阶段② 每路（每天一路，`ThreadPoolExecutor` 并发）只产出一小段 JSON：

```json
{
  "theme": "当天主题一句话",
  "notes": [{ "attraction_id": 2, "advice": "一句具体建议" }],
  "lunch":  { "area": "午餐区域", "hint": "本地特色小吃" },
  "dinner": { "area": "晚餐区域", "hint": "本地特色" }
}
```

| 产出 | 落到哪 | 落到 `PlanResult` 的哪个字段 |
|---|---|---|
| `theme` | — | `DayPlan.theme` |
| `notes[].advice` | — | 对应景点节点的 `TimelineNode.advice` |
| `lunch.area` / `hint` | 拼成「{area}·本地特色午餐」 | 午餐节点的 `name`；`hint` 进 `advice` |
| `dinner.area` / `hint` | 同上 | 晚餐节点的 `name` / `advice` |
| 终检 `summary` | — | `PlanResult.summary`（覆盖兜底文案） |
| 终检 `issues` | — | `PlanResponse.review.issues`（**只提意见，不改时间轴**） |

**提示词里的硬性要求**（`llm.py:91–98`）：

1. `notes` **只写标了「要 advice」的**景点 —— 有子景点的（73%）advice 由 `planner.spot_advice()`
   用预存攻略硬编码拼出来，模型不用重复写；
2. `advice` 要具体可执行（几点去人少、哪个门进、要不要预约、排队多久），禁止「值得一去」「风景优美」这类空话；
3. 餐饮**只给区域 + 本地特色小吃/菜系，不写具体店名**；
4. **不输出任何时间、停留时长、交通方式**。

`_apply_day_content()`(`llm.py:265`) 负责把这段 JSON 覆盖到已物化的 `DayPlan` 上——**它只碰文案字段，不碰时间**。

---

## 兜底引擎产出同一个结构

`planner.plan_fallback()` 返回**完全相同的 `PlanResult`**，所以前端不需要区分来源（`source` 字段只用来打标签）。
它严格遵守三条不变量，差别只在文案质量：

| | LLM 并行流水线 | 规则引擎兜底 |
|---|---|---|
| 景点建议 | 具体（哪个门进、几点人少、要不要预约） | 用库里的 `intro` |
| 餐饮 | 区域 + 本地特色小吃（LLM 按片区给） | 「{片区}·本地特色午餐（就近选）」 |
| 主题名 | 有文采 | 「片区—片区 顺路线｜A → B → C」 |
| 住宿理由 | 讲动线、讲夜间可逛性 | 带「平均单程 X km」的量化描述 |
| 耗时 | 15~30 秒 | **毫秒级** |

换句话说，**规则引擎是提示词规则的参考实现**——改提示词前先看它怎么做的。

### 降级时会发生什么

| 情况 | `source` | 表现 |
|---|---|---|
| 没配 Key | `fallback` | summary 末尾追加「（未配置 DeepSeek Key，由本地规则引擎生成）」 |
| 流水线整体失败 | `fallback` | summary 末尾追加失败原因 |
| 只有某一天失败 | `llm` | 该天回退到规则文案，`trace` 里记 `gen_day day=N ✗`，其余天仍是 LLM 产出 |
| 终检失败 | `llm` | `review` 为 `null`，不影响主方案 |
