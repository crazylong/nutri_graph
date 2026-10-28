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
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button link @click="act(row, 'approve')">通过</el-button>
          <el-button link type="danger" @click="act(row, 'reject')">驳回</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const statuses = ['pending', 'approved', 'rejected', 'merged']
const status = ref('pending')
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
    body: kind === 'reject' ? JSON.stringify({ reason }) : '{}',
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
