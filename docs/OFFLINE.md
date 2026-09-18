# 离线单文件版（`OFFLINE.md`）

> 起因：参赛/发布要的是**一个能直接打开的网页**，而平台（WorkBuddy 资料库「发布为网站」）
> 只托管静态文件 —— 没有后端、没有数据库、没有模型调用。
> 而本项目原本是 `Vue + FastAPI + 远端 MySQL + DeepSeek` 的四件套。
>
> 这份文档说明「同一份源码怎么同时产出联机版和离线版」，以及**改动时两边怎么不跑偏**。

---

## 1. 两种构建形态

| | 联机版 | 离线版（单文件） |
|---|---|---|
| 命令 | `cd frontend && npm run build` | `cd frontend && npm run build:offline` |
| 产物 | `frontend/dist/` | `frontend/dist-offline/` → 内联成**一个 HTML** |
| 接口实现 | `src/trip-api.js`（请求 `/api/trip`） | `src/trip-api-local.js`（浏览器内算） |
| 数据 | 远端 MySQL（68 目的地 / 899 景点 / 1724 子景点） | `src/engine/offline-data.json`（构建期从种子生成） |
| 排程 | `backend/app/trip/planner.py` | `src/engine/*.js`（planner.py 的镜像） |
| 模型调用 | DeepSeek 并行流水线 + 终检 | 无（全走规则引擎） |
| 路由 | `createWebHistory` | `createWebHashHistory`（静态托管下 history 深链会 404） |
| 存储 | MySQL | `localStorage`（方案 + 历史） |

**视图层（`views/trip/*.vue`）一行都不分叉**：它们统一 `import ... from '@api'`，
由 `frontend/vite.config.js` 的别名按构建模式指到具体实现；`__OFFLINE__` 是编译期常量，
只用在「路由模式」这种必须知道形态的地方。

---

## 2. 文件地图

```
frontend/src/
├── engine/                     ← 排程引擎（planner.py 的 JS 镜像，纯函数、零依赖）
│   ├── consts.js               ← 所有常量（与 planner.py 一一对应，改一边必须改两边）
│   ├── pyfmt.js                ← Python 的 round / 定点格式化语义（见 §4）
│   ├── geo.js                  ← haversine / 交通方式与耗时
│   ├── rules.js                ← 时段规则、分级、必去判定、每日预算
│   ├── planner.js              ← 裁剪 / 聚类分天 / 逐天裁剪 / 住宿 / 时间轴物化
│   ├── preview.js              ← 紧凑度预估 + 倾向微调
│   └── offline-data.json       ← 构建期生成（勿手改）
├── tests/                      ← **引擎单元测试**（node --test，零依赖；39 用例）
│   ├── engine-basics.test.js   pyfmt / 地理交通 / 判定规则
│   ├── engine-planner.test.js  聚类 / 裁剪 / 物化不变量 / plan_fallback
│   └── engine-preview.test.js  region 回归（莫高窟）+ 预估 + 微调冒烟
├── trip-api.js                 ← 联机接口实现
└── trip-api-local.js           ← 离线接口实现（与上面同名导出，可整块替换）

scripts/
├── build_offline_data.py       ← 种子 JSON → offline-data.json
├── build_single_html.py        ← dist-offline/ → 单个 HTML（正式版）
└── build_mock_html.py          ← engine + 数据 → mock 单个 HTML（快速验证件，无需 npm）

local/（gitignore，只在本机用 —— 这些是**回归工具**，不是临时脚本，别删）
├── offline-cases.json          ← 一致性对照的用例表
├── offline-dump-py.py          ← 用真后端引擎跑用例
├── offline-dump-js.mjs         ← 用浏览器引擎跑同样的用例
├── offline-diff.py             ← 逐节点比对（12 用例 / 195 节点）；期望输出 PASS
├── check-preview-region.mjs    ← region 预估回归（莫高窟 / 云南 场景）
└── check-region-flow.py        ← mock 单文件端到端（无头 Edge，file://）
```

---

## 3. 重建流程（改完东西要跑什么）

```bash
# ① 改过种子数据 / 首页排序口径 → 重新生成内联数据
cd backend && venv/Scripts/python ../scripts/build_offline_data.py

# ② 改过 planner.py（或 engine/*.js）→ 引擎单测 + 一致性对照，必须全绿
cd frontend && npm test                                     # 39 个引擎单元测试
cd ../backend && venv/Scripts/python ../local/offline-dump-py.py
node ../local/offline-dump-js.mjs                # 用 managed node 跑即可
venv/Scripts/python ../local/offline-diff.py     # 期望输出 PASS
node ../local/check-preview-region.mjs           # region 预估回归（莫高窟）

# ③ 出单文件 HTML
cd frontend && npm run build:offline
cd ../backend && venv/Scripts/python ../scripts/build_single_html.py
# → dist/trip-buddy.html（单文件，双击可开，也可传资料库「发布为网站」）
```

