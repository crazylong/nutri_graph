<template>
  <div v-loading="metaLoading">
    <h2>批量导入</h2>
    <p class="hint">上传一张表格（或点模板下载先填好），逐行校验后提交进审核队列，再一键整批通过合并进主图。</p>

    <div class="bar">
      <a class="tpl-link" :href="tplXlsx" download>下载 Excel 模板</a>
      <a class="tpl-link" :href="tplCsv" download>下载 CSV 模板</a>
      <span class="meta" v-if="meta">{{ meta.limits.max_rows }} 行以内 · {{ meta.limits.max_file_kb }}KB 以内</span>
    </div>

    <!-- 上传 -->
    <el-upload
      drag
      :auto-upload="false"
      :limit="1"
      :on-change="onFile"
      :on-remove="resetFile"
      accept=".xlsx,.csv"
      class="drop"
    >
      <div class="drop-inner">
        <p class="drop-title">拖拽文件到这里，或点击选择</p>
        <p class="drop-sub">支持 .xlsx / .csv</p>
      </div>
    </el-upload>

    <div class="actions" v-if="jobId">
      <el-button type="primary" :loading="committing" @click="commit">提交审核</el-button>
      <el-button :loading="committing" @click="resetAll">重新上传</el-button>
    </div>

    <!-- 汇总 -->
    <el-descriptions v-if="summary" :column="4" border class="summary">
      <el-descriptions-item label="总行数">{{ summary.total }}</el-descriptions-item>
      <el-descriptions-item label="可提交">{{ summary.importable }}</el-descriptions-item>
      <el-descriptions-item label="警告">{{ summary.warn }}</el-descriptions-item>
      <el-descriptions-item label="错误">{{ summary.error }}</el-descriptions-item>
    </el-descriptions>

    <!-- 逐行预览 -->
    <el-table v-if="rows.length" :data="rows" border max-height="480" class="preview">
      <el-table-column prop="row_no" label="行" width="60" />
      <el-table-column prop="name" label="名称" width="160" />
      <el-table-column prop="category" label="分类" width="90" />
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
        </template>
      </el-table-column>
    </el-table>

    <!-- 提交结果 → 批量通过 -->
    <el-alert v-if="committed" type="success" :closable="false" class="committed">
      已提交 {{ committed.staged }} 条进入审核队列（新增 {{ committed.inserted }} · 覆盖 {{ committed.updated }}）。
    </el-alert>
    <div class="actions" v-if="committed">
      <el-button type="success" :loading="approving" @click="approve">一键整批通过并合并</el-button>
      <el-button @click="goJobs">看导入记录</el-button>
    </div>
    <el-alert v-if="approveResult" type="success" :closable="false" class="committed">
      整批通过 {{ approveResult.approved }} 条，合并节点 {{ approveResult.merge.nodes }} 个、边 {{ approveResult.merge.edges }} 条。
      <span v-if="approveResult.skipped.length">跳过 {{ approveResult.skipped.length }} 条。</span>
    </el-alert>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const router = useRouter()
const metaLoading = ref(true)
const meta = ref(null)
const tplXlsx = ref('')
const tplCsv = ref('')

const jobId = ref('')
const summary = ref(null)
const rows = ref([])
const committing = ref(false)
const committed = ref(null)
const approving = ref(false)
const approveResult = ref(null)

const verdictTextMap = {
  ok: '可导入', warn: '可导入·有警告', error: '错误', duplicate_file: '文件内重复',
  duplicate_db: '已存在',
}
const verdictTypeMap = {
  ok: 'success', warn: 'warning', error: 'danger', duplicate_file: 'info', duplicate_db: 'primary',
}
function verdictText(v) { return verdictTextMap[v] || v }
function verdictType(v) { return verdictTypeMap[v] || 'info' }

async function loadMeta() {
  const response = await api('/admin/imports/meta')
  const body = await readJson(response)
  metaLoading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '导入元信息加载失败'))
    return
  }
  meta.value = body
  tplXlsx.value = body.template.xlsx
  tplCsv.value = body.template.csv
}

function onFile(file) {
  upload(file.raw)
}

function resetFile() {
  resetAll()
}

function resetAll() {
  jobId.value = ''
  summary.value = null
  rows.value = []
  committed.value = null
  approveResult.value = null
}

async function upload(raw) {
  const form = new FormData()
  form.append('file', raw)
  const response = await api('/admin/imports/preview', { method: 'POST', body: form })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '解析失败'))
    return
  }
  jobId.value = body.job_id
  summary.value = body.summary
  rows.value = body.rows || []
  committed.value = null
  approveResult.value = null
  ElMessage.success(`解析完成，可提交 ${body.summary.importable} 行`)
}

async function commit() {
  if (!jobId.value) return
  committing.value = true
  const response = await api(`/admin/imports/${jobId.value}/commit`, { method: 'POST' })
  const body = await readJson(response)
  committing.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '提交失败'))
    return
  }
  committed.value = body
  ElMessage.success('已提交审核队列')
}

async function approve() {
  if (!jobId.value) return
  approving.value = true
  const response = await api(`/admin/imports/${jobId.value}/approve`, { method: 'POST' })
  const body = await readJson(response)
  approving.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '批量通过失败'))
    return
  }
  approveResult.value = body
  ElMessage.success('已整批通过并合并')
}

function goJobs() {
  router.push('/imports')
}

onMounted(loadMeta)
</script>

<style scoped>
.hint { color: #909399; font-size: 13px; }
.bar { display: flex; gap: 16px; align-items: center; margin: 12px 0; }
.tpl-link { color: #409eff; text-decoration: none; }
.meta { color: #909399; font-size: 13px; }
.drop { margin: 12px 0; }
.drop-inner { padding: 16px 0; }
.drop-title { margin: 0 0 4px; }
.drop-sub { margin: 0; color: #909399; font-size: 13px; }
.actions { display: flex; gap: 8px; margin: 12px 0; }
.summary { margin: 12px 0; }
.preview { margin: 12px 0; }
.msg { display: block; font-size: 13px; }
.msg.error { color: #c45656; }
.msg.warn { color: #e6a23c; }
.msg.info { color: #909399; }
.msg .field { color: #909399; }
.committed { margin: 12px 0; }
</style>
