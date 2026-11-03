<template>
  <el-container class="shell">
    <el-aside width="220px">
      <div class="brand">
        <BrandMark :size="26" />
        <span>食疗管理</span>
      </div>
      <el-menu :default-active="active" router>
        <el-menu-item index="/guide"><el-icon><Guide /></el-icon><span>新手入门</span></el-menu-item>
        <el-menu-item index="/overview"><el-icon><Odometer /></el-icon><span>概览</span></el-menu-item>
        <el-menu-item index="/requests"><el-icon><QuestionFilled /></el-icon><span>未收录食物</span></el-menu-item>
        <el-menu-item index="/bundles"><el-icon><Checked /></el-icon><span>补录审核</span></el-menu-item>
        <el-menu-item index="/candidates"><el-icon><Share /></el-icon><span>候选关系</span></el-menu-item>
        <el-menu-item index="/aliases"><el-icon><Warning /></el-icon><span>疑似别名</span></el-menu-item>
        <el-menu-item index="/import-new"><el-icon><Upload /></el-icon><span>批量导入</span></el-menu-item>
        <el-menu-item index="/imports"><el-icon><List /></el-icon><span>导入记录</span></el-menu-item>
        <el-menu-item index="/foods"><el-icon><Search /></el-icon><span>食物查询</span></el-menu-item>
        <el-menu-item index="/users"><el-icon><User /></el-icon><span>用户</span></el-menu-item>
        <el-menu-item index="/tenants"><el-icon><OfficeBuilding /></el-icon><span>租户</span></el-menu-item>
        <el-menu-item index="/customers"><el-icon><UserFilled /></el-icon><span>客户</span></el-menu-item>
        <el-menu-item index="/kb"><el-icon><Reading /></el-icon><span>知识库查询</span></el-menu-item>
        <el-menu-item index="/kb-review"><el-icon><DocumentChecked /></el-icon><span>知识库审核</span></el-menu-item>
        <el-menu-item index="/sms"><el-icon><Message /></el-icon><span>短信记录</span></el-menu-item>
        <el-menu-item index="/trace"><el-icon><TrendCharts /></el-icon><span>调用追踪</span></el-menu-item>
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
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Odometer, TrendCharts, QuestionFilled, Checked, Share, Warning,
  Upload, List, Search, User, OfficeBuilding, UserFilled, Reading,
  DocumentChecked, Message, Guide,
} from '@element-plus/icons-vue'
import BrandMark from '../components/BrandMark.vue'
import { api, readJson, tokens } from '../api'

const route = useRoute()
const router = useRouter()
const mask = ref('')
const active = computed(() => {
  if (route.path.startsWith('/foods')) return '/foods'
  if (route.path.startsWith('/kb-review')) return '/kb-review'
  if (route.path.startsWith('/kb')) return '/kb'
  if (route.path.startsWith('/guide')) return '/guide'
  return route.path
})

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
.shell { min-height: 100vh; background: #f4f7f3; }
.brand { display: flex; align-items: center; gap: 10px; padding: 20px 20px; font-weight: 650; font-size: 17px; letter-spacing: .3px; color: #2f7d5d; }
.brand-mark { color: var(--ng-accent); }
.top { display: flex; justify-content: flex-end; align-items: center; gap: 12px; border-bottom: 1px solid #e3e9e4; background: #fff; }
</style>
