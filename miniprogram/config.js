// 后端地址配置。
//
// ⚠️ 小程序端**必须**用绝对地址 —— Web 版走 vite 代理（/api → 127.0.0.1:8002），
// 小程序没有代理这一层，所以 BASE_URL 要写全。

// 模拟器调试：127.0.0.1 即可。前提是开发者工具里勾了
//   「详情 → 本地设置 → 不校验合法域名、web-view（业务域名）、TLS 版本以及 HTTPS 证书」
//   （本项目已在 project.config.json 里把 setting.urlCheck 设为 false，默认就是关的）
const DEV_HOST = 'http://127.0.0.1:8002'

// 真机调试：127.0.0.1 指的是**手机自己**，必须换成电脑的局域网 IP，例如：
//   const DEV_HOST = 'http://192.168.1.5:8002'
// 同时后端要用 `python run.py --host 0.0.0.0` 起 —— run.py 默认只绑 127.0.0.1，手机连不上。

// 上架前：换成 https + 已备案域名，并在小程序后台
//   「开发 → 开发设置 → 服务器域名」把它加进 request 合法域名。

export const BASE_URL = DEV_HOST
