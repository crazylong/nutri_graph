<template>
  <div>
    <h2>知识库查询</h2>
    <el-form class="filters" inline>
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
          <el-option label="问答类" value="qa" />
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
      <el-table-column label="标题">
        <template #default="{ row }">
          <button type="button" class="kb-title-link" @click="source.open(row)">{{ row.title }}</button>
        </template>
      </el-table-column>
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
      <template #header>
        <span>最近一次离线评估</span>
        <el-tooltip placement="top" effect="dark">
          <template #content>
            用人工准备好的对照题，离线检验知识库问答质量：Recall 看检索能不能捞出正确答案，
            Ragas 看生成答案是否忠实于文档、是否切题。跑一次会调模型，产生费用。
          </template>
          <el-icon class="eval-help"><QuestionFilled /></el-icon>
        </el-tooltip>
      </template>
      <div class="eval-schedule">
        <span class="eval-schedule-tip">用下方按钮手动触发评估；跑一次会调模型、产生费用。</span>
        <div class="eval-actions">
          <el-button type="primary" size="small" :loading="evalBusy" @click="runEval">手动触发评估</el-button>
          <span v-if="evalStatus" class="eval-status">{{ evalStatus }}</span>
        </div>
      </div>
      <p v-if="!evalRun.created_at" class="eval-empty">
        还没有跑过评估。点击「手动触发评估」，用 eval/kb_pairs.jsonl 和 eval/kb_qa.jsonl 里的对照题跑一次。
      </p>
      <template v-else>
        <p class="eval-time">评估时间 {{ formatTime(evalRun.created_at) }}</p>
        <p class="eval-legend">越高越好。绿色达到 85%，黄色达到 70%，红色低于 70%。</p>

        <section class="eval-block">
          <div class="eval-block-head">
            <span class="eval-block-title">生成质量</span>
            <span v-if="ragasMeta" class="eval-block-meta">{{ ragasMeta }}</span>
          </div>
          <p v-if="ragasSkipped" class="eval-note">{{ ragasSkipped }}</p>
          <div v-if="ragasCards.length" class="eval-grid">
            <div v-for="item in ragasCards" :key="item.key" class="eval-metric">
              <div class="eval-score" :class="scoreClass(item.value)">{{ item.text }}</div>
              <div class="eval-name">{{ item.name }}</div>
              <div class="eval-hint">{{ item.hint }}</div>
              <div class="eval-bar"><span :style="barStyle(item.value)"></span></div>
            </div>
          </div>
          <p v-else-if="!ragasSkipped" class="eval-missing">这次没有生成质量样本。</p>
        </section>

        <section class="eval-block">
          <div class="eval-block-head">
            <span class="eval-block-title">检索召回</span>
            <span v-if="recallMeta" class="eval-block-meta">{{ recallMeta }}</span>
          </div>
          <div v-if="recallCards.length" class="eval-grid">
            <div v-for="item in recallCards" :key="item.key" class="eval-metric">
              <div class="eval-score" :class="scoreClass(item.value)">{{ item.text }}</div>
              <div class="eval-name">{{ item.name }}</div>
              <div class="eval-hint">{{ item.hint }}</div>
              <div class="eval-bar"><span :style="barStyle(item.value)"></span></div>
            </div>
          </div>
          <p v-else class="eval-missing">这次没有检索对照题。</p>
        </section>
      </template>
    </el-card>

    <el-dialog v-model="dialog" :title="editing ? '编辑文档' : '添加文档'" width="480px">
      <el-form label-width="80px">
        <el-form-item label="租户">
          <el-select v-model="form.tenant_id" placeholder="请选择租户" style="width: 100%">
            <el-option v-for="name in myTenants" :key="name" :label="name" :value="name" />
          </el-select>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.doc_kind">
            <el-option label="技术类" value="tech" />
            <el-option label="制度类" value="policy" />
            <el-option label="问答类" value="qa" />
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
    <KbSource ref="source" />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { QuestionFilled } from '@element-plus/icons-vue'
import { api, errorMessage, readJson } from '../api'
import KbSource from '../components/KbSource.vue'

