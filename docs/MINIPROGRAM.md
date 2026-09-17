# 小程序端

> **一句话**：小程序端**新增**在 `miniprogram/`，`frontend/` 和 `backend/` 一行不动。
> 两端**各写各的**，不共享代码 —— 简单、好懂，代价是少量重复（见 §2）。

---

## 1. 为什么是「新增」而不是「替换成 uni-app」

uni-app 的价值是「一套代码两端」。但如果 Web 版保持现状，uni-app 就只是给小程序用的
第二个工程——和原生小程序比没有明显优势，还多一层构建。而本项目：

- 前端**无 UI 框架、自绘**，DOM 结构极简（`page-root` 单根 + `style.css` 35 个 CSS 变量），
  「HTML→WXML」本来就是机械转换（见 [`FRONTEND.md`](FRONTEND.md#迁移到微信小程序的映射v1-已按此设计)）
- 迁移到 uni-app 要把 `vue-router` 换成 `pages.json`、入口换成 `App.vue`、根 `npm run dev` 编排重写，
  **迁移期间 Web 版会停摆**——没法边写小程序边对照

所以：`frontend/` 继续跑，小程序另起一个目录。等小程序稳定后，再评估要不要合并（那时引 uni-app 也不迟）。

---

## 2. 两个文件夹，各写各的

**UI 层复用不了**——WXML ≠ HTML（`<div>` → `<view>`、`<span>` → `<text>`），
WXSS 选择器受限，事件从 `@click` 变 `bindtap`。没有任何机制能让原生小程序直接渲染 `.vue`。

**逻辑层曾经抽过一个 `shared/` 真源目录**（Web 走 vite alias、小程序走脚本同步副本），
但那条路要额外维护一个同步脚本 + 校验命令，对当前体量来说不划算，**已拆掉**。

现在的规则很简单：

> `frontend/` 和 `miniprogram/` 各自完整、各自独立。改一边不影响另一边。

**代价是少量重复**，只有这几处，改的时候记得两边一起改：

| Web | 小程序 | 重复的内容 |
|---|---|---|
| `frontend/src/trip-theme.js` | `miniprogram/utils/theme.js` | 按天配色、节点图标、交通/节奏标签 |
| `frontend/src/trip-api.js` | `miniprogram/utils/request.js` | 接口路径、超时、错误文案归一化 |
| `frontend/src/trip-store.js` | `miniprogram/utils/store.js` | 历史记录上限、去重、轻量投影 |
| `frontend/src/main.js` | `miniprogram/app.js` + `app.json` | 路由表、全局错误兜底 |
| `frontend/src/views/trip/*.vue` | `miniprogram/pages/*/` | 页面本身 |

> 存储键 `triptide.history.v1` 两边都用同一个名字，**不要改**——改了用户历史全丢。
> （两端的 storage 是独立命名空间，同名不会冲突，也不会互通。）

---

## 3. 目录结构

```
trip-tide/
├── frontend/                    ← Web 版，独立完整
│   └── src/
│       ├── trip-theme.js        视觉常量
│       ├── trip-api.js          接口封装（fetch）
│       ├── trip-store.js        本地历史（localStorage + vue reactive）
│       └── views/trip/*.vue     4 个页面
└── miniprogram/                 ← 小程序版，独立完整
    ├── app.js / app.json / app.wxss
    ├── config.js                后端地址（模拟器 / 真机 / 线上三档）
    ├── project.config.json      appid、urlCheck 等
    ├── utils/
    │   ├── theme.js             视觉常量（trip-theme.js 的副本）
    │   ├── request.js           接口封装（wx.request）
    │   └── store.js             本地历史（wx.storage + 手写订阅）
    └── pages/
        └── index/               连通性自检页（待换成正式宫格）
```

---

## 4. 本地开发怎么跑

### 4.1 起后端

```bash
cd backend && venv/Scripts/python run.py          # 模拟器调试够用，默认绑 127.0.0.1:8002
cd backend && venv/Scripts/python run.py --host 0.0.0.0   # 真机调试必须这样起
```

### 4.2 开发者工具

1. 导入项目，目录选 **`miniprogram/`**（不是仓库根目录）
2. `project.config.json` 里 `appid` 现在是占位值 `touristappid`，**换成你的 AppID**
3. 域名校验已经默认关掉了（`setting.urlCheck: false`）——对应界面上
   「详情 → 本地设置 → 不校验合法域名、web-view（业务域名）、TLS 版本以及 HTTPS 证书」
4. 打开首页应看到「后端已连通」+ 目的地列表

### 4.3 真机调试

- `miniprogram/config.js` 的 `DEV_HOST` 换成**电脑的局域网 IP**（`127.0.0.1` 在手机上指手机自己）
- 后端要用 `--host 0.0.0.0` 起
- 手机微信里也要开一次「不校验域名」

### 4.4 上架前

- `DEV_HOST` 换成 https + 已备案域名
- 小程序后台「开发 → 开发设置 → 服务器域名」把域名加进 **request 合法域名**
- 如果要调腾讯位置服务的数据接口（地址解析/路线规划），还要把 `https://apis.map.qq.com` 加进去，
  并且 Key 上配了域名白名单时要把 `servicewechat.com` 一并加入（见 `local/pending-keys.md`）

---

## 5. 页面映射

沿用 [`FRONTEND.md`](FRONTEND.md#迁移到微信小程序的映射v1-已按此设计) 那张表，不再抄一份：

| Web | 小程序 | 状态 |
|---|---|---|
| `views/trip/TripHome.vue` | `pages/index/index` | 🟡 现为连通性自检页，待搬正式宫格 |
| `views/trip/TripPick.vue` | `pages/pick/pick` | ⬜ 待做 |
| `views/trip/TripPlan.vue` | `pages/plan/plan` | ⬜ 待做 |
| `views/trip/TripMe.vue` | `pages/me/me` | ⬜ 待做 |
| `trip-api.js` | `utils/request.js` | ✅ 已建 |
| `trip-store.js` | `utils/store.js` | ✅ 已建 |
| `trip-theme.js` | `utils/theme.js` | ✅ 已建 |
| `TripMap.vue`（高德 JS API / SVG） | `<map>` 组件 | ⬜ 待做，**必须重写** |
| `style.css` 的 35 个 CSS 变量 | WXSS 无变量机制 | ⬜ 样式各写各的，只共用颜色常量 |
| `PhoneShell.vue` 手机壳 | — | 真机上要去掉，不需要 |

> **地图是唯一必须重写的组件**：`TripMap.vue` 依赖高德 JS API + `window._AMapSecurityConfig`，
> 小程序没有 DOM，高德 JS API 根本用不了。好在全库坐标已是 GCJ-02，腾讯 `<map>` 直接吃（铁律 4）。
> 注意 `<map>` 组件的 marker / polyline **不需要 key**。

---

## 6. ⚠️ 一个待验证的假设

`miniprogram/` 下的 `.js` 全部用 **ESM** 写法（`export const` / `import`）。
微信官方文档以 `module.exports`（CommonJS）为主，但开发者工具支持 ES6 模块语法并会转译。

**第一次在开发者工具里打开时请留意**：如果报模块语法相关错误，说明这个假设不成立。
那时的修法是把 `miniprogram/` 下的 `export` / `import` 改成 `module.exports` / `require`
（只动小程序这一侧，Web 版不受影响——这正是「两个文件夹各写各的」的好处）。
先按现状试，不要提前加复杂度。

---

## 7. 待办

- [ ] 4 个页面按映射表搬过来（index 先做正式宫格）
- [ ] `TripMap.vue` → `<map>` 组件重写（景点点位 + 按天连线，免 key）
- [ ] 小程序头像 / 简称 / 介绍（已定：名称「智能旅行搭子」、简称「旅行搭子」）
- [ ] AIGC 显式标识（`/plan` 返回 + 前端展示）—— 上架前必须
- [ ] 登录 + 限次（`trip_user` 表 + `/auth/wx-login` + `/quota/check`）
