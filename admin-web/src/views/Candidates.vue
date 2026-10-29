<template>
  <div>
    <div class="bar">
      <h2>候选关系</h2>
      <el-select v-model="status" style="width: 160px" @change="reload">
        <el-option v-for="item in statuses" :key="item" :label="item" :value="item" />
      </el-select>
    </div>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="id" label="编号" width="80" />
      <el-table-column prop="src_id" label="起点" />
      <el-table-column prop="rel" label="关系" width="180" />
      <el-table-column prop="dst_name" label="终点" />
      <el-table-column prop="claim_text" label="断言" />
      <el-table-column label="来源">
        <template #default="{ row }">{{ formatSources(row.sources) }}</template>
      </el-table-column>
      <el-table-column label="敏感词" width="140">
        <template #default="{ row }">
          <el-tag v-if="row.sensitive_block" type="danger" size="small">阻断 {{ row.sensitive_block }}</el-tag>
          <el-tag v-else-if="row.sensitive_review" type="warning" size="small">待确认 {{ row.sensitive_review }}</el-tag>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column v-if="showActions" label="操作" width="160">
        <template #default="{ row }">
          <el-button v-if="status === 'pending'" link :disabled="row.sensitive_block > 0" @click="act(row, 'approve')">通过</el-button>
          <el-button v-if="status === 'pending' || status === 'approved'" link type="danger" @click="act(row, 'reject')">驳回</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const statuses = ['pending', 'approved', 'rejected', 'merged']
const status = ref('pending')
const showActions = computed(() => status.value === 'pending' || status.value === 'approved')
const items = ref([])
const loading = ref(false)

function formatSources(sources) {
  if (!sources || !sources.length) return '—'
  return typeof sources === 'string' ? sources : JSON.stringify(sources)
}

async function reload() {
  loading.value = true
  const response = await api('/admin/kg/candidates?status=' + status.value + '&limit=50')
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '候选列表加载失败'))
    return
  }
  items.value = body.items || []
}

async function act(row, kind) {
  let reason = ''
  let payload = {}
  if (kind === 'approve') {
    if (row.sensitive_block > 0) return
    if (row.sensitive_review > 0) {
      const words = (row.sensitive_words || []).join('、')
      try {
        await ElMessageBox.confirm(
          words ? `待确认：${words}` : '含待确认的敏感词',
          '确认通过',
          { confirmButtonText: '已看过，通过', cancelButtonText: '取消', type: 'warning' },
        )
      } catch {
        return
      }
      payload = { confirm_sensitive: true }
    }
  }
  if (kind === 'reject') {
    try {
      const answer = await ElMessageBox.prompt('驳回理由，可空', '驳回候选', {
        confirmButtonText: '驳回',
        cancelButtonText: '取消',
        inputValue: '',
      })
      reason = answer.value || ''
    } catch {
      return
    }
  }
  const response = await api('/admin/kg/candidates/' + row.id + '/' + kind, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: kind === 'reject' ? JSON.stringify({ reason }) : JSON.stringify(payload),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '操作失败'))
    return
  }
  ElMessage.success('已更新')
  reload()
}

onMounted(reload)
</script>

<style scoped>
.bar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.bar h2 { margin: 0; flex: 1; }
</style>
