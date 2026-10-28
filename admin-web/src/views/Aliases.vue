<template>
  <div>
    <div class="bar">
      <h2>疑似别名</h2>
      <el-select v-model="status" clearable placeholder="全部状态" style="width: 160px" @change="reload">
        <el-option v-for="item in statuses" :key="item" :label="item" :value="item" />
      </el-select>
    </div>
    <p class="hint">这里只查看。补录合并通过时，对得上的别名会被改成 approved。</p>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="alias" label="说法" />
      <el-table-column prop="suggested_canonical_id" label="建议指向" />
      <el-table-column prop="entity_type" label="类型" width="120" />
      <el-table-column prop="status" label="状态" width="120" />
      <el-table-column prop="created_at" label="时间" width="180" />
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

onMounted(reload)
</script>

<style scoped>
.bar { display: flex; gap: 8px; align-items: center; }
.bar h2 { margin: 0; flex: 1; }
.hint { color: #909399; font-size: 13px; }
</style>
