<template>
  <div v-loading="loading">
    <el-button link @click="router.push('/bundles')">返回列表</el-button>
    <template v-if="bundle">
      <h2>{{ bundle.canonical_name || bundle.food_key }}</h2>
      <p>状态 {{ bundle.status }} · 评分 {{ bundle.score_total }} · 来源 {{ bundle.source_desc }}</p>
      <p v-if="bundle.reject_reason">驳回理由：{{ bundle.reject_reason }}</p>
      <el-table :data="bundle.items || []">
        <el-table-column prop="item_type" label="类型" width="120" />
        <el-table-column prop="field_name" label="字段" />
        <el-table-column prop="field_value" label="值" />
        <el-table-column prop="unit" label="单位" width="80" />
        <el-table-column prop="source_locator" label="出处" />
      </el-table>
      <div class="actions">
        <el-input v-model="reason" placeholder="驳回理由，可空" />
        <el-button type="primary" :loading="busy" @click="approve">通过并合并</el-button>
        <el-button type="danger" :loading="busy" @click="reject">驳回</el-button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const route = useRoute()
const router = useRouter()
const bundle = ref(null)
const loading = ref(true)
const busy = ref(false)
const reason = ref('')

async function load() {
  const response = await api('/admin/kg/bundles/' + route.params.id)
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '明细加载失败'))
    return
  }
  bundle.value = body
  reason.value = body.reject_reason || ''
}

async function approve() {
  busy.value = true
  const response = await api('/admin/kg/bundles/' + route.params.id + '/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ merge: true }),
  })
  const body = await readJson(response)
  busy.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '通过失败'))
    return
  }
  ElMessage.success('已通过')
  router.push('/bundles')
}

async function reject() {
  busy.value = true
  const response = await api('/admin/kg/bundles/' + route.params.id + '/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: reason.value }),
  })
  const body = await readJson(response)
  busy.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '驳回失败'))
    return
  }
  ElMessage.success('已驳回')
  router.push('/bundles')
}

onMounted(load)
</script>

<style scoped>
.actions { display: flex; gap: 8px; margin-top: 12px; }
.actions .el-input { max-width: 360px; }
</style>
