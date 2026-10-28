<template>
  <div>
    <div class="bar">
      <h2>未收录食物</h2>
      <el-button type="primary" @click="manualVisible = true">手工登记</el-button>
    </div>
    <div class="filters">
      <el-select v-model="status" clearable placeholder="全部状态" style="width: 180px" @change="reload">
        <el-option v-for="item in statuses" :key="item" :label="item" :value="item" />
      </el-select>
      <el-input v-model="keyword" placeholder="食物名或原话" style="width: 240px" @keyup.enter="reload" />
      <el-button @click="reload">查询</el-button>
    </div>
    <el-table :data="items" v-loading="loading" @row-click="openDetail">
      <el-table-column prop="food_key" label="归一名" />
      <el-table-column prop="raw_mention" label="原话" />
      <el-table-column prop="hit_count" label="次数" width="80" />
      <el-table-column prop="status" label="状态" width="140" />
      <el-table-column prop="source" label="来源" width="100" />
      <el-table-column prop="reject_reason" label="驳回理由" />
      <el-table-column prop="last_requested_at" label="最近提及" width="180" />
    </el-table>
    <el-pagination
      layout="total, prev, pager, next"
      :total="total"
      :page-size="limit"
      :current-page="page"
      @current-change="changePage"
    />

    <el-drawer v-model="drawer" title="登记明细" size="420px">
      <template v-if="detail">
        <p>归一名：{{ detail.food_key }}</p>
        <p>原话：{{ detail.raw_mention }}</p>
        <p>次数：{{ detail.hit_count }}</p>
        <p>状态：{{ detail.status }}</p>
        <p>来源：{{ detail.source }}</p>
        <p>提交者：{{ detail.requested_by }}</p>
        <p>驳回理由：{{ detail.reject_reason || '—' }}</p>
        <el-input v-model="reason" type="textarea" placeholder="驳回理由，可空" />
        <el-button type="danger" :loading="rejecting" @click="reject">驳回</el-button>
      </template>
    </el-drawer>

    <el-dialog v-model="manualVisible" title="手工登记" width="420px">
      <el-input v-model="manualName" placeholder="食物名" />
      <template #footer>
        <el-button @click="manualVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">登记</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const statuses = ['queued', 'fetching', 'fetched', 'pending_review', 'approved', 'rejected']
const status = ref('')
const keyword = ref('')
const items = ref([])
const total = ref(0)
const limit = 20
const page = ref(1)
const loading = ref(false)
const drawer = ref(false)
const detail = ref(null)
const reason = ref('')
const rejecting = ref(false)
const manualVisible = ref(false)
const manualName = ref('')
const saving = ref(false)

async function reload() {
  loading.value = true
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String((page.value - 1) * limit),
  })
  if (status.value) params.set('status', status.value)
  if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
  const response = await api('/admin/kg/requests?' + params)
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '列表加载失败'))
    return
  }
  items.value = body.items || []
  total.value = body.count || 0
}

function changePage(next) {
  page.value = next
  reload()
}

async function openDetail(row) {
  const response = await api('/admin/kg/requests/' + row.id)
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '明细加载失败'))
    return
  }
  detail.value = body
  reason.value = body.reject_reason || ''
  drawer.value = true
}

async function reject() {
  rejecting.value = true
  const response = await api('/admin/kg/requests/' + detail.value.id + '/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: reason.value }),
  })
  const body = await readJson(response)
  rejecting.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '驳回失败'))
    return
  }
  ElMessage.success('已驳回')
  drawer.value = false
  reload()
}

async function create() {
  saving.value = true
  const response = await api('/admin/kg/requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: manualName.value.trim() }),
  })
  const body = await readJson(response)
  saving.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '登记失败'))
    return
  }
  ElMessage.success(body.message || '已登记')
  manualVisible.value = false
  manualName.value = ''
  reload()
}

onMounted(reload)
</script>

<style scoped>
.bar, .filters { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.bar h2 { margin: 0; flex: 1; }
</style>
