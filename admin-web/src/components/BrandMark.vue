<template>
  <!-- 品牌标：碗 + 嫩芽。「吃什么」和「养身体」用最少的笔画说完，
       32 网格、白描在品牌绿上，22px 的侧栏尺寸下也不糊。
       badge=false 时只留图形本身（用 currentColor），放在已经是品牌绿的底色上用。 -->
  <svg
    class="brand-mark"
    :width="size"
    :height="size"
    viewBox="0 0 32 32"
    role="img"
    aria-label="食疗管理"
  >
    <rect v-if="badge" width="32" height="32" :rx="round" fill="currentColor" />
    <g :fill="glyph">
      <path d="M6 17.5H26C26 23.4 21.4 27.6 16 27.6C10.6 27.6 6 23.4 6 17.5Z" />
      <path d="M16 15.2C15.9 11.4 13.4 8.6 9.2 8.8C9.4 12.6 11.8 15.2 16 15.2Z" />
      <path d="M16 12.4C15.8 8.4 18.3 5.2 22.6 5.2C22.6 9.2 20.2 12.4 16 12.4Z" />
    </g>
    <path
      d="M16 17.5V11.6"
      :stroke="glyph"
      stroke-width="2.1"
      stroke-linecap="round"
    />
  </svg>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  size: { type: [Number, String], default: 22 },
  round: { type: [Number, String], default: 9 },
  badge: { type: Boolean, default: true },
})

const glyph = computed(() => (props.badge ? '#ffffff' : 'currentColor'))
</script>

<style scoped>
.brand-mark { display: block; flex: none; }
</style>
