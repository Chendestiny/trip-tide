<template>
  <div class="shell">
    <header v-if="title || back || $slots.right" class="shell-head">
      <button v-if="back" class="back-link" aria-label="返回" @click="onBack">
        <svg class="back-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M14.6 5.4 L7.8 12 L14.6 18.6" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" /></svg><span class="back-text">返回</span>
      </button>
      <div>
        <span class="shell-title">{{ title }}</span>
        <span v-if="sub" class="shell-sub">{{ sub }}</span>
      </div>
      <div class="shell-head-right"><slot name="right" /></div>
    </header>

    <div class="shell-body" ref="bodyEl"><slot /></div>

    <slot name="footer" />
  </div>
</template>

<script setup>
// 应用壳：宽屏时是 1120px 居中容器 + 页面标题行；手机时是铺满全屏的 App 外壳。
// 布局差异全部由 style.css 的媒体查询控制，本组件不做判断。
import { ref } from 'vue'
import { useRouter } from 'vue-router'

const props = defineProps({
  title: { type: String, default: '' },
  sub: { type: String, default: '' },
  /** true 用 router.back()，字符串则 push 到该路径 */
  back: { type: [Boolean, String], default: false },
})

const emit = defineEmits(['back'])
const router = useRouter()
const bodyEl = ref(null)

function onBack() {
  emit('back')
  if (typeof props.back === 'string') router.push(props.back)
  else router.back()
}

defineExpose({
  scrollToTop: () => bodyEl.value?.scrollTo({ top: 0 }),
})
</script>
