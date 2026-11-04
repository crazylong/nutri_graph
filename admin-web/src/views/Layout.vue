<template>
  <el-container class="shell">
    <el-aside :width="asideWidth" class="aside" :class="{ open: mobileOpen }">
      <div class="brand">
        <BrandMark :size="26" />
        <span v-if="!collapsed">食疗管理</span>
      </div>
      <el-menu :default-active="active" router :collapse="collapsed" :collapse-transition="false">
        <el-menu-item index="/guide"><el-icon><Guide /></el-icon><span>新手入门</span></el-menu-item>
        <el-menu-item index="/overview"><el-icon><Odometer /></el-icon><span>概览</span></el-menu-item>
        <el-menu-item index="/kb"><el-icon><Reading /></el-icon><span>知识库查询</span></el-menu-item>
        <el-menu-item index="/kb-review"><el-icon><DocumentChecked /></el-icon><span>知识库审核</span></el-menu-item>
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
        <el-menu-item index="/sms"><el-icon><Message /></el-icon><span>短信记录</span></el-menu-item>
        <el-menu-item index="/trace"><el-icon><TrendCharts /></el-icon><span>调用追踪</span></el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="top">
        <el-button link class="menu-btn" @click="toggleMenu">
          <el-icon><component :is="isMobile ? Expand : collapsed ? Expand : Fold" /></el-icon>
        </el-button>
        <span class="mask">{{ mask }}</span>
        <el-button link @click="logout">退出</el-button>
      </el-header>
      <div v-if="isMobile && mobileOpen" class="backdrop" @click="mobileOpen = false"></div>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Odometer, TrendCharts, QuestionFilled, Checked, Share, Warning,
  Upload, List, Search, User, OfficeBuilding, UserFilled, Reading,
  DocumentChecked, Message, Guide, Expand, Fold,
} from '@element-plus/icons-vue'
import BrandMark from '../components/BrandMark.vue'
import { api, readJson, tokens } from '../api'

const route = useRoute()
const router = useRouter()
const mask = ref('')
const collapsed = ref(false)
const isMobile = ref(false)
const mobileOpen = ref(false)
const asideWidth = computed(() => (isMobile.value ? '220px' : collapsed.value ? '64px' : '220px'))
const active = computed(() => {
  if (route.path.startsWith('/foods')) return '/foods'
  if (route.path.startsWith('/kb-review')) return '/kb-review'
  if (route.path.startsWith('/kb')) return '/kb'
  if (route.path.startsWith('/guide')) return '/guide'
  return route.path
})

function toggleMenu() {
  if (isMobile.value) {
    mobileOpen.value = !mobileOpen.value
  } else {
    collapsed.value = !collapsed.value
  }
}

function onResize() {
  isMobile.value = window.innerWidth < 768
  if (!isMobile.value) mobileOpen.value = false
}

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
  onResize()
  window.addEventListener('resize', onResize)
  const response = await api('/auth/me')
  const body = await readJson(response)
  if (!response.ok || !body || body.role !== 'admin') {
    router.replace('/login')
    return
  }
  mask.value = body.phone_mask || ''
})

onBeforeUnmount(() => window.removeEventListener('resize', onResize))
</script>

<style scoped>
.shell { min-height: 100vh; background: #f4f7f3; }
.aside { transition: width .2s; overflow: hidden; }
.brand { display: flex; align-items: center; gap: 10px; padding: 20px 20px; font-weight: 650; font-size: 17px; letter-spacing: .3px; color: #2f7d5d; white-space: nowrap; }
.brand-mark { color: var(--ng-accent); }
.top { display: flex; justify-content: space-between; align-items: center; gap: 12px; border-bottom: 1px solid #e3e9e4; background: #fff; }
.top .mask { margin-left: auto; }
.menu-btn { display: none; }
.backdrop {
  position: fixed;
  inset: 0;
  z-index: 1999;
  background: rgba(26, 36, 30, .35);
}

/* 移动端：侧栏收起为抽屉，顶部出现折叠按钮 */
@media (max-width: 768px) {
  .menu-btn { display: inline-flex; }
  .aside {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    z-index: 2000;
    background: #fff;
    border-right: 1px solid #e3e9e4;
    box-shadow: 2px 0 12px rgba(26, 36, 30, .08);
    transform: translateX(-100%);
    transition: transform .22s;
  }
  .aside.open { transform: translateX(0); }
}
</style>
