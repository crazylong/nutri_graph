<template>
  <div v-loading="loading">
    <el-button link @click="router.push('/bundles')">返回列表</el-button>
    <template v-if="bundle">
      <h2>{{ bundle.canonical_name || bundle.food_key }}</h2>
      <p>状态 {{ bundle.status }} · 评分 {{ bundle.score_total }} · 来源 {{ bundle.source_desc }}</p>
      <p v-if="bundle.reject_reason">驳回理由：{{ bundle.reject_reason }}</p>

      <!-- 敏感词命中：审核员看不见命中就等于没提示。
           展示的是按**当前词表**实时重扫的结果，落库时的那份可能已经过时。 -->
      <el-alert
        v-if="hits.length"
        :title="`敏感词命中 ${hits.length} 处`"
        :type="hasBlock ? 'error' : 'warning'"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
      >
        <template #default>
          <span v-if="hasBlock">含阻断级命中，后端会拒绝批准 —— 请先让数据方修改。</span>
          <span v-else>这些是待确认级命中，需要人工看过再决定。</span>
        </template>
      </el-alert>
      <el-table v-if="hits.length" :data="hits" size="small" style="margin-bottom: 12px">
        <el-table-column prop="fieldLabel" label="字段" width="120" />
        <el-table-column prop="word" label="命中词" width="140" />
        <el-table-column label="类别" width="120">
          <template #default="{ row }">{{ categoryLabel(row.category) }}</template>
        </el-table-column>
        <el-table-column label="动作" width="100">
          <template #default="{ row }">
            <el-tag :type="row.action === 'block' ? 'danger' : 'warning'" size="small">
              {{ row.action === 'block' ? '阻断' : '待确认' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <el-table :data="bundle.items || []">
        <el-table-column prop="item_type" label="类型" width="120" />
        <el-table-column prop="field_name" label="字段" />
        <el-table-column prop="field_value" label="值" />
        <el-table-column prop="unit" label="单位" width="80" />
        <el-table-column prop="source_locator" label="出处" />
      </el-table>
      <div v-if="needConfirm" class="confirm-row">
        <el-checkbox v-model="confirmed">我已逐条看过上面的命中，确认可以入库</el-checkbox>
      </div>
      <div class="actions">
        <el-input v-model="reason" placeholder="驳回理由，可空" />
        <el-button
          type="primary"
          :loading="busy"
          :disabled="approveDisabled"
          :title="approveHint"
          @click="approve"
        >通过并合并</el-button>
        <el-button type="danger" :loading="busy" @click="reject">驳回</el-button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const route = useRoute()
const router = useRouter()
const bundle = ref(null)
const loading = ref(true)
const busy = ref(false)
const reason = ref('')
const confirmed = ref(false)

const CATEGORY_LABEL = {
  political: '政治',
  medical_claim: '疗效断言',
  marketing: '营销导流',
  superlative: '绝对化用语',
}
const FIELD_LABEL = {
  name: '名称',
  canonical_name: '名称',
  aliases: '别名',
  english_name: '英文名',
  scientific_name: '学名',
  note: '备注',
  source: '来源',
  source_desc: '原始描述',
  claim_text: '断言原文',
}

// 实时重扫的优先：落库后词表可能更新过，看旧结果会漏
const hits = computed(() => {
  const raw = (bundle.value && (bundle.value.sensitive_hits_live || bundle.value.sensitive_hits)) || []
  return raw.map((h) => ({ ...h, fieldLabel: FIELD_LABEL[h.field] || h.field || '' }))
})
const hasBlock = computed(() => hits.value.some((h) => h.action === 'block'))
const needConfirm = computed(() => !hasBlock.value && hits.value.some((h) => h.action === 'review'))
const approveDisabled = computed(() => hasBlock.value || (needConfirm.value && !confirmed.value))
const approveHint = computed(() => {
  if (hasBlock.value) return '含阻断级命中，后端会拒绝批准'
  if (needConfirm.value && !confirmed.value) return '请先勾选「已逐条看过命中」'
  return ''
})

function categoryLabel(cat) {
  return CATEGORY_LABEL[cat] || cat
}

async function load() {
  const response = await api('/admin/kg/bundles/' + route.params.id)
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '明细加载失败'))
    return
  }
  bundle.value = body
  reason.value = body.reject_reason || ''
}

async function approve() {
  busy.value = true
  const response = await api('/admin/kg/bundles/' + route.params.id + '/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ merge: true, confirm_sensitive: confirmed.value }),
  })
  const body = await readJson(response)
  busy.value = false
  if (!response.ok) {
    // 409 里带的是后端拒绝的原因（命中了哪一类），直接显示给审核员
    ElMessage.error(errorMessage(body, '通过失败'))
    return
  }
  ElMessage.success('已通过')
  router.push('/bundles')
}

async function reject() {
  busy.value = true
  const response = await api('/admin/kg/bundles/' + route.params.id + '/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: reason.value }),
  })
  const body = await readJson(response)
  busy.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '驳回失败'))
    return
  }
  ElMessage.success('已驳回')
  router.push('/bundles')
}

onMounted(load)
</script>

<style scoped>
.actions { display: flex; gap: 8px; margin-top: 12px; }
.actions .el-input { max-width: 360px; }
.confirm-row { margin-top: 8px; }
</style>
