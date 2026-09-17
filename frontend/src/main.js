import { createApp } from 'vue'
import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import App from './App.vue'
import TripHome from './views/trip/TripHome.vue'
import TripPick from './views/trip/TripPick.vue'
import TripPlan from './views/trip/TripPlan.vue'
import TripMe from './views/trip/TripMe.vue'
import './style.css'

const SITE = 'AI 旅行搭子'

/**
 * 兜底：任何渲染期异常都显示成可见的错误面板，而不是一片白。
 *
 * 白屏是最难排查的故障形态——编译不报错、接口全 200、控制台可能只有一行 Vue 警告。
 * 本项目已经踩过一次（router-view 套 transition），所以这里宁可丑一点也要把错误摆出来。
 */
function renderFatal(err, info = '') {
  const root = document.getElementById('app')
  if (!root) return
  // 页面已经渲染出内容就不要再盖上去，避免把局部错误放大成整页错误
  if (root.innerText && root.innerText.trim().length > 40) return

  const msg = (err && (err.stack || err.message)) || String(err)
  root.innerHTML = `
    <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;
                padding:24px;background:#f2f4f3;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
      <div style="max-width:640px;width:100%;background:#fff;border:1px solid #e5eae8;border-radius:16px;
                  padding:26px 28px;box-shadow:0 8px 30px -12px rgba(16,29,35,.18)">
        <div style="font-size:19px;font-weight:700;color:#101d23;letter-spacing:-.02em">页面渲染出错</div>
        <p style="margin:10px 0 16px;font-size:13.5px;line-height:1.7;color:#4b5a62">
          这不是接口问题（后端可能一切正常）。常见原因是前端模块热更新处于坏状态，
          <b>按 Ctrl/Cmd + Shift + R 强制刷新</b>通常即可恢复。
        </p>
        <div style="font-size:12px;color:#8a99a1;margin-bottom:6px">错误信息${info ? `（${info}）` : ''}</div>
        <pre style="margin:0 0 18px;padding:12px 14px;background:#f8faf9;border:1px solid #e5eae8;border-radius:10px;
                    font-size:12px;line-height:1.6;color:#c4534f;white-space:pre-wrap;word-break:break-word;
                    max-height:220px;overflow:auto">${String(msg).slice(0, 1200)}</pre>
        <button onclick="location.reload()"
                style="padding:10px 20px;border:none;border-radius:10px;background:#0d7d86;color:#fff;
                       font-size:14px;font-weight:600;cursor:pointer">重新加载</button>
      </div>
    </div>`
}

const router = createRouter({
  // 离线版是单个 HTML 文件（workbuddy.link/p/{id} 或双击打开），
  // 深链/刷新时静态托管找不到 /trip/plan 这个路径 → 必须用 hash 路由。
  history: __OFFLINE__ ? createWebHashHistory() : createWebHistory(),
  routes: [
    { path: '/', redirect: '/trip' },
    { path: '/trip', name: 'trip-home', component: TripHome, meta: { title: SITE } },
    { path: '/trip/pick', name: 'trip-pick', component: TripPick, meta: { title: '选景点 · ' + SITE } },
    { path: '/trip/plan', name: 'trip-plan', component: TripPlan, meta: { title: '行程方案 · ' + SITE } },
    { path: '/trip/me', name: 'trip-me', component: TripMe, meta: { title: '我的行程 · ' + SITE } },
    { path: '/:pathMatch(.*)*', redirect: '/trip' },
  ],
  scrollBehavior() {
    return { top: 0 }
  },
})

router.afterEach((to) => {
  document.title = to.meta.title || SITE
})

// 路由懒加载/组件解析失败也要可见
router.onError((err) => {
  console.error('[AI 旅行搭子] 路由错误', err)
  renderFatal(err, 'router')
})

const app = createApp(App)

app.config.errorHandler = (err, _instance, info) => {
  console.error('[AI 旅行搭子] 渲染错误', err, info)
  renderFatal(err, info)
}

app.use(router).mount('#app')

// 兜底：非 Vue 管的异常（例如某个模块顶层就抛错）
window.addEventListener('error', (e) => renderFatal(e.error || e.message, 'window'))
window.addEventListener('unhandledrejection', (e) => renderFatal(e.reason, 'promise'))
