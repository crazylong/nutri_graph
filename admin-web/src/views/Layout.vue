<template>
  <el-container class="shell">
    <el-aside width="200px">
      <div class="brand">食疗管理</div>
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/overview">概览</el-menu-item>
        <el-menu-item index="/requests">未收录食物</el-menu-item>
        <el-menu-item index="/bundles">补录审核</el-menu-item>
        <el-menu-item index="/candidates">候选关系</el-menu-item>
        <el-menu-item index="/aliases">疑似别名</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="top">
        <span>{{ mask }}</span>
        <el-button link @click="logout">退出</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, readJson, tokens } from '../api'

const route = useRoute()
const router = useRouter()
const mask = ref('')

async function logout() {
  if (tokens.refresh) {
    await api('/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: tokens.refresh }),
    })
  }
  tokens.clear()
  router.replace('/login')
}

onMounted(async () => {
  const response = await api('/auth/me')
  const body = await readJson(response)
  if (!response.ok || !body || body.role !== 'admin') {
    router.replace('/login')
    return
  }
  mask.value = body.phone_mask || ''
})
</script>

<style scoped>
.shell { min-height: 100vh; }
.brand { padding: 18px 16px; font-weight: 600; }
.top { display: flex; justify-content: flex-end; align-items: center; gap: 12px; border-bottom: 1px solid #ebeef5; }
</style>
