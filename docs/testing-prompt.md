# 单元测试提示词（可直接整段交给另一个模型）

> 用法：把下面「===== 提示词开始 =====」到「===== 提示词结束 =====」之间的内容整段复制给对方模型。
> 建议附上仓库路径，或让它先读 `backend/app/trip/planner.py`。

---

===== 提示词开始 =====

你是 Python 测试工程师。请为一个 FastAPI + SQLAlchemy 项目写**单元测试**。

## 项目

- 路径：`D:\Project\trip-tide`（Windows，Git Bash 环境）
- 后端：`backend/`，Python 3.13，依赖 FastAPI / SQLAlchemy 2.0 / Pydantic v2
- 业务：AI 行程规划工具 —— 用户选城市和景点，后端按天排出带时间的行程
- Python 解释器：`backend/venv/Scripts/python.exe`（不要用全局 python）

## 任务

为 `backend/app/trip/planner.py` 与 `backend/app/trip/llm.py` 里的**纯函数**写单元测试。

## 硬性约束（务必遵守）

1. **不连数据库**（不要 `SessionLocal` / `select`）
2. **不调 LLM**（不要真实 API 请求）
3. **不起服务**（不要 uvicorn / TestClient）
4. **只用标准库 `unittest`**，不要引入 pytest 或其他依赖
5. 测试文件放 `backend/tests/test_planner.py`，文件末尾用 `unittest.main(verbosity=2)`，使其可直接 `venv/Scripts/python tests/test_planner.py` 运行
6. 文件头用 `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` 让 `app.*` 可导入
7. 构造测试数据用 `types.SimpleNamespace`（**不要**用 ORM 模型，那会触发 DB）
8. 断言要精确（用 `assertEqual` 明确期望值），不要只断言 `is not None`

## 被测函数的语义（先读代码确认，以代码为准）

`planner.py`：

| 函数 | 语义 | 需要注意 |
|---|---|---|
| `grade_attraction(item)` | 按 `visit_minutes` 分级：≤90 小 / ≤180 中 / 否则大 | 缺字段时默认按小处理 |
| `meal_minutes(end_hhmm)` | 当天预留的用餐分钟：能赶上晚饭返回 135，否则只 60 | 判据是 `end - (75+10) >= 17:00` |
| `day_budget(req, day, total)` | 当天「游览+赶路」可用分钟 = 时间窗 − 当天餐时 | **绝不能乘任何倾向系数**（曾经乘了 ×1.15 导致最后一天超时） |
| `time_rule(item)` | 时段约束。**优先读 `item.best_time` 字段**（morning/night/museum），字段为空才回退到名称关键词 | 非法 `best_time` 值应被忽略并回退 |
| `_is_must(item)` | 「必去」= `must_visit` 为真 或 `heat >= MUST_HEAT`（常量 = **500**，**不是**旧尺度的 90） | |
| `haversine_m(...)` | 球面距离（米） | |
| `travel_matrix(city, items, transport)` | 两两通行时间，键统一为 `(小 id, 大 id)` | 不能出现 `(5,2)` 这种反序键 |
| `cluster_attractions(city, items, transport, threshold_m)` | 按**直线距离**聚类成片区簇（≤1200m 并簇） | 用最近邻成链后按距离断开 |
| `estimate_days_needed(city, items, transport)` | 「按片区排最舒服需要几天」= 独占型个数 + 簇数 | 只作提示，不是硬判定 |
| `_group_load(city, group, transport)` | 一天的实际占用 = **游览 + 路程**（含最后回中心） | 必须大于纯游览时长 |
| `pick_attractions(city, items, days, pace, transport)` | 一键 AI 挑景点：**必去优先 → 热度其次**，累计游览逼近 `days × PACE_TARGET_MINUTES[pace]`，超 10% 收口 | 景点多时必须收口，不能全选 |
| `_is_standalone(city, item, transport)` | 独占型 = 游览 ≥240 分 或 单程 ≥45 分 | |

`llm.py`：

| 函数 | 语义 |
|---|---|
| `_validate_days(city, req, items, groups)` | LLM 分天的硬校验，返回错误文案或 `None`。5 条规则见下 |
| `_matrix_text(city, items, req)` | 渲染通行时间矩阵文本，首行是表头（含全部 id） |
| `_items_text(city, items, req)` | 渲染景点清单，必去标「必去」、远郊标「远郊」 |

`_validate_days` 的五条规则（每条都要单独一个用例）：

1. 景点 id 不重不漏，且只能用输入里给的
2. `len(groups)` 必须等于 `req.days`
3. 不允许空天
4. **紧邻（直线 ≤ `CLUSTER_MAX_METERS`）必须同天** —— 拆开要报错且错误文案含「同一天」
5. 独占型不能和别人同天（错误文案含「独占」）；每天 `_group_load` 不能超 `day_budget × 1.05`

## 重点场景（这些是踩过坑的地方，必须覆盖）

1. `day_budget` 不随 pace 变化（relaxed / packed 结果相同）
2. 最后一天 16:00 返回 → `meal_minutes` 只给 60 分（不排晚餐）
3. `time_rule` 的字段优先与关键词兜底两条路径
4. 「锦里古街 ↔ 武侯祠」这种 200 米级的景点必须同簇 / 同天
5. 远郊大景点（如都江堰）必须独占一天
6. `_validate_days` 把「紧邻景点拆到不同天」判为非法

## 参考

仓库里已有一个基线版本：`backend/tests/test_planner.py`（**122** 个用例，全绿）。
**JS 侧镜像引擎也有基线**：`frontend/tests/`（**84** 个用例，`npm test` 用 node --test 跑，零依赖）——
改 `planner.py` 或 `frontend/src/engine/*.js` 后两边都要跑，另见 [`OFFLINE.md`](OFFLINE.md)。
你可以**在它基础上补充/重写**，也可以另起一份，但覆盖度不要低于它，且应该包含更多边界值
（空列表、单元素、边界值 90/91/180/181、极短/极长时间窗、跨零点时间窗如 `09:00 → 01:00`）。

## 交付

1. 完整的测试文件（可直接运行）
2. 运行命令与**实际输出**（贴出来）
3. 一个简短说明：哪些函数覆盖了、哪些故意没覆盖及原因（比如需要 DB 或网络的）

如果发现被测函数的**行为与上面描述不一致**，不要改测试去迁就 —— 明确指出差异，那可能是真 bug。

===== 提示词结束 =====
