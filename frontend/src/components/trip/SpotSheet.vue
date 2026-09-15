<template>
  <div v-if="open" class="spot-mask" @click.self="$emit('close')">
    <div class="spot-sheet">
      <button class="spot-x" aria-label="关闭" @click="$emit('close')">✕</button>

      <template v-if="att">
        <header class="spot-head">
          <h3>{{ att.name }}</h3>
          <div class="spot-chips">
            <span v-if="att.district" class="chip">{{ att.district }}</span>
            <span class="chip primary">⏱ 建议 {{ fmtMin(att.visit_minutes) }}</span>
            <span class="chip hot">🔥 {{ att.heat }}</span>
            <span v-if="bestTimeLabel" class="chip">{{ bestTimeLabel }}</span>
            <span v-for="t in (att.tags || []).slice(0, 3)" :key="t" class="chip">{{ t }}</span>
          </div>
          <p class="spot-intro">{{ att.intro }}</p>
        </header>

        <div class="spot-body">
          <div class="spot-sec-title">内部点位与攻略 <span>按建议游览顺序</span></div>

          <div v-if="loading" class="spot-loading">加载中…</div>
          <p v-else-if="!spots.length" class="spot-empty">
            这个景点还没有整理内部点位。整体建议：{{ att.intro || '安排建议时长内慢慢逛即可。' }}
          </p>

          <div v-else class="spot-list">
            <div v-for="s in spots" :key="s.id" class="spot-item">
              <div class="spot-item-head">
                <i class="spot-no">{{ s.order_index || '•' }}</i>
                <strong>{{ s.name }}</strong>
                <span v-if="s.stay_minutes" class="spot-stay">约 {{ fmtMin(s.stay_minutes) }}</span>
              </div>
              <p v-if="s.guide" class="spot-guide">{{ s.guide }}</p>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  att: { type: Object, default: null },   // AttractionOut
  spots: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
})

defineEmits(['close'])

const BEST_TIME = {
  morning: '⛅ 上午去最佳',
  night: '🌙 夜里去最佳',
  museum: '🏛 闭馆早，别排太晚',
}

const bestTimeLabel = computed(() => BEST_TIME[props.att?.best_time] || '')

const fmtMin = (m) => (m >= 60 ? `${Math.floor(m / 60)}h${m % 60 ? (m % 60) + 'm' : ''}` : `${m}m`)
</script>

<style scoped>
.spot-mask {
  position: absolute; inset: 0; z-index: 80;
  background: rgba(10, 30, 36, 0.42);
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
  display: flex; align-items: flex-end;
  animation: maskIn 0.2s var(--ease);
}
@keyframes maskIn { from { opacity: 0; } }

.spot-sheet {
  position: relative;
  width: 100%; max-height: 76%;
  background: var(--surface);
  border-radius: var(--r-xl) var(--r-xl) 0 0;
  padding: 20px 18px calc(18px + env(safe-area-inset-bottom));
  display: flex; flex-direction: column;
  box-shadow: 0 -12px 44px -12px rgba(10, 30, 36, 0.32);
  animation: sheetUp 0.28s var(--ease);
  overflow: hidden;
}
@keyframes sheetUp { from { transform: translateY(32px); opacity: 0.5; } }

.spot-x {
  position: absolute; right: 14px; top: 14px;
  width: 30px; height: 30px; border-radius: 9px;
  color: var(--ink-3); font-size: 13px;
  background: var(--surface-3);
  transition: background 0.16s var(--ease), color 0.16s var(--ease);
}
.spot-x:hover { background: var(--line-2); color: var(--ink); }

.spot-head { flex-shrink: 0; padding-right: 40px; }
.spot-head h3 {
  margin: 0 0 10px;
  font-size: 21px; font-weight: 720; letter-spacing: -0.02em;
}
.spot-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.spot-intro {
  margin: 11px 0 0;
  font-size: 13.5px; line-height: 1.7; color: var(--ink-2);
}

.spot-body { flex: 1; min-height: 0; overflow-y: auto; margin-top: 16px; }
.spot-sec-title {
  display: flex; align-items: baseline; gap: 8px;
  font-size: 14px; font-weight: 660;
  padding-bottom: 10px; margin-bottom: 4px;
  border-bottom: 1px solid var(--line);
}
.spot-sec-title span { font-size: 11.5px; font-weight: 450; color: var(--ink-4); }

.spot-loading, .spot-empty {
  padding: 26px 0; text-align: center;
  font-size: 13px; color: var(--ink-3); line-height: 1.7;
}

.spot-item { padding: 13px 0; border-bottom: 1px dashed var(--line); }
.spot-item:last-child { border-bottom: none; }
.spot-item-head { display: flex; align-items: center; gap: 9px; }
.spot-no {
  flex-shrink: 0;
  width: 21px; height: 21px; border-radius: 7px;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-style: normal; font-weight: 700;
  color: #fff; background: var(--brand);
}
.spot-item-head strong { font-size: 14.5px; font-weight: 660; }
.spot-stay { font-size: 11.5px; color: var(--ink-3); margin-left: auto; flex-shrink: 0; }
.spot-guide {
  margin: 7px 0 0 30px;
  font-size: 12.5px; line-height: 1.72; color: var(--ink-2);
}

/* 宽屏：居中卡片 */
@media (min-width: 641px) {
  .spot-mask { align-items: center; justify-content: center; padding: 28px; }
  .spot-sheet {
    max-width: 580px; max-height: 84%;
    border-radius: var(--r-xl);
    padding: 24px 26px;
  }
}
</style>
