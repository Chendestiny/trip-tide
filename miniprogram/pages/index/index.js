// 首页 —— 目前是**连通性自检页**，用来确认小程序能打通后端。
// 正式的「目的地宫格」页按 docs/MINIPROGRAM.md 的映射表从 views/trip/TripHome.vue 搬过来。

import { getCities, health } from '../../utils/request.js'
import { BASE_URL } from '../../config.js'

Page({
  data: {
    baseUrl: BASE_URL,
    statusText: '检测中…',
    healthOk: false,
    cities: [],
    loading: true,
    error: '',
  },

  onLoad() {
    this.check()
  },

  check() {
    const that = this
    this.setData({ loading: true, error: '', statusText: '检测中…' })

    health()
      .then(function (h) {
        that.setData({
          healthOk: true,
          statusText:
            '后端已连通 · ' + (h.app || '') + ' · ' +
            'AI ' + (h.llm_ready ? '已就绪' : '未配置（走规则引擎）') + ' · ' +
            (h.db || '') + ' · ' + (h.model || ''),
        })
      })
      .catch(function (e) {
        that.setData({ healthOk: false, statusText: '后端未连通：' + e.message })
      })

    getCities()
      .then(function (list) {
        that.setData({ cities: list || [], loading: false })
      })
      .catch(function (e) {
        that.setData({ error: e.message, loading: false })
      })
  },
})
