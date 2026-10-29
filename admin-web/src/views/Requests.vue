<template>
  <div>
    <div class="bar">
      <h2>未收录食物</h2>
      <el-button :disabled="!selected.length" @click="batch('ignore_noise')">批量忽略</el-button>
      <el-button :disabled="!selected.length" type="danger" plain @click="batch('reject')">批量驳回</el-button>
      <el-button type="primary" @click="manualVisible = true">代登记</el-button>
    </div>
    <div class="filters">
      <el-select v-model="status" clearable placeholder="全部状态" style="width: 180px" @change="reload">
        <el-option v-for="item in statuses" :key="item" :label="statusText(item)" :value="item" />
      </el-select>
      <el-select v-model="sort" style="width: 140px" @change="reload">
        <el-option label="最近提及" value="recent" />
        <el-option label="提及次数" value="hits" />
      </el-select>
      <el-input v-model="keyword" placeholder="食物名或原话" style="width: 240px" @keyup.enter="reload" />
      <el-button @click="reload">查询</el-button>
    </div>
    <el-table
      :data="items"
      v-loading="loading"
      row-key="id"
      @selection-change="onSelect"
      @row-click="openDetail"
    >
      <el-table-column type="selection" width="42" />
      <el-table-column label="说法" min-width="180">
        <template #default="{ row }">{{ mention(row) }}</template>
      </el-table-column>
      <el-table-column prop="hit_count" label="次数" width="70" />
      <el-table-column label="反查" width="110">
        <template #default="{ row }">{{ triageText(row.triage) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">{{ statusText(row.status) }}</template>
      </el-table-column>
      <el-table-column label="最近提及" width="170">
        <template #default="{ row }">{{ shortTime(row.last_requested_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button
            v-if="canFetch(row)"
            link
            type="primary"
            @click.stop="fetchNow(row)"
          >立即抓取</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      layout="total, prev, pager, next"
      :total="total"
      :page-size="limit"
      :current-page="page"
      @current-change="changePage"
    />

    <el-drawer v-model="drawer" title="登记明细" size="480px">
      <template v-if="detail">
        <p>说法：{{ mention(detail) }}</p>
        <p>次数：{{ detail.hit_count }}　状态：{{ statusText(detail.status) }}</p>
        <p>反查：{{ triageText(matchResult && matchResult.verdict) }}</p>
        <div v-if="matchResult && matchResult.hits && matchResult.hits.length" class="hits">
          <p v-for="(hit, index) in matchResult.hits" :key="index" class="hit">
            <span>{{ hit.name }}</span>
            <span class="why">{{ hit.why }}<template v-if="hit.score != null"> {{ hit.score }}</template></span>
            <el-button
              v-if="hit.canonical_id && detail.status !== 'approved'"
              size="small"
              type="primary"
              :loading="acting"
              @click="attach(hit)"
            >确认挂别名</el-button>
          </p>
        </div>
        <p v-else-if="matchResult">没有对得上的已有食材。</p>
        <div class="actions">
          <el-button v-if="detail.status !== 'approved'" @click="openBundle">自建营养素</el-button>
          <el-button :loading="acting" @click="ignoreOne">标记噪音</el-button>
        </div>
        <el-input v-model="reason" type="textarea" placeholder="驳回理由，可空" />
        <el-button type="danger" :loading="acting" @click="reject">驳回</el-button>
      </template>
    </el-drawer>

    <el-dialog v-model="manualVisible" title="代登记" width="420px">
      <el-input v-model="manualName" placeholder="食物名" />
      <template #footer>
        <el-button @click="manualVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">登记</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="bundleVisible" title="自建营养素" width="520px">
      <p v-if="detail" class="hint">名称锁定为「{{ detail.food_key }}」，跟这张登记是同一条。</p>
      <div class="form">
        <label v-for="field in formFields" :key="field.key">
          <span>{{ field.header }}<template v-if="field.required"> *</template></span>
          <el-select
            v-if="field.key === 'category'"
            v-model="form[field.key]"
            filterable
            style="width: 100%"
          >
            <el-option v-for="item in categories" :key="item" :label="item" :value="item" />
          </el-select>
          <el-input v-else v-model="form[field.key]" :placeholder="field.hint || field.unit" />
        </label>
      </div>
      <template #footer>
        <el-button @click="bundleVisible = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitBundle">提交审核</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const statuses = ['queued', 'fetching', 'fetched', 'pending_review', 'approved', 'rejected']
const STATUS_CN = {
  queued: '已登记，等待处理',
  fetching: '正在从权威来源获取',
  fetched: '已获取，等待校验',
  pending_review: '等待人工审核',
  approved: '已收录',
  rejected: '未通过核实',
}
const TRIAGE_CN = {
  exists: '已在库',
  likely_exists: '可能已有',
  not_found: '没找到',
  not_food: '不是食物',
}
function statusText(s) {
  return STATUS_CN[s] || s
}
function triageText(s) {
  return TRIAGE_CN[s] || '未反查'
}
function mention(row) {
  if (!row.raw_mention || row.raw_mention === row.food_key) return row.food_key
  return row.raw_mention + ' → ' + row.food_key
}
function shortTime(value) {
  if (!value) return ''
  return String(value).replace('T', ' ').slice(0, 16)
}
function canFetch(row) {
  return row.status === 'queued' && ['queued', 'failed', 'rejected'].includes(row.task_status)
}

