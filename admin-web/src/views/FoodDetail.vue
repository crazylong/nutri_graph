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
    <h3 v-if="components.length" class="sub">成分</h3>
    <div v-if="components.length" class="level-help">
      <div class="level-help-title">证据等级</div>
      <div class="level-grid">
        <div v-for="item in levelHelp" :key="item.code" class="level-item">
          <i :style="{ background: item.color }" />
          <b>{{ item.code }}</b>
          <span class="level-name">{{ item.name }}</span>
          <span class="level-desc">{{ item.desc }}</span>
        </div>
      </div>
    </div>
    <el-table v-if="components.length" :data="components" size="small">
      <el-table-column prop="name" label="成分" min-width="120" />
      <el-table-column prop="kind" label="类型" width="90" />
      <el-table-column prop="rel" label="关系" width="110" />
      <el-table-column label="含量" width="110">
        <template #default="{ row }">{{ formatAmount(row.amount) }}</template>
      </el-table-column>
      <el-table-column label="单位" width="90">
        <template #default="{ row }">{{ row.unit || '—' }}</template>
      </el-table-column>
      <el-table-column label="证据" min-width="168">
        <template #default="{ row }">
          <span v-if="levelOf(row.level)" class="ev" :title="levelOf(row.level).desc">
            <i :style="{ background: levelOf(row.level).color }" />
            {{ row.level }} {{ levelOf(row.level).name }}
          </span>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column prop="detail" label="说明" min-width="180" />
    </el-table>

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
import { computed, onMounted, reactive, ref } from 'vue'
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
const kindName = {
  Ingredient: '食材',
  Nutrient: '营养素',
  Population: '人群',
  Symptom: '症状',
  Taboo: '禁忌',
}
const levelHelp = [
  { code: 'A', name: '指南/共识', desc: '权威指南或机构发布，可信度最高', color: '#22684c' },
  { code: 'B', name: '实测/研究', desc: '有实测数据或研究支撑', color: '#2a5b83' },
  { code: 'C', name: '典籍/单一来源', desc: '传统典籍或单一来源补录，未做多源交叉验证', color: '#b4772a' },
  { code: 'D', name: '民间说法', desc: '未经验证，回答时通常会被过滤掉', color: '#9a9a9a' },
]

function levelOf(code) {
  return levelHelp.find((item) => item.code === code) || null
}

const components = computed(() => {
  const graph = food.value.subgraph || {}
  const byId = new Map((graph.nodes || []).map((n) => [n.id, n]))
  const anchors = new Set((graph.nodes || []).filter((n) => n.anchor).map((n) => n.id))
  const rows = []
  for (const edge of graph.edges || []) {
    let otherId = edge.target
    if (anchors.has(edge.target) && !anchors.has(edge.source)) otherId = edge.source
    else if (anchors.has(edge.source)) otherId = edge.target
    const other = byId.get(otherId) || {}
    rows.push({
      name: other.name || otherId,
      kind: kindName[other.label] || other.label || '',
      rel: edge.rel_label || edge.rel || '',
      nutrient: edge.rel === 'CONTAINS_NUTRIENT',
      amount: edge.amount,
      unit: edge.unit || '',
      level: edge.evidence_level || '',
      detail: edge.detail || edge.claim || '',
    })
  }
  rows.sort((a, b) => {
    if (a.nutrient !== b.nutrient) return a.nutrient ? -1 : 1
    const left = a.amount == null ? -1 : Number(a.amount)
    const right = b.amount == null ? -1 : Number(b.amount)
    if (left !== right) return right - left
    return String(a.name).localeCompare(String(b.name), 'zh')
  })
  return rows
})

function formatAmount(value) {
  if (value == null || value === '') return '—'
  const number = Number(value)
  if (Number.isNaN(number)) return String(value)
  if (Number.isInteger(number)) return String(number)
  return String(Math.round(number * 1000) / 1000)
}

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
.sub { margin: 16px 0 8px; font-size: 16px; }
.level-help { margin: 0 0 10px; padding: 10px 12px; background: #f7f8f6; border-radius: 8px; }
.level-help-title { margin-bottom: 6px; font-size: 13px; font-weight: 600; color: #303133; }
.level-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 20px; font-size: 13px; color: #606266; }
.level-item { display: flex; gap: 6px; align-items: baseline; min-width: 0; }
.level-item i, .ev i { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.level-name { color: #303133; white-space: nowrap; }
.level-desc { color: #909399; }
.ev { display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }
@media (max-width: 860px) {
  .level-grid { grid-template-columns: 1fr; }
}
</style>