const source = ref(null)
const items = ref([])
const evalRun = ref(null)
const evalBusy = ref(false)
const evalStatus = ref('')
const dialog = ref(false)
const editing = ref(null)
const file = ref(null)
const myTenants = ref([])
const filters = reactive({
  tenant_id: '',
  title: '',
  doc_kind: '',
  uploader_phone_mask: '',
  status: '',
})
const form = reactive({ tenant_id: '', title: '', doc_kind: 'policy' })

const KIND_LABELS = { tech: '技术类', policy: '制度类', qa: '问答类' }

function kindLabel(value) {
  return KIND_LABELS[value] || value
}
function statusLabel(value) {
  return { pending: '待审', approved: '已通过', rejected: '已驳回' }[value] || value
}
const RAGAS_FIELDS = [
  { key: 'faithfulness', name: '忠实度', hint: '答案是否依据文档，没有编造' },
  { key: 'answer_relevancy', name: '答案相关性', hint: '回答是否切题' },
  { key: 'llm_context_precision_without_reference', name: '上下文精确度', hint: '捞回的片段是否对准问题' },
  { key: 'abstention_correctness', name: '弃答正确率', hint: '该拒答的题有没有拒答' },
]
const RECALL_FIELDS = [
  { key: 'recall_at_1', name: '前 1 条命中', hint: '第一条就是对照答案' },
  { key: 'recall_at_5', name: '前 5 条命中', hint: '前五条里出现对照答案' },
  { key: 'recall_at_10', name: '前 10 条命中', hint: '前十条里出现对照答案' },
  { key: 'mrr', name: '排序质量', hint: '正确答案越靠前越高，满分 1', asNumber: true },
]

const ragasCards = computed(() => metricCards(evalRun.value && evalRun.value.ragas, RAGAS_FIELDS))
const recallCards = computed(() => metricCards(evalRun.value && evalRun.value.recall, RECALL_FIELDS))
const ragasMeta = computed(() => sampleLine(evalRun.value && evalRun.value.ragas, true))
const recallMeta = computed(() => sampleLine(evalRun.value && evalRun.value.recall, false))
const ragasSkipped = computed(() => {
  const source = evalRun.value && evalRun.value.ragas
  return source && source.skipped ? String(source.skipped) : ''
})

function formatTime(value) {
  const match = String(value || '').match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
  return match ? `${match[1]} ${match[2]}` : (value || '')
}

function sampleLine(source, withBreakdown) {
  if (!source || source.n == null) return ''
  const parts = [`${source.n} 题`]
  const breakdown = withBreakdown && source.breakdown
  if (breakdown) {
    if (breakdown.grounded != null) parts.push(`有据回答 ${breakdown.grounded}`)
    if (breakdown.abstain != null) parts.push(`应弃答 ${breakdown.abstain}`)
  }
  return parts.join(' · ')
}

function metricCards(source, fields) {
  if (!source || typeof source !== 'object') return []
  return fields
    .filter((field) => source[field.key] != null && source[field.key] !== '')
    .map((field) => {
      const value = Number(source[field.key])
      const ok = Number.isFinite(value)
      return {
        key: field.key,
        name: field.name,
        hint: field.hint,
        value: ok ? value : null,
        text: !ok ? '—' : field.asNumber ? value.toFixed(2) : formatPct(value),
      }
    })
}

function formatPct(value) {
  return `${(value * 100).toFixed(1)}%`
}

function scoreClass(value) {
  if (value == null) return ''
  if (value >= 0.85) return 'good'
  if (value >= 0.7) return 'mid'
  return 'low'
}

function barStyle(value) {
  const colors = { good: '#2f7d5d', mid: '#c4922a', low: '#c45656' }
  const width = value == null ? 0 : Math.max(0, Math.min(100, value * 100))
  return { width: `${width}%`, background: colors[scoreClass(value)] || '#c5d0c8' }
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
  await loadEval()
}

async function loadEval() {
  const latest = await api('/admin/kb/eval/latest')
  evalRun.value = await readJson(latest)
}

