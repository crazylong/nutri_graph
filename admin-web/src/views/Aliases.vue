<template>
  <div>
    <div class="bar">
      <h2>疑似别名</h2>
      <el-select v-model="status" clearable placeholder="全部状态" style="width: 160px" @change="reload">
        <el-option v-for="item in statuses" :key="item" :label="item" :value="item" />
      </el-select>
    </div>
    <p class="hint">确认后写入别名表。补录合并通过时，对得上的别名也会变成 approved。</p>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="alias" label="说法" />
      <el-table-column prop="suggested_canonical_id" label="建议指向" />
      <el-table-column prop="entity_type" label="类型" width="120" />
      <el-table-column prop="status" label="状态" width="120" />
      <el-table-column prop="created_at" label="时间" width="180" />
      <el-table-column label="操作" width="140">
        <template #default="{ row }">
          <template v-if="row.status === 'pending' && row.suggested_canonical_id">
            <el-button link type="primary" @click="approve(row)">确认</el-button>
            <el-button link type="danger" @click="reject(row)">驳回</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      layout="total, prev, pager, next"
      :total="total"
      :page-size="limit"
      :current-page="page"
      @current-change="changePage"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const statuses = ['pending', 'approved', 'rejected']
const status = ref('')
const items = ref([])
const total = ref(0)
const limit = 20
const page = ref(1)
const loading = ref(false)

async function reload() {
  loading.value = true
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String((page.value - 1) * limit),
  })
  if (status.value) params.set('status', status.value)
  const response = await api('/admin/kg/alias-candidates?' + params)
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '别名列表加载失败'))
    return
  }
  items.value = body.items || []
  total.value = body.count || 0
}

function changePage(next) {
  page.value = next
  reload()
}

async function approve(row) {
  const response = await api('/admin/kg/alias-candidates/' + row.id + '/approve', { method: 'POST' })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '确认失败'))
    return
  }
  ElMessage.success('已写入别名')
  reload()
}

async function reject(row) {
  const response = await api('/admin/kg/alias-candidates/' + row.id + '/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: '' }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '驳回失败'))
    return
  }
  ElMessage.success('已驳回')
  reload()
}

onMounted(reload)
</script>

<style scoped>
.bar { display: flex; gap: 8px; align-items: center; }
.bar h2 { margin: 0; flex: 1; }
.hint { color: #909399; font-size: 13px; }
</style>
