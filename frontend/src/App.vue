<template>
  <div class="site">
    <SiteHeader />
    <div class="site-main">
      <router-view />
    </div>
  </div>
</template>

<script setup>
// AI 旅行搭子 根组件。
// 注意：这里**不要**给 <router-view> 套 <transition mode="out-in">。
// 实测踩过坑——在 <router-view v-slot> 下配合过渡时，离场动画不完成会导致
// 新页面永不挂载：路由已变、router-view 渲染成空注释，表现就是白屏，
// 而直接打开/硬刷新同一个 URL 却完全正常（因为不经过过渡）。
// 排查手段见 scripts/smoke_ui.py。
//
// 并入 my-website 时本文件不需要改动 —— 只要在它的 main.js 里注册 /trip 路由，
// 并把 PhoneShell / SiteHeader 换成站点外壳即可。
import SiteHeader from './components/trip/SiteHeader.vue'
</script>
