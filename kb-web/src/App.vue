<template>
  <main class="page">
    <aside class="sidebar">
      <button type="button" class="ghost" @click="fresh">新对话</button>
      <h2>历史会话</h2>
      <p v-if="!sessions.length" class="empty">还没有对话</p>
      <button
        v-for="item in sessions"
        :key="item.id"
        type="button"
        class="history"
        :class="{ active: item.id === sessionId }"
        @click="openSession(item.id)"
      >
        <span class="title">{{ item.title || '未命名对话' }}</span>
        <span class="when">{{ formatWhen(item.last_at) }}</span>
      </button>
      <h2>最近问过</h2>
      <p v-if="!recent.length" class="empty">还没有提问</p>
      <button
        v-for="(item, index) in recent"
        :key="index"
        type="button"
        class="recent"
        @click="askRecent(item.question)"
      >{{ item.question }}</button>
    </aside>
    <div class="main">
    <header class="bar">
      <h1>企业知识库</h1>
      <button type="button" class="ghost" @click="fresh">新对话</button>
    </header>
    <section v-if="turns.length" class="thread">
      <article v-for="(turn, index) in turns" :key="index" :class="turn.role">
        <p class="who">{{ turn.role === 'user' ? '提问' : '回答' }}</p>
        <p v-if="turn.role === 'assistant' && turn.status && !turn.content" class="status">{{ turn.status }}</p>
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
      <input ref="fileEl" type="file" accept="image/*" @change="onFile" />
    </label>
    <button :disabled="busy" @click="ask">提问</button>
    <p v-if="error" class="error">{{ error }}</p>
    </div>
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
const fileEl = ref(null)
const busy = ref(false)
const error = ref('')
const turns = ref([])
const sessions = ref([])
const recent = ref([])
const sessionId = ref(localStorage.getItem(SESSION_KEY) || '')

function clientId() {
  let value = localStorage.getItem(CLIENT_KEY) || ''
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(value)) {
    value = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now())
    localStorage.setItem(CLIENT_KEY, value)
  }
  return value
}

function formatWhen(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const month = date.getMonth() + 1
  const day = date.getDate()
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return month + '/' + day + ' ' + hours + ':' + minutes
}

async function loadHistory() {
  if (!clientId()) return
  const [rs, rq] = await Promise.all([
    fetch('/kb/sessions?client_id=' + encodeURIComponent(clientId())),
    fetch('/kb/recent?client_id=' + encodeURIComponent(clientId())),
  ])
  if (rs.ok) sessions.value = (await rs.json()).sessions || []
  if (rq.ok) recent.value = (await rq.json()).questions || []
}

async function openSession(id) {
  const r = await fetch('/kb/sessions/' + encodeURIComponent(id) + '?client_id=' + encodeURIComponent(clientId()))
  if (!r.ok) {
    fresh()
    return
  }
  const body = await r.json()
  sessionId.value = id
  localStorage.setItem(SESSION_KEY, id)
  turns.value = body.messages || []
  error.value = ''
  loadHistory()
}

onMounted(async () => {
  const response = await fetch('/kb/tenants')
  if (response.ok) {
    const body = await response.json()
    tenants.value = body.tenants || []
    if (tenants.value.length === 1) tenantId.value = tenants.value[0]
  }
  if (sessionId.value) {
    const saved = await fetch('/kb/sessions/' + sessionId.value + '?client_id=' + encodeURIComponent(clientId()))
    if (!saved.ok) fresh()
    else {
      const body = await saved.json()
      turns.value = body.messages || []
    }
  }
  loadHistory()
})

function onFile(event) {
  file.value = event.target.files[0] || null
}

function fresh() {
  sessionId.value = ''
  turns.value = []
  localStorage.removeItem(SESSION_KEY)
  loadHistory()
}

function askRecent(text) {
  if (!tenantId.value) {
    error.value = '请先选择租户'
    return
  }
  question.value = text
  ask()
}

async function readSse(response, onEvent) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let cut
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, cut)
      buf = buf.slice(cut + 2)
      if (!frame || frame.startsWith(':')) continue
      let event = 'message'
      const dataLines = []
      frame.split('\n').forEach((line) => {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      })
      if (!dataLines.length) continue
      onEvent(event, JSON.parse(dataLines.join('')))
    }
  }
}

