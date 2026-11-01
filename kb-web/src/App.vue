<template>
  <main class="page">
    <header class="bar">
      <h1>企业知识库</h1>
      <button type="button" class="ghost" @click="fresh">新对话</button>
    </header>
    <section v-if="turns.length" class="thread">
      <article v-for="(turn, index) in turns" :key="index" :class="turn.role">
        <p class="who">{{ turn.role === 'user' ? '提问' : '回答' }}</p>
        <p class="content">{{ turn.content }}</p>
        <div v-if="turn.hits && turn.hits.length" class="hits">
          <div v-for="hit in turn.hits" :key="hit.chunk_id" class="hit">
            <img v-if="hit.image_url" :src="hit.image_url" alt="" />
            <div>
              <div class="path">{{ hit.heading_path || '正文' }} · {{ hit.block_kind === 'image' ? '图片' : '正文' }}</div>
              <p>{{ hit.text }}</p>
            </div>
          </div>
        </div>
      </article>
    </section>
    <label>
      租户
      <select v-model="tenantId">
        <option value="">请选择</option>
        <option v-for="item in tenants" :key="item" :value="item">{{ item }}</option>
      </select>
    </label>
    <label>
      问题
      <textarea v-model="question" rows="4" placeholder="接着上一问也可以，或只上传一张图"></textarea>
    </label>
    <label>
      图片
      <input type="file" accept="image/*" @change="onFile" />
    </label>
    <button :disabled="busy" @click="ask">提问</button>
    <p v-if="error" class="error">{{ error }}</p>
  </main>
</template>

<script setup>
import { onMounted, ref } from 'vue'

const CLIENT_KEY = 'ng.kb.client'
const SESSION_KEY = 'ng.kb.session'

const tenants = ref([])
const tenantId = ref('')
const question = ref('')
const file = ref(null)
const busy = ref(false)
const error = ref('')
const turns = ref([])
const sessionId = ref(localStorage.getItem(SESSION_KEY) || '')

function clientId() {
  let value = localStorage.getItem(CLIENT_KEY) || ''
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(value)) {
    value = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now())
    localStorage.setItem(CLIENT_KEY, value)
  }
  return value
}

onMounted(async () => {
  const response = await fetch('/kb/tenants')
  if (response.ok) {
    const body = await response.json()
    tenants.value = body.tenants || []
    if (tenants.value.length === 1) tenantId.value = tenants.value[0]
  }
  if (!sessionId.value) return
  const saved = await fetch('/kb/sessions/' + sessionId.value + '?client_id=' + encodeURIComponent(clientId()))
  if (!saved.ok) {
    fresh()
    return
  }
  const body = await saved.json()
  turns.value = body.messages || []
})

function onFile(event) {
  file.value = event.target.files[0] || null
}

function fresh() {
  sessionId.value = ''
  turns.value = []
  localStorage.removeItem(SESSION_KEY)
}

async function ask() {
  error.value = ''
  if (!tenantId.value) {
    error.value = '请先选择租户'
    return
  }
  const spoken = question.value.trim()
  const data = new FormData()
  data.set('tenant_id', tenantId.value)
  data.set('question', spoken)
  data.set('client_id', clientId())
  if (sessionId.value) data.set('session_id', sessionId.value)
  if (file.value) data.set('image', file.value)
  busy.value = true
  try {
    const response = await fetch('/kb/ask', { method: 'POST', body: data })
    const body = await response.json()
    if (!response.ok) {
      error.value = (body.detail && body.detail.message) || '提问失败'
      if (response.status === 404) fresh()
      return
    }
    if (body.session_id) {
      sessionId.value = body.session_id
      localStorage.setItem(SESSION_KEY, body.session_id)
    }
    turns.value = turns.value.concat([
      { role: 'user', content: spoken || (file.value && file.value.name) || '（上传图片）' },
      { role: 'assistant', content: body.answer || '', hits: body.hits || [] },
    ])
    question.value = ''
  } finally {
    busy.value = false
  }
}
</script>

<style>
body { margin: 0; font-family: "PingFang SC", "Noto Sans SC", sans-serif; background: #f6f4ef; color: #1f2430; }
.page { max-width: 760px; margin: 0 auto; padding: 32px 20px 64px; }
.bar { display: flex; align-items: center; justify-content: space-between; }
h1 { font-size: 28px; margin: 0; }
button.ghost { background: transparent; color: #1f6b4a; border: 1px solid #1f6b4a; }
.thread { margin: 20px 0; }
.user, .assistant { margin: 14px 0; }
.who { margin: 0 0 4px; color: #5c6570; font-size: 13px; }
.content { margin: 0; white-space: pre-wrap; }
.hit { display: flex; gap: 12px; background: #fff; border-radius: 10px; padding: 12px 14px; margin: 10px 0; }
.hit img { width: 168px; height: 126px; object-fit: contain; background: #f3f1ea; border-radius: 8px; flex: none; }
.hit p { margin: 6px 0 0; }
label { display: block; margin: 14px 0; font-size: 14px; }
select, textarea, input[type="file"] { display: block; width: 100%; margin-top: 6px; font: inherit; }
textarea, select { border: 1px solid #d5d0c6; border-radius: 8px; padding: 8px 10px; background: #fff; }
button { background: #1f6b4a; color: #fff; border: 0; border-radius: 8px; padding: 8px 18px; font: inherit; cursor: pointer; }
button:disabled { opacity: 0.6; }
.error { color: #9b2c2c; }
.path { color: #5c6570; font-size: 13px; }
</style>
