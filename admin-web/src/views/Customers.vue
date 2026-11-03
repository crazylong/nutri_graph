<template>
  <div>
    <div class="bar"><h2>客户</h2></div>
    <div class="filters">
      <el-input v-model="phone" placeholder="手机号" style="width: 180px" @keyup.enter="reload" />
      <el-date-picker
        v-model="range"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="创建开始"
        end-placeholder="创建结束"
      />
      <el-button @click="reload">查询</el-button>
    </div>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="phone_mask" label="手机号" width="140" />
      <el-table-column prop="created_at" label="创建时间" min-width="180" />
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
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const phone = ref('')
const range = ref([])
const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)

async function reload() {
  loading.value = true
  const params = new URLSearchParams({ page: String(page.value) })
  if (phone.value.trim()) params.set('phone', phone.value.trim())
  if (range.value && range.value.length === 2) {
    params.set('created_from', range.value[0])
    params.set('created_to', range.value[1])
  }
  const response = await api('/admin/customers?' + params.toString())
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '加载失败'))
    return
  }
  items.value = body.items || []
  total.value = body.total || 0
}

function changePage(next) {
  page.value = next
  reload()
}

onMounted(reload)
</script>
