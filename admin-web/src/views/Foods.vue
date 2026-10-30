<template>
  <div>
    <div class="bar">
      <h2>食物查询</h2>
      <el-input v-model="q" placeholder="食物名、营养素，或胡萝卜 和 鸡蛋" style="width: 360px" @keyup.enter="run" />
      <el-button type="primary" @click="run">查询</el-button>
    </div>
    <p v-if="!mode" class="hint">支持一次查询多种食物的关系，用顿号、空格或「和」隔开，例如：胡萝卜、鸡蛋、西红柿</p>

    <p v-if="hint" class="hint">{{ hint }}</p>

    <div v-if="mode === 'ambiguous'" class="choices">
      <el-button @click="openFood(result.matches[0])">当作食材：{{ result.matches[0].name }}</el-button>
      <el-button @click="openNutrient(result.nutrient_matches[0])">当作营养素：{{ result.nutrient_matches[0].name }}</el-button>
    </div>

    <template v-if="(mode === 'food' || mode === 'fuzzy') && (result.matches || []).length">
      <el-table :data="result.matches || []" @selection-change="onSelect">
        <el-table-column type="selection" width="48" />
        <el-table-column prop="name" label="名称" />
        <el-table-column prop="alias" label="命中词" />
        <el-table-column prop="method" label="方式" width="100" />
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button link @click="openFood(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="(result.nutrient_matches || []).length" class="hint">
        也像营养素：
        <el-button v-for="item in result.nutrient_matches" :key="item.canonical_id" link @click="openNutrient(item)">
          {{ item.name }}
        </el-button>
      </div>
      <el-button :disabled="!selected.length" type="danger" plain @click="removeSelected">删除选中</el-button>
    </template>

    <template v-if="mode === 'nutrient' || nutrient.items">
      <h3>{{ nutrient.nutrient?.name || '营养素' }}</h3>
      <el-table :data="nutrient.items || []">
        <el-table-column prop="name" label="食材" />
        <el-table-column prop="amount" label="含量" width="120" />
        <el-table-column prop="unit" label="单位" width="100" />
        <el-table-column label="" width="100">
          <template #default="{ row }">
            <el-button link @click="router.push('/foods/' + row.canonical_id)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-if="nutrient.total"
        layout="prev, pager, next, total"
        :page-size="15"
        :total="nutrient.total"
        :current-page="page"
        @current-change="changePage"
      />
      <p v-if="nutrient.missing_amount" class="hint">另有 {{ nutrient.missing_amount }} 条没有数值，不参与排序。</p>
    </template>

    <template v-if="mode === 'relation'">
      <div class="rel-bar">
        <strong>食物关系</strong>
        <el-button @click="clearResult">返回</el-button>
      </div>
      <p v-if="relation.needs_confirm">有模糊命中，确认后再画图。</p>
      <ul>
        <li v-for="item in relation.resolved || []" :key="item.term">
          <template v-if="item.method === 'alias'">{{ item.term }} 是 {{ item.name }} 的别名</template>
          <template v-else-if="item.method === 'fuzzy'">{{ item.term }} 模糊命中 {{ item.name }}</template>
          <template v-else>
            {{ item.name }}<span v-if="otherAliases(item)">，别名：{{ otherAliases(item) }}</span>
          </template>
        </li>
      </ul>
      <p v-if="(relation.missing || []).length">未收录：{{ relation.missing.map((m) => m.term).join('、') }}</p>
      <el-button v-if="relation.needs_confirm" type="primary" @click="loadRelation(true)">确认并画图</el-button>
      <FoodGraph v-if="(relation.subgraph?.nodes || []).length" :subgraph="relation.subgraph" />
    </template>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorMessage, readJson } from '../api'
import FoodGraph from '../components/FoodGraph.vue'

const router = useRouter()
const q = ref('')
const mode = ref('')
const hint = ref('')
const result = ref({})
const nutrient = ref({})
const relation = ref({})
const page = ref(1)
const selected = ref([])

function onSelect(rows) {
  selected.value = rows
}

function otherAliases(item) {
  const name = item.name || ''
  return (item.aliases || []).filter((alias) => alias && alias !== name).join('、')
}

function clearResult() {
  mode.value = ''
  hint.value = ''
  result.value = {}
  nutrient.value = {}
  relation.value = {}
  selected.value = []
}

async function run() {
  hint.value = ''
  nutrient.value = {}
  relation.value = {}
  const response = await api('/admin/kg/foods?q=' + encodeURIComponent(q.value))
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '查询失败'))
    return
  }
  result.value = body
  mode.value = body.mode
  if (body.mode === 'nutrient') {
    await openNutrient(body.nutrient)
  } else if (body.mode === 'relation') {
    await loadRelation(false, body.foods)
  } else if (!(body.matches || []).length && !(body.nutrient_matches || []).length) {
    hint.value = '没有匹配的食材或营养素'
  }
}

async function openNutrient(item) {
  if (item.method === 'fuzzy') {
    try {
      await ElMessageBox.confirm(`「${q.value}」模糊命中「${item.name}」，按这个营养素查？`, '确认')
    } catch {
      return
    }
  }
  page.value = 1
  mode.value = 'nutrient'
  await loadNutrient(item.name || item.alias || q.value)
}

async function loadNutrient(name) {
  const response = await api('/admin/kg/nutrients?q=' + encodeURIComponent(name) + '&page=' + page.value)
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '营养素查询失败'))
    return
  }
  if (body.mode === 'choose') {
    mode.value = 'fuzzy'
    result.value = { matches: [], nutrient_matches: body.candidates || [] }
    hint.value = '对上了多个营养素，请选一个'
    return
  }
  nutrient.value = body
}

function changePage(next) {
  page.value = next
  loadNutrient(nutrient.value.nutrient?.name || q.value)
}

function openFood(row) {
  if (row.method === 'fuzzy') {
    ElMessageBox.confirm(`模糊命中「${row.name}」，打开详情？`, '确认')
      .then(() => router.push('/foods/' + row.canonical_id))
      .catch(() => {})
    return
  }
  router.push('/foods/' + row.canonical_id)
}

async function loadRelation(confirm, foods) {
  const list = foods || (result.value.foods || [])
  const response = await api('/admin/kg/foods/relations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ foods: list, confirm }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '关系查询失败'))
    return
  }
  relation.value = body
  mode.value = 'relation'
}

async function removeSelected() {
  const ids = selected.value.map((row) => row.canonical_id)
  const preview = await api('/admin/kg/foods/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ canonical_ids: ids, preview: true }),
  })
  const previewBody = await readJson(preview)
  if (!preview.ok) {
    ElMessage.error(errorMessage(previewBody, '无法删除'))
    return
  }
  const edges = (previewBody.items || []).reduce((sum, item) => sum + (item.edge_count || 0), 0)
  const names = (previewBody.items || []).map((item) => item.name).join('、')
  try {
    await ElMessageBox.confirm(`删除 ${names}，将丢掉 ${edges} 条边。营养素节点会保留。`, '确认删除', { type: 'warning' })
  } catch {
    return
  }
  const response = await api('/admin/kg/foods/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ canonical_ids: ids }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '删除失败'))
    return
  }
  if ((body.warnings || []).length) ElMessage.warning(body.warnings.join('；'))
  else ElMessage.success('已删除')
  run()
}
</script>

<style scoped>
.bar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.bar h2 { margin: 0 12px 0 0; }
.hint { color: #909399; }
.choices { display: flex; gap: 8px; margin: 12px 0; }
.rel-bar { display: flex; align-items: center; margin: 8px 0; }
.rel-bar strong { flex: 1; }
</style>
