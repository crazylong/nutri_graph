<template>
  <div>
    <div class="bar">
      <h2>租户</h2>
    </div>
    <p class="hint">可以点击租户添加用户。</p>
    <div class="filters">
      <el-input v-model="name" placeholder="租户名" style="width: 220px" />
      <el-button type="primary" @click="create">新增</el-button>
    </div>
    <el-table class="pick" :data="items" v-loading="loading" highlight-current-row @current-change="pick">
      <el-table-column prop="name" label="租户名" min-width="180" />
      <el-table-column prop="user_count" label="人数" width="80" />
      <el-table-column prop="created_at" label="创建时间" min-width="180" />
      <el-table-column label="操作" width="100">
        <template #default="{ row }">
          <el-button v-if="row.name !== 'default_renter'" link type="danger" @click.stop="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="current" class="members">
      <h3>{{ current }} 的用户</h3>
      <div class="filters">
        <el-input v-model="phone" placeholder="手机号" style="width: 180px" />
        <el-button @click="addMember">加入</el-button>
      </div>
      <el-table :data="members">
        <el-table-column prop="phone_mask" label="手机号" width="140" />
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column label="角色" width="100">
          <template #default="{ row }">{{ row.role === 'admin' ? '管理员' : '普通用户' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button link type="danger" @click="dropMember(row)">移出</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const name = ref('')
const phone = ref('')
const items = ref([])
const members = ref([])
const current = ref('')
const loading = ref(false)

async function reload() {
  loading.value = true
  const response = await api('/admin/tenants')
  const body = await readJson(response)
  loading.value = false
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '加载失败'))
    return
  }
  items.value = body.items || []
}

async function loadMembers() {
  if (!current.value) {
    members.value = []
    return
  }
  const response = await api('/admin/tenants/' + encodeURIComponent(current.value) + '/users')
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '成员加载失败'))
    return
  }
  members.value = body.items || []
}

function pick(row) {
  current.value = row ? row.name : ''
  loadMembers()
}

async function create() {
  const response = await api('/admin/tenants', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.value.trim() }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '新增失败'))
    return
  }
  name.value = ''
  reload()
}

async function remove(row) {
  const response = await api('/admin/tenants/' + encodeURIComponent(row.name), { method: 'DELETE' })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '删除失败'))
    return
  }
  if (current.value === row.name) current.value = ''
  reload()
  loadMembers()
}

async function addMember() {
  const response = await api('/admin/tenants/' + encodeURIComponent(current.value) + '/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ phone: phone.value.trim() }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '加入失败'))
    return
  }
  phone.value = ''
  reload()
  loadMembers()
}

async function dropMember(row) {
  const response = await api(
    '/admin/tenants/' + encodeURIComponent(current.value) + '/users/' + row.user_id,
    { method: 'DELETE' },
  )
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '移出失败'))
    return
  }
  reload()
  loadMembers()
}

onMounted(reload)
</script>

<style scoped>
.hint {
  margin: -2px 0 14px;
  padding: 8px 12px;
  font-size: 13px;
  color: #256a4d;
  background: #eaf4ee;
  border-radius: 8px;
}
.members { margin-top: 28px; }
.members h3 { margin: 0 0 12px; font-size: 16px; }
.pick :deep(.el-table__row) { cursor: pointer; }
</style>
