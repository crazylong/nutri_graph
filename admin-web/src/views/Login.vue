<template>
  <div class="login">
    <el-card class="card">
      <h1>食疗管理后台</h1>
      <p v-if="denied" class="denied">这个账号不是管理员。请先在库里把 role 改成 admin，然后刷新本页。</p>
      <el-form v-else @submit.prevent="submit">
        <el-form-item label="手机号">
          <el-input v-model="phone" maxlength="11" placeholder="11 位手机号" />
        </el-form-item>
        <el-form-item v-if="captcha.src" label="图形码">
          <div class="captcha">
            <img :src="captcha.src" alt="图形验证码" @click="loadCaptcha" />
            <el-input v-model="captcha.answer" placeholder="图中答案" />
          </div>
        </el-form-item>
        <el-form-item label="验证码">
          <div class="code">
            <el-input v-model="code" maxlength="6" />
            <el-button :disabled="cooldown > 0" @click="sendCode">
              {{ cooldown > 0 ? cooldown + 's' : '获取验证码' }}
            </el-button>
          </div>
        </el-form-item>
        <el-button type="primary" :loading="busy" native-type="submit" style="width:100%">登录</el-button>
        <p v-if="message" class="msg">{{ message }}</p>
      </el-form>
      <el-button v-if="denied" style="width:100%" @click="logout">退出并换账号</el-button>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, deviceFp, errorMessage, readJson, tokens } from '../api'

const router = useRouter()
const phone = ref('')
const code = ref('')
const message = ref('')
const busy = ref(false)
const denied = ref(false)
const cooldown = ref(0)
const captcha = reactive({ id: '', src: '', answer: '' })
let timer = null

async function loadCaptcha() {
  const response = await api('/auth/captcha')
  const body = await readJson(response)
  if (!response.ok || !body?.svg) {
    message.value = '图形验证码加载失败'
    return
  }
  captcha.id = body.challenge_id
  captcha.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(body.svg)))
  captcha.answer = ''
}

async function sendCode() {
  message.value = ''
  const payload = { phone: phone.value.trim(), device_fp: deviceFp() }
  if (captcha.id) {
    payload.captcha_id = captcha.id
    payload.captcha_answer = captcha.answer
  }
  const response = await api('/auth/sms', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await readJson(response)
  if (response.status === 428 && body?.detail?.code === 'captcha_required') {
    await loadCaptcha()
    message.value = '请先填写图形验证码'
    return
  }
  if (!response.ok) {
    if (body?.detail?.code === 'captcha_invalid') await loadCaptcha()
    message.value = errorMessage(body, '验证码发送失败')
    return
  }
  cooldown.value = 60
  timer = setInterval(() => {
    cooldown.value -= 1
    if (cooldown.value <= 0) clearInterval(timer)
  }, 1000)
}

async function submit() {
  busy.value = true
  message.value = ''
  try {
    const response = await api('/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        phone: phone.value.trim(),
        code: code.value.trim(),
        console: true,
      }),
    })
    const body = await readJson(response)
    if (!response.ok) {
      message.value = errorMessage(body, '登录失败')
      return
    }
    tokens.save(body)
    await enterIfAdmin()
  } finally {
    busy.value = false
  }
}

async function enterIfAdmin() {
  const response = await api('/auth/me')
  const body = await readJson(response)
  if (!response.ok) {
    tokens.clear()
    denied.value = false
    return
  }
  if (body.role !== 'admin') {
    denied.value = true
    return
  }
  router.replace('/overview')
}

function logout() {
  tokens.clear()
  denied.value = false
}

onMounted(() => {
  if (tokens.access) enterIfAdmin()
})
</script>

<style scoped>
.login { min-height: 100vh; display: grid; place-items: center; background: #f4f6f8; }
.card { width: 420px; }
h1 { margin: 0 0 16px; font-size: 20px; }
.code, .captcha { display: flex; gap: 8px; width: 100%; }
.captcha img { cursor: pointer; height: 40px; border: 1px solid #dcdfe6; border-radius: 4px; }
.msg, .denied { color: #c45656; font-size: 13px; }
.denied { margin-bottom: 12px; }
</style>