async function ask() {
  if (busy.value) return
  error.value = ''
  if (!tenantId.value) {
    error.value = '请先选择租户'
    return
  }
  const spoken = question.value.trim()
  const picked = file.value
  const label = spoken || (picked && picked.name) || '（上传图片）'
  const data = new FormData()
  data.set('tenant_id', tenantId.value)
  data.set('question', spoken)
  data.set('client_id', clientId())
  if (sessionId.value) data.set('session_id', sessionId.value)
  if (picked) data.set('image', picked)
  file.value = null
  if (fileEl.value) fileEl.value.value = ''
  question.value = ''
  turns.value.push({ role: 'user', content: label })
  turns.value.push({ role: 'assistant', content: '', hits: [], status: '' })
  const answerIndex = turns.value.length - 1
  busy.value = true
  let gotSse = false
  try {
    const response = await fetch('/kb/ask/stream', { method: 'POST', body: data })
    const ctype = response.headers.get('content-type') || ''
    if (!response.ok || !ctype.includes('text/event-stream')) {
      let body = null
      try { body = await response.json() } catch { /* nginx 或 JSON 错误页 */ }
      turns.value.splice(answerIndex - 1, 2)
      error.value = (body && body.detail && body.detail.message) || (body && body.error) || '提问失败'
      if (response.status === 404) fresh()
      return
    }
    gotSse = true
    let result = null
    await readSse(response, (event, payload) => {
      const turn = turns.value[answerIndex]
      if (!turn) return
      if (event === 'status') turn.status = payload.text || ''
      else if (event === 'text_delta') turn.content += payload.text || ''
      else if (event === 'error') error.value = payload.text || '生成失败'
      else if (event === 'result') result = payload
    })
    const turn = turns.value[answerIndex]
    if (result && turn) {
      if (!turn.content && result.answer) turn.content = result.answer
      turn.hits = result.hits || []
      turn.status = ''
      if (result.session_id) {
        sessionId.value = result.session_id
        localStorage.setItem(SESSION_KEY, result.session_id)
      }
    }
  } catch {
    error.value = '连接被中断，请重试。'
  } finally {
    busy.value = false
    if (gotSse) loadHistory()
  }
}
</script>

