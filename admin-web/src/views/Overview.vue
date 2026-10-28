<template>
  <div v-loading="loading">
    <h2>概览</h2>
    <el-row :gutter="12">
      <el-col :span="8"><el-card><div class="n">{{ users }}</div><div>活跃用户</div></el-card></el-col>
      <el-col :span="8"><el-card><div class="n">{{ pendingBundles }}</div><div>待审补录</div></el-card></el-col>
      <el-col :span="8"><el-card><div class="n">{{ requestSummary }}</div><div>未收录各状态</div></el-card></el-col>
    </el-row>
    <p v-if="error" class="err">{{ error }}</p>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, errorMessage, readJson } from '../api'

const loading = ref(true)
const error = ref('')
const stats = ref(null)
const users = computed(() => stats.value?.users?.active ?? '—')
const pendingBundles = computed(() => stats.value?.kg?.pending_bundles ?? '—')
const requestSummary = computed(() => {
  const rows = stats.value?.kg?.food_requests
  if (!rows) return '—'
  const parts = Object.entries(rows)
    .filter(([key]) => key !== 'total' && key !== 'resolution_rate')
    .map(([key, value]) => `${key} ${value}`)
  const rate = rows.resolution_rate
  return (parts.join(' / ') || '0') + (rate == null ? '' : ` · 解决率 ${rate}`)
})

onMounted(async () => {
  const response = await api('/admin/stats')
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    error.value = errorMessage(body, '概览加载失败')
    return
  }
  stats.value = body
})
</script>

<style scoped>
.n { font-size: 22px; font-weight: 600; margin-bottom: 6px; }
.err { color: #c45656; }
</style>