const status = ref('')
const sort = ref('recent')
const keyword = ref('')
const items = ref([])
const total = ref(0)
const limit = 20
const page = ref(1)
const loading = ref(false)
const selected = ref([])
const drawer = ref(false)
const detail = ref(null)
const matchResult = ref(null)
const reason = ref('')
const acting = ref(false)
const manualVisible = ref(false)
const manualName = ref('')
const saving = ref(false)
const bundleVisible = ref(false)
const columns = ref([])
const categories = ref([])
const form = ref({})

const formFields = computed(() =>
  columns.value.filter((field) => field.key !== 'name' && (field.required || field.key === 'source' || field.key === 'nature')),
)

async function reload() {
  loading.value = true
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String((page.value - 1) * limit),
    sort: sort.value,
  })
  if (status.value) params.set('status', status.value)
  if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
  const response = await api('/admin/kg/requests?' + params)
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '列表加载失败'))
    return
  }
  items.value = body.items || []
  total.value = body.count || 0
}

function changePage(next) {
  page.value = next
  reload()
}

function onSelect(rows) {
  selected.value = rows
}

async function openDetail(row) {
  const response = await api('/admin/kg/requests/' + row.id)
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '明细加载失败'))
    return
  }
  detail.value = body
  reason.value = ''
  drawer.value = true
  matchResult.value = null
  const matched = await api('/admin/kg/requests/' + row.id + '/matches')
  const found = await readJson(matched)
  if (!matched.ok) {
    ElMessage.error(errorMessage(found, '反查失败'))
    return
  }
  matchResult.value = found
}

async function reject() {
  acting.value = true
  const response = await api('/admin/kg/requests/' + detail.value.id + '/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: reason.value }),
  })
  const body = await readJson(response)
  acting.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '驳回失败'))
    return
  }
  ElMessage.success('已驳回')
  drawer.value = false
  reload()
}

async function batch(action) {
  const ids = selected.value.map((row) => row.id)
  if (!ids.length) return
  acting.value = true
  const response = await api('/admin/kg/requests/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, action, reason: reason.value }),
  })
  const body = await readJson(response)
  acting.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '批量处理失败'))
    return
  }
  ElMessage.success(action === 'ignore_noise' ? '已标为噪音' : '已驳回')
  drawer.value = false
  reload()
}

function ignoreOne() {
  selected.value = [detail.value]
  batch('ignore_noise')
}

async function attach(hit) {
  acting.value = true
  const response = await api('/admin/kg/requests/' + detail.value.id + '/alias', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ canonical_id: hit.canonical_id, alias: detail.value.food_key }),
  })
  const body = await readJson(response)
  acting.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '挂别名失败'))
    return
  }
  ElMessage.success('已挂到 ' + hit.name)
  drawer.value = false
  reload()
}

async function fetchNow(row) {
  const response = await api('/admin/kg/requests/' + row.id + '/fetch', { method: 'POST' })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '没能启动抓取'))
    return
  }
  ElMessage.success('已开始抓取')
  reload()
}

async function create() {
  saving.value = true
  const response = await api('/admin/kg/requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: manualName.value.trim() }),
  })
  const body = await readJson(response)
  saving.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '登记失败'))
    return
  }
  ElMessage.success(body.message || '已登记')
  manualVisible.value = false
  manualName.value = ''
  reload()
}

async function openBundle() {
  if (!columns.value.length) {
    const response = await api('/kg/import/meta')
    const body = await readJson(response)
    if (!response.ok) {
      ElMessage.error(errorMessage(body, '读不到录入字段'))
      return
    }
    columns.value = body.columns || []
    categories.value = body.categories || []
  }
  const next = {}
  for (const field of formFields.value) next[field.key] = ''
  form.value = next
  bundleVisible.value = true
}

async function submitBundle() {
  acting.value = true
  const response = await api('/admin/kg/requests/' + detail.value.id + '/bundle', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...form.value, name: detail.value.food_key }),
  })
  const body = await readJson(response)
  acting.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '提交失败'))
    return
  }
  ElMessage.success('已进入审核')
  bundleVisible.value = false
  drawer.value = false
  reload()
}

onMounted(reload)
</script>

<style scoped>
.bar, .filters, .actions { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
.bar h2 { margin: 0; flex: 1; }
.hit { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
.why { color: #909399; font-size: 13px; flex: 1; }
.hint { color: #909399; font-size: 13px; }
.form { display: flex; flex-direction: column; gap: 10px; max-height: 420px; overflow: auto; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
</style>
