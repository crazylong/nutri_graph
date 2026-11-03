<template>
  <div>
    <h2>调用追踪</h2>
    <div class="filters stack">
      <el-form inline @submit.prevent="loadRecent">
        <el-form-item label="手机号">
          <el-input v-model="phone" placeholder="用户手机号" clearable @clear="loadRecent" />
        </el-form-item>
        <el-button type="primary" :loading="loading" @click="loadRecent">查询</el-button>
        <span v-if="phoneMask" class="hint">查询号码 {{ phoneMask }}</span>
      </el-form>
      <el-form inline @submit.prevent>
        <el-form-item label="rid">
          <el-input v-model="rid" placeholder="一次请求的 rid" clearable />
        </el-form-item>
        <el-button :loading="loading" @click="loadRid">展开这一次</el-button>
      </el-form>
      <el-form inline @submit.prevent>
        <el-form-item label="session">
          <el-input v-model="sessionId" placeholder="会话 id" clearable />
        </el-form-item>
        <el-button :loading="loading" @click="loadSession">列轮次</el-button>
      </el-form>
    </div>
    <p v-if="error" class="err">{{ error }}</p>
    <el-table :data="turns" v-loading="loading" size="small" style="margin-bottom: 12px">
      <el-table-column label="时间" width="180">
        <template #default="{ row }">{{ formatTime(row.latest_at || row.started_at) }}</template>
      </el-table-column>
      <el-table-column prop="phone_mask" label="手机号" width="130" />
      <el-table-column prop="rid" label="rid" width="140" />
      <el-table-column prop="session_id" label="session" min-width="180" />
      <el-table-column prop="spans" label="span 数" width="90" />
      <el-table-column label="" width="80">
        <template #default="{ row }">
          <el-button v-if="row.rid" link @click="openTurn(row.rid)">展开</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-tree v-if="tree.length" :data="tree" default-expand-all :props="{ label: 'label', children: 'children' }">
      <template #default="{ data }">
        <div class="node">
          <div>{{ data.label }}</div>
          <pre v-if="data.summary" class="sum">{{ data.summary }}</pre>
        </div>
      </template>
    </el-tree>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api, errorMessage, readJson } from '../api'

const rid = ref('')
const sessionId = ref('')
const phone = ref('')
const phoneMask = ref('')
const loading = ref(false)
const error = ref('')
const turns = ref([])
const tree = ref([])

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function labelOf(span) {
  const tokens = (span.prompt_tokens || 0) + (span.completion_tokens || 0)
  const head = `${span.kind || ''} ${span.node || span.purpose || ''}`.trim()
  const extra = span.model ? ` · ${span.model}` : ''
  const cost = tokens ? ` · ${tokens} tok` : ''
  const flag = span.ok === false ? ' · 失败' : ''
  return `${head}${extra}${cost}${flag} · ${span.latency_ms || 0}ms`
}

function toTree(spans) {
  return (spans || []).map((span) => ({
    label: labelOf(span),
    summary: span.summary || '',
    children: toTree(span.children || []),
  }))
}

async function loadRecent() {
  error.value = ''
  tree.value = []
  phoneMask.value = ''
  loading.value = true
  const params = new URLSearchParams()
  if (phone.value.trim()) params.set('phone', phone.value.trim())
  const query = params.toString()
  const response = await api('/admin/trace/recent' + (query ? '?' + query : ''))
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    turns.value = []
    error.value = errorMessage(body, '追踪加载失败')
    return
  }
  turns.value = body.turns || []
  phoneMask.value = body.phone_mask || ''
}

async function loadRid() {
  error.value = ''
  tree.value = []
  if (!rid.value.trim()) return
  loading.value = true
  const response = await api('/admin/trace?rid=' + encodeURIComponent(rid.value.trim()))
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    error.value = errorMessage(body, '追踪加载失败')
    return
  }
  tree.value = toTree(body.spans || [])
}

async function loadSession() {
  error.value = ''
  tree.value = []
  phoneMask.value = ''
  if (!sessionId.value.trim()) return
  loading.value = true
  const response = await api('/admin/trace/sessions?session_id=' + encodeURIComponent(sessionId.value.trim()))
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    turns.value = []
    error.value = errorMessage(body, '会话加载失败')
    return
  }
  const session = sessionId.value.trim()
  turns.value = (body.turns || []).map((row) => ({ ...row, session_id: session }))
}

function openTurn(value) {
  rid.value = value
  loadRid()
}

onMounted(loadRecent)
</script>

<style scoped>
.err { color: #c45656; }
.hint { color: #909399; margin-left: 8px; }
.node { white-space: normal; line-height: 1.5; }
.sum {
  white-space: pre-wrap;
  color: #606266;
  font-size: 12px;
  margin: 4px 0 8px;
  max-width: 720px;
}
</style>
