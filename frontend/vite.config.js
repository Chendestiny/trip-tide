import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 两种构建形态共用一份源码：
 *
 * · 默认（`npm run build`）—— 联机版：视图层走 `@api` → `src/trip-api.js`，请求后端 `/api/trip`。
 * · 离线版（`npm run build:offline`，即 `--mode offline`）—— 单文件 HTML 发布形态：
 *   视图层走 `@api` → `src/trip-api-local.js`，数据内联、排程在浏览器里算，**不碰任何外部服务**。
 *
 * 视图代码一行都不用改，靠下面这个别名切实现。`__OFFLINE__` 是编译期常量，
 * 只用于「路由用 hash 模式」这类必须知道形态的地方（tree-shaking 会把另一支干掉）。
 */
export default defineConfig(({ mode }) => {
  const offline = mode === 'offline'

  return {
    plugins: [vue()],

    define: {
      __OFFLINE__: JSON.stringify(offline),
    },

    resolve: {
      alias: {
        '@api': fileURLToPath(
          new URL(offline ? './src/trip-api-local.js' : './src/trip-api.js', import.meta.url),
        ),
      },
    },

    /**
     * 只让依赖预打包去扫 `index.html`。
     *
     * 默认值是**爬取根目录下所有 `**\/*.html`**，而 `mock/mock.html` 是「构建期模板」不是页面：
     * 它内联的脚本里有两处占位符（`const DATA = /*__DATA__*\/;` 与 `/*__ENGINE__*\/`），
     * 由 `scripts/build_mock_html.py` 在构建时做字符串替换后才成为合法 JS。
     * 原文里的 `/*__DATA__*\/` 是注释、等于 `const DATA = ;` —— esbuild 解析直接报
     * `Unexpected ";"`，**整个依赖扫描失败**。
     *
     * 后果不是「日志里多一行红字」：扫描失败意味着这次启动没有预打包结果，
     * 首次打开页面时才边跑边优化，Vite 随即触发一次重载 —— 表现就是
     * **首次加载报错、刷新一次就正常**（且每次重启 dev server 必然复现）。
     */
    optimizeDeps: {
      entries: ['index.html'],
    },

    // 离线产物会被内联成一个 HTML 文件并可能挂在子路径下（workbuddy.link/p/{id}），
    // 用相对路径最稳；产物单独出到 dist-offline/，别和联机版混在一起。
    base: offline ? './' : '/',

    build: offline
      ? {
          outDir: 'dist-offline',
          emptyOutDir: true,
          // 图标、字体之类一律内联成 data URI —— 单文件形态下没有「旁边的资源文件」这回事
          assetsInlineLimit: 100000000,
          // 单文件形态不能有 chunk 拆分：内联的 <script> 里再 import 相对路径会 404
          rollupOptions: { output: { manualChunks: undefined, inlineDynamicImports: true } },
        }
      : {},

    server: {
      port: 5176,
      // 开发时把 /api 请求代理到后端（本项目的后端固定跑在 8002）。
      // ⚠️ 别在这里加 proxy error 重试：Vite 自己的 error 监听器注册在 `configure` 之后，
      //    同一个 tick 里就会把 500 写掉（见 vite/dist/node/chunks 里 proxyMiddleware），
      //    代理层抢不过它。后端冷启动的重试在 `src/trip-api.js`。
      proxy: {
        '/api': {
          target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8002',
          changeOrigin: true,
        },
      },
    },
  }
})
