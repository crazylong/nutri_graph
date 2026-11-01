<template>
  <div>
    <h2>知识库查询</h2>
    <el-form inline>
      <el-form-item label="租户">
        <el-input v-model="filters.tenant_id" clearable />
      </el-form-item>
      <el-form-item label="标题">
        <el-input v-model="filters.title" clearable />
      </el-form-item>
      <el-form-item label="分类">
        <el-select v-model="filters.doc_kind" clearable placeholder="全部" style="width: 120px">
          <el-option label="技术类" value="tech" />
          <el-option label="制度类" value="policy" />
        </el-select>
      </el-form-item>
      <el-form-item label="上传人">
        <el-input v-model="filters.uploader_phone_mask" clearable />
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="filters.status" clearable placeholder="全部" style="width: 120px">
          <el-option label="待审" value="pending" />
          <el-option label="已通过" value="approved" />
          <el-option label="已驳回" value="rejected" />
        </el-select>
      </el-form-item>
      <el-button type="primary" @click="load">查询</el-button>
      <el-button @click="openAdd">添加</el-button>
    </el-form>

    <el-table :data="items" style="width: 100%">
      <el-table-column prop="tenant_id" label="租户" width="120" />
      <el-table-column prop="title" label="标题" />
      <el-table-column label="分类" width="100">
        <template #default="{ row }">{{ kindLabel(row.doc_kind) }}</template>
      </el-table-column>
      <el-table-column prop="uploader_phone_mask" label="上传人" width="140" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">{{ statusLabel(row.status) }}</template>
      </el-table-column>
      <el-table-column prop="error" label="备注" />
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button link @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-card v-if="evalRun" style="margin-top: 16px">
      <template #header>最近一次离线评估</template>
      <p v-if="!evalRun.created_at">还没有跑过 eval/kb_run.py</p>
      <template v-else>
        <p>时间：{{ evalRun.created_at }}</p>
        <p>Ragas：{{ formatJson(evalRun.ragas) }}</p>
        <p>Recall：{{ formatJson(evalRun.recall) }}</p>
      </template>
    </el-card>

    <el-dialog v-model="dialog" :title="editing ? '编辑文档' : '添加文档'" width="480px">
      <el-form label-width="80px">
        <el-form-item label="租户">
          <el-input v-model="form.tenant_id" />
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.doc_kind">
            <el-option label="技术类" value="tech" />
            <el-option label="制度类" value="policy" />
          </el-select>
        </el-form-item>
        <el-form-item label="文件">
          <input type="file" accept=".md,.markdown,.pdf,.docx" @change="onFile" />
          <div v-if="editing">不选文件则只改标题、租户和分类，并下线已有向量。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const items = ref([])
const evalRun = ref(null)
const dialog = ref(false)
const editing = ref(null)
const file = ref(null)
const filters = reactive({
  tenant_id: '',
  title: '',
  doc_kind: '',
  uploader_phone_mask: '',
  status: '',
})
const form = reactive({ tenant_id: '', title: '', doc_kind: 'policy' })

function kindLabel(value) {
  return value === 'tech' ? '技术类' : value === 'policy' ? '制度类' : value
}
function statusLabel(value) {
  return { pending: '待审', approved: '已通过', rejected: '已驳回' }[value] || value
}
function formatJson(value) {
  if (!value) return '—'
  return JSON.stringify(value)
}

function query() {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value)
  }
  const text = params.toString()
  return text ? '?' + text : ''
}

async function load() {
  const response = await api('/admin/kb/docs' + query())
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '查询失败'))
    return
  }
  items.value = body.items || []
  const latest = await api('/admin/kb/eval/latest')
  evalRun.value = await readJson(latest)
}

function openAdd() {
  editing.value = null
  file.value = null
  form.tenant_id = filters.tenant_id
  form.title = ''
  form.doc_kind = 'policy'
  dialog.value = true
}

function openEdit(row) {
  editing.value = row
  file.value = null
  form.tenant_id = row.tenant_id
  form.title = row.title
  form.doc_kind = row.doc_kind
  dialog.value = true
}

function onFile(event) {
  file.value = event.target.files[0] || null
}

function formData() {
  const data = new FormData()
  data.set('tenant_id', form.tenant_id.trim())
  data.set('title', form.title.trim())
  data.set('doc_kind', form.doc_kind)
  if (file.value) data.set('file', file.value)
  return data
}

async function save() {
  if (!editing.value && !file.value) {
    ElMessage.error('请选择文件')
    return
  }
  const response = editing.value
    ? await api('/admin/kb/docs/' + editing.value.id, { method: 'PUT', body: formData() })
    : await api('/admin/kb/docs', { method: 'POST', body: formData() })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '保存失败'))
    return
  }
  dialog.value = false
  await load()
}

async function remove(row) {
  await ElMessageBox.confirm('删除后检索里立刻没有这篇', '删除文档')
  const response = await api('/admin/kb/docs/' + row.id, { method: 'DELETE' })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '删除失败'))
    return
  }
  await load()
}

onMounted(load)
</script>
