import { BASE_URL } from './config.js'

App({
  globalData: {
    baseUrl: BASE_URL,
  },

  onLaunch() {
    // 本地调试时这行最有用：确认小程序到底在打哪个后端
    console.log('[AI 旅行搭子] 后端地址 =', BASE_URL)
  },
})