<style>
:root {
  --ng-bg: #f4f7f3;
  --ng-panel: #ffffff;
  --ng-ink: #1a241e;
  --ng-ink-2: #55635b;
  --ng-ink-3: #87948b;
  --ng-line: #e3e9e4;
  --ng-accent: #2f7d5d;
  --ng-accent-grad: linear-gradient(135deg, #3a8f6b 0%, #2c7a59 55%, #256a4d 100%);
  --ng-accent-weak: #eaf4ee;
  --ng-shadow-sm: 0 1px 2px rgba(26, 36, 30, .05), 0 2px 6px rgba(26, 36, 30, .04);
  --ng-shadow: 0 1px 2px rgba(26, 36, 30, .05), 0 6px 20px rgba(26, 36, 30, .06);
}

body {
  margin: 0;
  font-family: "PingFang SC", "Noto Sans SC", -apple-system, BlinkMacSystemFont, sans-serif;
  background-color: var(--ng-bg);
  background-image:
    radial-gradient(1000px 520px at 15% -10%, #e7f3ec 0%, rgba(231, 243, 236, 0) 60%),
    radial-gradient(800px 460px at 100% 0%, #eef5ee 0%, rgba(238, 245, 238, 0) 55%);
  background-attachment: fixed;
  color: var(--ng-ink);
  -webkit-font-smoothing: antialiased;
}

.page {
  max-width: 1040px;
  margin: 0 auto;
  padding: 24px 20px 72px;
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.sidebar {
  width: 260px;
  flex: none;
  background: var(--ng-panel);
  border: 1px solid var(--ng-line);
  border-radius: 14px;
  box-shadow: var(--ng-shadow-sm);
  padding: 14px;
  max-height: calc(100vh - 48px);
  overflow: auto;
}
.sidebar h2 {
  margin: 16px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--ng-ink-3);
}
.sidebar .empty { margin: 0; color: var(--ng-ink-3); font-size: 13px; }
.sidebar button.history,
.sidebar button.recent {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  color: var(--ng-ink);
  border: 0;
  box-shadow: none;
  border-radius: 8px;
  padding: 8px 10px;
  margin: 2px 0;
  font-weight: 400;
}
.sidebar button.history:hover,
.sidebar button.recent:hover { background: var(--ng-accent-weak); transform: none; }
.sidebar button.history.active { background: var(--ng-accent-weak); }
.sidebar .title,
.sidebar button.recent {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sidebar .when { display: block; color: var(--ng-ink-3); font-size: 12px; margin-top: 2px; }
.main { flex: 1; min-width: 0; }
@media (max-width: 720px) {
  .page { flex-direction: column; }
  .sidebar { width: 100%; max-height: 220px; }
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  background: rgba(255, 255, 255, .82);
  backdrop-filter: blur(12px);
  border: 1px solid var(--ng-line);
  border-radius: 14px;
  box-shadow: var(--ng-shadow-sm);
  margin-bottom: 20px;
}

h1 {
  font-size: 19px;
  margin: 0;
  font-weight: 650;
  letter-spacing: .2px;
  background: var(--ng-accent-grad);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}

button.ghost {
  background: #fff;
  color: var(--ng-accent);
  border: 1px solid var(--ng-line);
  padding: 7px 15px;
  border-radius: 9px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: .15s;
}
button.ghost:hover { border-color: var(--ng-accent); background: var(--ng-accent-weak); }

.thread { margin: 0 0 22px; }

.user, .assistant { margin: 16px 0; }

.user { display: flex; flex-direction: column; align-items: flex-end; }
.user .who {
  margin: 0 6px 5px 0;
  color: var(--ng-ink-3);
  font-size: 12px;
}
.user .content {
  background: var(--ng-accent-grad);
  color: #fff;
  padding: 11px 16px;
  border-radius: 14px 14px 4px 14px;
  max-width: 82%;
  white-space: pre-wrap;
  box-shadow: 0 6px 16px rgba(47, 125, 93, .18);
}

.assistant { display: flex; gap: 12px; }
.assistant .who {
  flex: 0 0 auto;
  width: 30px; height: 30px;
  border-radius: 9px;
  background: var(--ng-accent-grad);
  color: #fff;
  display: grid; place-items: center;
  font-size: 12px;
  font-weight: 600;
  margin: 3px 0 0;
  box-shadow: 0 3px 10px rgba(47, 125, 93, .18);
}
.assistant .content {
  flex: 1;
  min-width: 0;
  margin: 0;
  white-space: pre-wrap;
  background: #fff;
  border: 1px solid var(--ng-line);
  border-radius: 4px 14px 14px 14px;
  padding: 14px 16px;
  box-shadow: var(--ng-shadow-sm);
}

.who { margin: 0 0 4px; color: var(--ng-ink-3); font-size: 13px; }

.hits { margin-top: 10px; }
.hit {
  display: flex;
  gap: 12px;
  background: #fbfdfb;
  border: 1px solid var(--ng-line);
  border-radius: 10px;
  padding: 12px 14px;
  margin: 8px 0;
}
.hit img { width: 168px; height: 126px; object-fit: contain; background: #f3f5f3; border-radius: 8px; flex: none; }
.hit p { margin: 6px 0 0; }
.path { color: var(--ng-ink-3); font-size: 13px; }

/* 输入区做成一张卡片 */
label { display: block; margin: 14px 0; font-size: 14px; color: var(--ng-ink-2); }

select, textarea, input[type="file"] {
  display: block;
  width: 100%;
  margin-top: 6px;
  font: inherit;
  color: var(--ng-ink);
}
textarea, select {
  border: 1px solid var(--ng-line);
  border-radius: 10px;
  padding: 10px 12px;
  background: #fff;
  outline: none;
  transition: border-color .15s, box-shadow .15s;
}
textarea:focus, select:focus {
  border-color: var(--ng-accent);
  box-shadow: 0 0 0 3px rgba(47, 125, 93, .12);
}

button {
  background: var(--ng-accent-grad);
  color: #fff;
  border: 0;
  border-radius: 10px;
  padding: 10px 22px;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
  box-shadow: 0 3px 10px rgba(47, 125, 93, .18);
  transition: transform .15s, box-shadow .15s;
}
button:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 6px 16px rgba(47, 125, 93, .22); }
button:disabled { opacity: .55; cursor: not-allowed; box-shadow: none; }

.error { color: #b0442f; }
.status { margin: 0 0 6px; color: var(--ng-ink-3); font-size: 13px; }
</style>
