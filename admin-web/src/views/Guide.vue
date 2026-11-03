<template>
  <div class="guide">
    <h2>新手入门</h2>
    <p class="hint">选一个流程查看操作手册，帮助你快速上手。</p>

    <div class="cards">
      <el-card class="card" :class="{ active: current === 'food' }" shadow="hover" @click="pick('food')">
        <el-icon class="ico food"><Upload /></el-icon>
        <div class="card-title">食材录入流程</div>
        <div class="card-sub">上传 → 校验 → 提交审核 → 合并进图谱</div>
      </el-card>
      <el-card class="card" :class="{ active: current === 'kb' }" shadow="hover" @click="pick('kb')">
        <el-icon class="ico kb"><Reading /></el-icon>
        <div class="card-title">知识库流程</div>
        <div class="card-sub">添加文档 → 待审 → 通过 → 解析入库</div>
      </el-card>
    </div>

    <div v-if="current" class="viewer">
      <div class="viewer-head">
        <span class="viewer-title">{{ currentTitle }}</span>
        <el-button link type="primary" :href="pdfUrl" target="_blank" rel="noopener">在新窗口打开</el-button>
      </div>
      <iframe :key="current" class="pdf-frame" :src="pdfUrl" title="操作手册预览"></iframe>
    </div>
    <el-empty v-else description="点击上方卡片查看操作流程" />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { Upload, Reading } from '@element-plus/icons-vue'

const current = ref('')

const guides = {
  food: { title: '食材录入的整个流程', url: '/console/sop/food-import-flow.pdf' },
  kb: { title: '知识库上传的整个流程', url: '/console/sop/kb-upload-flow.pdf' },
}

const currentTitle = computed(() => (current.value ? guides[current.value].title : ''))
const pdfUrl = computed(() => (current.value ? guides[current.value].url : ''))

function pick(key) {
  current.value = key === current.value ? '' : key
}
</script>

<style scoped>
.guide { min-height: 100%; }
.hint { color: #909399; font-size: 13px; margin: 0 0 16px; }
.cards { display: flex; gap: 16px; flex-wrap: wrap; }
.card { flex: 1; min-width: 240px; cursor: pointer; text-align: center; padding: 8px; transition: border-color .15s, box-shadow .15s; }
.card.active { border-color: var(--el-color-primary); }
.ico { font-size: 40px; margin-bottom: 8px; }
.ico.food { color: var(--el-color-primary); }
.ico.kb { color: #2f7d5d; }
.card-title { font-size: 16px; font-weight: 600; color: #1a241e; }
.card-sub { font-size: 13px; color: #909399; margin-top: 6px; }
.viewer { margin-top: 20px; }
.viewer-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.viewer-title { font-size: 15px; font-weight: 600; color: #1a241e; }
.pdf-frame { width: 100%; height: 72vh; border: 1px solid #e3e9e4; border-radius: 8px; background: #f7f8f7; }
</style>
