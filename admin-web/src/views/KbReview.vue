<template>
  <div>
    <h2>知识库审核</h2>
    <div class="filters">
      <el-radio-group v-model="status" @change="load">
        <el-radio-button value="pending">待审</el-radio-button>
        <el-radio-button value="approved">已通过</el-radio-button>
        <el-radio-button value="rejected">已驳回</el-radio-button>
      </el-radio-group>
    </div>
    <el-table :data="items" style="width: 100%">
      <el-table-column prop="tenant_id" label="租户" width="120" />
      <el-table-column label="标题">
        <template #default="{ row }">
          <button type="button" class="kb-title-link" @click="source.open(row)">{{ row.title }}</button>
        </template>
      </el-table-column>
      <el-table-column label="分类" width="100">
        <template #default="{ row }">{{ kindLabel(row.doc_kind) }}</template>
      </el-table-column>
      <el-table-column prop="filename" label="文件" />
      <el-table-column prop="uploader_phone_mask" label="上传人" width="140" />
      <el-table-column prop="error" label="备注" />
      <el-table-column label="操作" width="180">
        <template #default="{ row }">
          <el-button v-if="row.status === 'pending'" link type="primary" @click="approve(row)">通过</el-button>
          <el-button v-if="row.status !== 'rejected'" link type="danger" @click="reject(row)">驳回</el-button>
        </template>
      </el-table-column>
    </el-table>
    <KbSource ref="source" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorMessage, readJson } from '../api'
import KbSource from '../components/KbSource.vue'

const status = ref('pending')
const items = ref([])
const source = ref(null)
const KIND_LABELS = { tech: '技术类', policy: '制度类', qa: '问答类' }

function kindLabel(value) {
  return KIND_LABELS[value] || value
}

async function load() {
  const response = await api('/admin/kb/review?status=' + encodeURIComponent(status.value))
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '加载失败'))
    return
  }
  items.value = body.items || []
}

async function approve(row) {
  try {
    await ElMessageBox.confirm(`确定通过「${row.title}」吗？`, '审核确认', {
      confirmButtonText: '确定通过',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  const response = await api('/admin/kb/docs/' + row.id + '/approve', { method: 'POST' })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '通过失败'))
    return
  }
  ElMessage.success('已通过')
  await load()
}

async function reject(row) {
  let value
  try {
    const result = await ElMessageBox.prompt('确定驳回该文档吗？请填写驳回原因（可空）：', '驳回确认', {
      inputValue: '',
      confirmButtonText: '确定驳回',
      cancelButtonText: '取消',
      inputPlaceholder: '驳回原因（可空）',
    })
    value = result.value
  } catch {
    return
  }
  const data = new FormData()
  data.set('reason', value || '')
  const response = await api('/admin/kb/docs/' + row.id + '/reject', { method: 'POST', body: data })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '驳回失败'))
    return
  }
  ElMessage.success('已驳回')
  await load()
}

onMounted(load)
</script>
