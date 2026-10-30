<template>
  <div>
    <div class="bar">
      <h2>短信记录</h2>
    </div>
    <div class="filters">
      <el-input v-model="phone" placeholder="手机号" style="width: 180px" @keyup.enter="reload" />
      <el-date-picker
        v-model="range"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
      />
      <el-button @click="reload">查询</el-button>
      <span v-if="queriedMask">查询号码 {{ queriedMask }}</span>
    </div>
    <el-table :data="items" v-loading="loading">
      <el-table-column label="时间" width="180">
        <template #default="{ row }">{{ row.created_at }}</template>
      </el-table-column>
      <el-table-column prop="phone_mask" label="手机号" width="120" />
      <el-table-column prop="purpose" label="用途" width="80" />
      <el-table-column prop="provider" label="通道" width="90" />
      <el-table-column label="受理" width="70">
        <template #default="{ row }">{{ row.ok ? '是' : '否' }}</template>
      </el-table-column>
      <el-table-column label="送达" width="140">
        <template #default="{ row }">{{ deliveryText(row) }}</template>
      </el-table-column>
      <el-table-column prop="err_code" label="状态码" width="120" />
      <el-table-column prop="err_msg" label="说明" min-width="160" />
      <el-table-column prop="biz_id" label="biz_id" min-width="140" />
      <el-table-column prop="provider_msg_id" label="out_id" min-width="160" />
    </el-table>
    <el-pagination
      layout="total, prev, pager, next"
      :total="total"
      :page-size="20"
      :current-page="page"
      @current-change="changePage"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api, readJson } from '../api'

const phone = ref('')
const range = ref([])
const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const queriedMask = ref('')

function deliveryText(row) {
  if (row.delivery_status === 'pending') return '等待回执'
  if (row.delivery_status === 'delivered') return '已送达'
  if (row.delivery_status === 'failed') return '送达失败'
  if (row.delivery_status === 'none' && row.provider === 'console') return '无回执（本地）'
  return '未回执'
}

async function reload() {
  page.value = 1
  await load()
}

async function changePage(next) {
  page.value = next
  await load()
}

async function load() {
  loading.value = true
  const params = new URLSearchParams()
  params.set('page', String(page.value))
  if (phone.value.trim()) params.set('phone', phone.value.trim())
  if (range.value && range.value[0]) params.set('from', range.value[0])
  if (range.value && range.value[1]) params.set('to', range.value[1])
  const response = await api('/admin/sms?' + params.toString())
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    items.value = []
    total.value = 0
    queriedMask.value = ''
    return
  }
  items.value = body.items || []
  total.value = body.total || 0
  queriedMask.value = body.phone_mask || ''
}

onMounted(load)
</script>
