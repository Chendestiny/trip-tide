// 视口断点（与 style.css 的媒体查询保持一致）
//
// 大部分布局差异用纯 CSS 就能搞定，不需要 JS。但有两处必须知道当前形态：
//   · 景点池的设置面板：宽屏常驻在右侧栏，手机藏在底部浮条的折叠里
//   · 行程页的地图按钮：宽屏放在页头，手机放在底部浮条
// 这两处用 useIsWide() 显式分支，比用 CSS 强改 v-show 可读得多。

import { onUnmounted, ref } from 'vue'

const MOBILE_MAX = 640

export function useMediaQuery(query) {
  const supported = typeof window !== 'undefined' && 'matchMedia' in window
  const mql = supported ? window.matchMedia(query) : null
  // 同步初始化，避免首帧闪一下错误形态
  const matches = ref(mql ? mql.matches : false)

  const update = (e) => { matches.value = e.matches }
  mql?.addEventListener('change', update)
  onUnmounted(() => mql?.removeEventListener('change', update))

  return matches
}

/** 是否宽屏（≥641px，网页版布局） */
export const useIsWide = () => useMediaQuery(`(min-width: ${MOBILE_MAX + 1}px)`)
