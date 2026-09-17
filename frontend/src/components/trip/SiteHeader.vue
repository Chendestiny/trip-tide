<template>
  <header class="site-head">
    <div class="site-head-in">
      <router-link to="/trip" class="brand">
        <span class="brand-mark" aria-hidden="true">
          <!-- 罗盘图标（内联 SVG，避免字体字形缺失时渲染成方框/错字） -->
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="9" />
            <path d="M15.6 8.4 13.2 13.2 8.4 15.6 10.8 10.8z" />
          </svg>
        </span>
        <span>AI 旅行搭子</span>
      </router-link>
      <nav class="site-nav">
        <router-link to="/trip">规划</router-link>
        <router-link to="/trip/me">我的行程</router-link>
      </nav>
    </div>
  </header>
</template>

<script setup>
// 顶部导航：只在宽屏显示（手机形态用底部标签栏，见 TripTabBar）。
// 显示/隐藏由 style.css 的媒体查询控制，这里不做视口判断。
</script>