`build_single_html.py` 只是把 `index.html` 里的 `<script src>` / `<link rel=stylesheet>`
换成内联（并把 `</script>` 转义，避免提前截断），**不做打包** ——
依赖关系已经在 Vite 那一步解决完了。所以离线构建必须关掉 chunk 拆分
（`vite.config.js` 里的 `inlineDynamicImports`），否则内联进去的脚本再 import 相对路径会 404。

### 3.5 快速验证件：mock 单文件（不需要 npm）

不想跑 npm 也有一条捷径：`scripts/build_mock_html.py` 把**同一份引擎 + 同一份数据**
注入 `frontend/mock/mock.html`（一份手写的原生 JS 薄壳 UI，三视图：宫格 → 勾景点 → 时间轴），
产出 `dist/trip-buddy-mock.html`（约 790 KB），双击即可打开。

```bash
cd backend && venv/Scripts/python ../scripts/build_mock_html.py
# 验证：本机 local/check-mock.py（无头 Edge 跑三个视图 + 截图）
```

| | mock 版（`build_mock_html.py`） | 正式版（`build:offline` + `build_single_html.py`） |
|---|---|---|
| UI | 手写原生 JS 薄壳 | 真实 Vue 页面（与联机版完全同一套） |
| 算法/数据 | **同一份** engine + offline-data.json | 同左 |
| 依赖 | 只需 Python | 需要 npm install + vite build |
| 用途 | 快速看效果 / 临时演示 | 对外发布 |

⚠️ 两者的**算法与数据是同一份**，所以行程一致；差别只在 UI 壳。
改了 `engine/*.js` 或种子数据，两个产物都要重出。

---

## 4. 一致性约束（这是最容易踩的地方）

`engine/*.js` 是**翻译**不是**重写**。任何一处「顺手优化」都会让离线版和联机版给出不同的行程。

| 坑 | 说明 |
|---|---|
| **`Math.round` / `toFixed` 不是 Python 的舍入** | Python `round(742.5) = 742`、`f"{6.25:.1f}" = "6.2"`（五取偶）；JS 分别是 743 / "6.3"。**实测就是靠这个抓出来的**：总结里「日均游览约 742 分钟」两侧差 1。所以数值一律走 `pyfmt.js` 的 `pyRound` / `pyFixed` |
| **JS 数组比较是字符串比较** | `[560] > [98]` 会被算成 `"560" > "98"` = false。`planner.js` 里的 `argMax` 因此实现了**逐元素数值比较**（Python 的元组 key 语义） |
| **`max()` / `min()` 并列时取第一个** | `argMax` 用 `>` 而不是 `>=`；裁剪时用 `min` 语义选第一个最小值 |
| **Pydantic 会补默认值，JS 不会** | 后端 `TimelineNode` 没显式给的字段带默认值（`travel_mode=""`、`must_visit=false`…），JS 普通对象缺键会在 JSON 里直接消失。`normalizeNode()` 负责补齐，让两侧节点形状逐键一致 |
| **`spots` 排序** | `spotAdvice()` 按 `(order_index, id)` 排；生成器给子景点编的顺序必须和库里一致 |

对照脚本比对的是「看得见的东西」：天数 / 主题 / 每日节点的时间·类型·名称·停留·路程·交通方式 /
饭点位置 / 酒店片区 / 舍弃清单 / 总结 / 紧凑度预估（含 suggestion 文案）。
id 两侧各自编号，不参与比较。

---

## 5. 与联机版的**固定差异**（刻意的，不是 bug）

| 项 | 离线版行为 | 为什么 |
|---|---|---|
| `source` | 恒为 `fallback` → 界面显示「规则引擎生成」 | 不撒谎。没有模型参与就不能标「AI 生成」 |
| `review` | 恒为 `null` → 方案说明里没有「AI 复核意见」 | 没有第二遍复核 |
| 方案存储 | `localStorage`（最多 20 份），`getPlan(id)` 找不到就报错 | 没有服务端兜底 |
| 首页提示条 | 显示「离线演示版 · 路线由本地规则引擎在你的浏览器里算出」 | `health()` 返回 `db: 'offline'`，与「未配置 Key」区分开 |
| 地图 | 没有 JS Key 时降级成 SVG 示意路线（标「示意地图」） | 见 `TripMap.vue` 的 `useAmap` 分支；这是现成能力，不是离线版新增的 |

---

## 6. 体积

| 部分 | 大小 |
|---|---|
| `offline-data.json`（68 目的地 + 899 景点 + 1724 子景点，含 `spot_count` / `distance_km`） | **700 KB** |
| Vue + Vue Router + 引擎 + 样式（构建产物） | 约 150~200 KB |
| **最终单文件 HTML** | **约 0.9~1 MB** |

单文件 1MB 在手机 4G 下也是秒开级别（无外部请求、无字体、无图片）。
要更小就把 `spots` 砍掉（省约 275 KB），但景点详情弹窗会空掉。
