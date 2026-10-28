<template>
  <div>
    <div class="bar">
      <h2>补录审核</h2>
      <el-select v-model="status" style="width: 180px" @change="reload">
        <el-option v-for="item in statuses" :key="item" :label="item" :value="item" />
      </el-select>
    </div>
    <el-table :data="items" v-loading="loading" @row-click="open">
      <el-table-column prop="id" label="编号" width="80" />
      <el-table-column prop="food_key" label="食物" />
      <el-table-column prop="canonical_name" label="规范名" />
      <el-table-column prop="status" label="状态" width="140" />
      <el-table-column prop="score_total" label="评分" width="90" />
      <el-table-column prop="source_desc" label="来源说明" />
    </el-table>
    <p class="hint">驳回在明细页操作。这个列表没有 offset，一次最多 50 条。</p>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const router = useRouter()
const statuses = ['pending_review', 'draft', 'verified', 'approved', 'merged', 'rejected']
const status = ref('pending_review')
const items = ref([])
const loading = ref(false)

async function reload() {
  loading.value = true
  const response = await api('/admin/kg/bundles?status=' + status.value + '&limit=50')
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '补录列表加载失败'))
    return
  }
  items.value = body.items || []
}

function open(row) {
  router.push('/bundles/' + row.id)
}

onMounted(reload)
</script>

<style scoped>
.bar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.bar h2 { margin: 0; flex: 1; }
.hint { color: #909399; font-size: 13px; }
</style>
