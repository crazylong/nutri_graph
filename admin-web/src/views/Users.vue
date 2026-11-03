<template>
  <div>
    <div class="bar">
      <h2>用户</h2>
      <el-button type="primary" @click="openCreate">新增</el-button>
    </div>
    <div class="filters">
      <el-input v-model="phone" placeholder="手机号" style="width: 160px" @keyup.enter="reload" />
      <el-input v-model="username" placeholder="用户名" style="width: 160px" @keyup.enter="reload" />
      <el-select v-model="role" clearable placeholder="角色" style="width: 140px">
        <el-option label="管理员" value="admin" />
        <el-option label="普通用户" value="user" />
      </el-select>
      <el-button @click="reload">查询</el-button>
    </div>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="phone_mask" label="手机号" width="140" />
      <el-table-column prop="username" label="用户名" min-width="140" />
      <el-table-column label="角色" width="100">
        <template #default="{ row }">{{ row.role === 'admin' ? '管理员' : '普通用户' }}</template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" min-width="180" />
      <el-table-column prop="last_login_at" label="最近登录" min-width="180" />
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button link @click="openEdit(row)">修改</el-button>
          <el-button v-if="row.role !== 'admin'" link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      layout="total, prev, pager, next"
      :total="total"
      :page-size="20"
      :current-page="page"
      @current-change="changePage"
    />

    <el-dialog v-model="creating" title="新增用户" width="420px">
      <el-form @submit.prevent="create">
        <el-form-item label="手机号"><el-input v-model="form.phone" maxlength="11" /></el-form-item>
        <el-form-item label="用户名"><el-input v-model="form.username" /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role">
            <el-option label="普通用户" value="user" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="creating = false">取消</el-button>
        <el-button type="primary" @click="create">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editing" title="修改用户" width="420px">
      <el-form @submit.prevent="save">
        <el-form-item label="用户名"><el-input v-model="form.username" /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" :disabled="form.role === 'admin' && editLocked">
            <el-option label="普通用户" value="user" :disabled="editLocked" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editing = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'

const phone = ref('')
const username = ref('')
const role = ref('')
const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const creating = ref(false)
const editing = ref(false)
const editLocked = ref(false)
const editingId = ref('')
const form = reactive({ phone: '', username: '', role: 'user' })

async function reload() {
  loading.value = true
  const params = new URLSearchParams({ page: String(page.value) })
  if (phone.value.trim()) params.set('phone', phone.value.trim())
  if (username.value.trim()) params.set('username', username.value.trim())
  if (role.value) params.set('role', role.value)
  const response = await api('/admin/users?' + params.toString())
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

function openCreate() {
  form.phone = ''
  form.username = ''
  form.role = 'user'
  creating.value = true
}

function openEdit(row) {
  editingId.value = row.user_id
  form.username = row.username || ''
  form.role = row.role
  editLocked.value = row.role === 'admin'
  editing.value = true
}

async function create() {
  const response = await api('/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ phone: form.phone.trim(), username: form.username, role: form.role }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '新增失败'))
    return
  }
  creating.value = false
  reload()
}

async function save() {
  const response = await api('/admin/users/' + editingId.value, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: form.username, role: form.role }),
  })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '修改失败'))
    return
  }
  editing.value = false
  reload()
}

async function remove(row) {
  const response = await api('/admin/users/' + row.user_id, { method: 'DELETE' })
  const body = await readJson(response)
  if (!response.ok) {
    ElMessage.error(errorMessage(body, '删除失败'))
    return
  }
  reload()
}

onMounted(reload)
</script>
