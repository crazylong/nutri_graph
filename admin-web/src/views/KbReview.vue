<template>
  <div>
    <h2>知识库审核</h2>
    <el-radio-group v-model="status" @change="load">
      <el-radio-button value="pending">待审</el-radio-button>
      <el-radio-button value="approved">已通过</el-radio-button>
      <el-radio-button value="rejected">已驳回</el-radio-button>
    </el-radio-group>
    <el-table :data="items" style="width: 100%; margin-top: 12px">
      <el-table-column prop="tenant_id" label="租户" width="120" />
      <el-table-column prop="title" label="标题" />
      <el-table-column label="分类" width="100">
        <template #default="{ row }">{{ row.doc_kind === 'tech' ? '技术类' : '制度类' }}</template>
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
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const status = ref('pending')
const items = ref([])

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
  const response = await api('/admin/kb/docs/' + row.id + '/approve', { method: 'POST' })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '通过失败，文档仍是待审'))
  }
  await load()
}

async function reject(row) {
  const { value } = await ElMessageBox.prompt('驳回原因（可空）', '驳回', { inputValue: '' })
  const data = new FormData()
  data.set('reason', value || '')
  const response = await api('/admin/kb/docs/' + row.id + '/reject', { method: 'POST', body: data })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '驳回失败'))
    return
  }
  await load()
}

onMounted(load)
</script>
