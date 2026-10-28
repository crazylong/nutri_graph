<template>
  <div>
    <div class="bar">
      <h2>导入记录</h2>
      <el-button link @click="router.push('/import-new')">返回批量导入</el-button>
    </div>
    <el-table :data="items" v-loading="loading" @row-click="open">
      <el-table-column prop="filename" label="文件名" />
      <el-table-column prop="total_rows" label="总行" width="70" />
      <el-table-column prop="ok_rows" label="可提交" width="80" />
      <el-table-column prop="warn_rows" label="警告" width="70" />
      <el-table-column prop="error_rows" label="错误" width="70" />
      <el-table-column prop="status" label="状态" width="100" />
      <el-table-column prop="created_at" label="创建时间" width="180" />
    </el-table>
    <p class="hint">点任意一行查看该任务逐行校验结果。</p>
    <el-pagination
      v-if="total > pageSize"
      layout="prev, pager, next"
      :total="total"
      :page-size="pageSize"
      :current-page="page"
      @current-change="onPage"
      class="pager"
    />

    <!-- 单任务详情 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="860px" top="6vh">
      <div v-loading="detailLoading">
        <el-descriptions :column="4" border v-if="detail">
          <el-descriptions-item label="文件名">{{ detail.filename || '—' }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ detail.status }}</el-descriptions-item>
          <el-descriptions-item label="总行">{{ detail.total_rows }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ detail.created_at }}</el-descriptions-item>
        </el-descriptions>

        <el-table v-if="detail && detail.rows.length" :data="detail.rows" border max-height="460" class="rows">
          <el-table-column prop="row_no" label="行" width="60" />
          <el-table-column prop="name" label="名称" width="180" />
          <el-table-column label="判定" width="110">
            <template #default="{ row }">
              <el-tag :type="verdictType(row.verdict)" size="small">{{ verdictText(row.verdict) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="提示">
            <template #default="{ row }">
              <span v-for="(m, i) in row.messages" :key="i" class="msg" :class="m.level">
                {{ m.text }}<span v-if="m.field" class="field">（{{ m.field }}）</span>
              </span>
              <span v-if="!row.messages || !row.messages.length" class="msg info">—</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else-if="detail" description="这个任务没有行级记录" :image-size="60" />
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const router = useRouter()
const items = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20

const dialogVisible = ref(false)
const dialogTitle = ref('')
const detailLoading = ref(false)
const detail = ref(null)

const verdictTextMap = {
  ok: '可导入', warn: '可导入·有警告', error: '错误', duplicate_file: '文件内重复',
  duplicate_db: '已存在',
}
const verdictTypeMap = {
  ok: 'success', warn: 'warning', error: 'danger', duplicate_file: 'info', duplicate_db: 'primary',
}
function verdictText(v) { return verdictTextMap[v] || v }
function verdictType(v) { return verdictTypeMap[v] || 'info' }

async function load() {
  loading.value = true
  const offset = (page.value - 1) * pageSize
  const response = await api(`/admin/imports?limit=${pageSize}&offset=${offset}`)
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '导入记录加载失败'))
    return
  }
  items.value = body.items || []
  total.value = body.count || 0
}

async function open(row) {
  dialogVisible.value = true
  dialogTitle.value = `导入详情 · ${row.filename || row.job_id}`
  detailLoading.value = true
  detail.value = null
  const response = await api('/admin/imports/' + row.job_id)
  const body = await readJson(response)
  detailLoading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '详情加载失败'))
    return
  }
  detail.value = body
}

function onPage(p) {
  page.value = p
  load()
}

onMounted(load)
</script>

<style scoped>
.bar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.bar h2 { margin: 0; flex: 1; }
.hint { color: #909399; font-size: 13px; }
.pager { margin-top: 12px; justify-content: flex-end; }
.rows { margin-top: 12px; }
.msg { display: block; font-size: 13px; }
.msg.error { color: #c45656; }
.msg.warn { color: #e6a23c; }
.msg.info { color: #909399; }
.msg .field { color: #909399; }
</style>
