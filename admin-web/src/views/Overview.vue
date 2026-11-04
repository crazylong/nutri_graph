<template>
  <div v-loading="loading">
    <h2>概览</h2>
    <el-row :gutter="12">
      <el-col :xs="24" :sm="12" :md="8"><el-card><div class="n">{{ users }}</div><div>活跃用户</div></el-card></el-col>
      <el-col :xs="24" :sm="12" :md="8"><el-card><div class="n">{{ pendingBundles }}</div><div>待审补录</div></el-card></el-col>
      <el-col :xs="24" :sm="24" :md="8"><el-card><div class="n">{{ requestSummary }}</div><div>未收录各状态</div></el-card></el-col>
    </el-row>
    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :xs="24" :sm="12" :md="8"><el-card><div class="n">{{ todayCalls }}</div><div>今日模型调用</div></el-card></el-col>
      <el-col :xs="24" :sm="12" :md="8"><el-card><div class="n">{{ todayTokens }}</div><div>今日 token</div></el-card></el-col>
      <el-col :xs="24" :sm="24" :md="8"><el-card><div class="n">{{ todayCost }}</div><div>今日费用（元）</div></el-card></el-col>
    </el-row>
    <el-table v-if="topUsers.length" :data="topUsers" size="small" style="margin-top: 12px">
      <el-table-column prop="user_id" label="用户" />
      <el-table-column prop="cost" label="估算费用" width="120" />
      <el-table-column prop="tokens" label="token" width="120" />
    </el-table>
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
const todayCalls = computed(() => stats.value?.today_calls ?? '—')
const todayTokens = computed(() => stats.value?.today_tokens ?? '—')
const todayCost = computed(() => stats.value?.today_cost ?? '—')
const topUsers = computed(() => stats.value?.top_users || [])
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
.n { font-size: 22px; font-weight: 600; margin-bottom: 6px; overflow-wrap: anywhere; }
.err { color: #c45656; }
@media (max-width: 768px) {
  .el-col { margin-bottom: 12px; }
}
</style>
