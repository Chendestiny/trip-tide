<template>
  <nav class="tabbar">
    <router-link v-for="t in tabs" :key="t.to" :to="t.to" :class="{ on: active === t.key }">
      <span class="ic">{{ t.icon }}</span>{{ t.label }}
    </router-link>
  </nav>
</template>

<script setup>
// 底部标签栏（App 尺寸下比侧边导航更合适）。
// 并入 my-website 时可整体替换成站点的 SiteNav，本组件不承载业务逻辑。
defineProps({
  active: { type: String, default: 'home' },
})

const tabs = [
  { key: 'home', icon: '🧭', label: '规划', to: '/trip' },
  { key: 'me', icon: '👤', label: '我的', to: '/trip/me' },
]
</script>