async function runEval() {
  evalBusy.value = true
  evalStatus.value = ''
  try {
    const response = await api('/admin/kb/eval/run', { method: 'POST' })
    const body = await readJson(response)
    if (!response.ok) {
      ElMessage.error(errorMessage(body, '触发失败'))
      return
    }
    evalStatus.value = '评估已在后台启动，跑完会自动刷新结果'
    // 轮询，最多等 3 分钟
    for (let i = 0; i < 36; i++) {
      await new Promise((r) => setTimeout(r, 5000))
      const st = await api('/admin/kb/eval/status')
      const statusBody = await readJson(st)
      if (statusBody && statusBody.running === false) {
        await loadEval()
        if (statusBody.error) {
          ElMessage.error('评估出错：' + statusBody.error)
        } else if (evalRun.value && evalRun.value.created_at) {
          ElMessage.success('评估完成')
        } else {
          ElMessage.warning('没有评估样本，未生成结果')
        }
        evalStatus.value = ''
        return
      }
    }
    evalStatus.value = '仍在运行中，稍后刷新可见结果'
  } finally {
    evalBusy.value = false
  }
}

async function loadMyTenants() {
  const response = await api('/kb/tenants')
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '租户加载失败'))
    return null
  }
  const names = body.tenants || []
  myTenants.value = names
  return names
}

async function openAdd() {
  editing.value = null
  file.value = null
  form.title = ''
  form.doc_kind = 'policy'
  const names = await loadMyTenants()
  form.tenant_id = names && names.length ? names[0] : ''
  dialog.value = true
}

async function openEdit(row) {
  editing.value = row
  file.value = null
  form.title = row.title
  form.doc_kind = row.doc_kind
  await loadMyTenants()
  form.tenant_id = row.tenant_id
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
  const names = await loadMyTenants()
  if (!names) return
  if (!form.tenant_id || !names.includes(form.tenant_id)) {
    ElMessage.error(names.length ? '所选租户不属于当前账号' : '当前账号没有可用租户')
    form.tenant_id = names[0] || ''
    return
  }
  const response = editing.value
    ? await api('/admin/kb/docs/' + editing.value.id, { method: 'PUT', body: formData() })
    : await api('/admin/kb/docs', { method: 'POST', body: formData() })
  const body = await readJson(response)
  if (response.status === 413) {
    ElMessage.error('文件超过 32MB')
    return
  }
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

<style scoped>
.eval-help {
  margin-left: 8px;
  vertical-align: middle;
  color: #909399;
  cursor: help;
}
.eval-empty {
  color: #909399;
  font-size: 13px;
  line-height: 1.8;
}
.eval-empty code {
  background: #f4f6f4;
  padding: 1px 6px;
  border-radius: 4px;
  color: #2f7d5d;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.eval-time {
  margin: 0;
  color: #3d4f46;
  font-weight: 600;
}
.eval-legend {
  margin: 4px 0 0;
  color: #87948b;
  font-size: 12px;
}
.eval-block {
  margin-top: 16px;
}
.eval-block-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
}
.eval-block-title {
  font-weight: 650;
  color: #1a241e;
}
.eval-block-meta {
  font-size: 13px;
  color: #87948b;
}
.eval-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.eval-metric {
  min-width: 0;
  padding: 12px 12px 10px;
  background: #f7faf8;
  border: 1px solid #e3e9e4;
  border-radius: 10px;
}
.eval-score {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.15;
  font-variant-numeric: tabular-nums;
}
.eval-score.good { color: #2f7d5d; }
.eval-score.mid { color: #b07d1a; }
.eval-score.low { color: #c45656; }
.eval-name {
  margin-top: 6px;
  font-size: 13px;
  font-weight: 650;
  color: #1a241e;
}
.eval-hint {
  margin-top: 2px;
  min-height: 2.8em;
  font-size: 12px;
  line-height: 1.45;
  color: #87948b;
}
.eval-bar {
  height: 4px;
  margin-top: 8px;
  overflow: hidden;
  border-radius: 99px;
  background: #e3e9e4;
}
.eval-bar span {
  display: block;
  height: 100%;
  border-radius: 99px;
}
.eval-note,
.eval-missing {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: #8a6a12;
}
.eval-missing { color: #87948b; }
@media (max-width: 980px) {
  .eval-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.eval-schedule {
  margin-bottom: 10px;
  padding: 8px 12px;
  background: #f0f7f3;
  border: 1px solid #d6e8de;
  border-radius: 6px;
  font-size: 13px;
  color: #55635b;
  line-height: 1.8;
}
.eval-schedule-tip {
  display: block;
  margin-bottom: 6px;
}
.eval-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.eval-status {
  color: #2f7d5d;
  font-size: 13px;
}
</style>
