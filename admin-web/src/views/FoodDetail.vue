<template>
  <div v-loading="loading">
    <div class="bar">
      <h2>{{ food.name || '食材' }}</h2>
      <el-button @click="router.push('/foods')">返回</el-button>
      <el-button type="danger" plain @click="remove">删除</el-button>
      <el-button type="primary" plain @click="openForm">重录</el-button>
    </div>
    <p>别名：{{ (food.aliases || []).join('、') || '—' }}</p>
    <p v-if="food.category">分类：{{ food.category }}　性味：{{ food.nature || '—' }}</p>
    <p class="hint">相关边 {{ food.edge_count || 0 }} 条</p>
    <FoodGraph v-if="food.subgraph" :subgraph="food.subgraph" />

    <el-dialog v-model="formOpen" title="重录" width="640px">
      <el-form label-width="120px">
        <el-form-item v-for="col in columns" :key="col.key" :label="col.header" :required="col.required">
          <el-input v-model="form[col.key]" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formOpen = false">取消</el-button>
        <el-button type="primary" @click="submit">提交审核</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorMessage, readJson } from '../api'
import FoodGraph from '../components/FoodGraph.vue'

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const food = ref({})
const formOpen = ref(false)
const columns = ref([])
const form = reactive({})

async function load() {
  loading.value = true
  const response = await api('/admin/kg/foods/' + route.params.cid)
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '详情加载失败'))
    return
  }
  food.value = body
}

async function remove() {
  const response = await api('/admin/kg/foods/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ canonical_ids: [route.params.cid], preview: true }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '无法删除'))
    return
  }
  const item = (body.items || [])[0] || {}
  try {
    await ElMessageBox.confirm(
      `删除「${item.name || food.value.name}」，将丢掉 ${item.edge_count || 0} 条边。`,
      '确认删除',
      { type: 'warning' },
    )
  } catch {
    return
  }
  const done = await api('/admin/kg/foods/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ canonical_ids: [route.params.cid] }),
  })
  const doneBody = await readJson(done)
  if (!done.ok) {
    ElMessage.error(errorMessage(doneBody, '删除失败'))
    return
  }
  ElMessage.success('已删除')
  router.push('/foods')
}

async function openForm() {
  const response = await api('/admin/imports/meta')
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '表单加载失败'))
    return
  }
  columns.value = body.columns || []
  const preset = food.value.form || {}
  for (const col of columns.value) form[col.key] = preset[col.key] || ''
  formOpen.value = true
}

async function submit() {
  const response = await api('/admin/kg/foods/' + route.params.cid + '/rerecord', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(form),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '提交失败'))
    return
  }
  ElMessage.success('已进入补录审核 #' + body.bundle_id)
  formOpen.value = false
}

onMounted(load)
</script>

<style scoped>
.bar { display: flex; gap: 8px; align-items: center; }
.bar h2 { margin: 0; flex: 1; }
.hint { color: #909399; }
</style>
