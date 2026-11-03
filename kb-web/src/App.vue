<template>
  <!-- 登录必须是真的 <form> + 真正的 submit 事件：浏览器（Chrome/Safari）
       只在这种时候才会记住填过的手机号，下次双击输入框弹原生历史下拉。
       记几条、什么时候清，全由浏览器自己决定。 -->
  <form v-if="!access" class="gate" autocomplete="on" @submit.prevent="submitLogin">
    <h1>企业知识库</h1>
    <label>
      手机号
      <input v-model="phone" name="phone" maxlength="11" type="tel" inputmode="numeric" autocomplete="tel" />
    </label>
    <label v-if="captcha.svg">
      图形码
      <img :src="captcha.svg" alt="图形验证码" @click="loadCaptcha" />
      <input v-model="captcha.answer" name="captcha" placeholder="图中答案" autocomplete="off" />
    </label>
    <label>
      验证码
      <input v-model="code" name="code" maxlength="6" autocomplete="one-time-code" />
      <button type="button" class="ghost" :disabled="cooldown > 0" @click="sendCode">
        {{ cooldown > 0 ? cooldown + 's' : '获取验证码' }}
      </button>
    </label>
    <label>
      租户
      <input v-model="loginTenant" name="tenant" autocomplete="on" />
    </label>
    <button type="submit" :disabled="busy">登录</button>
    <p v-if="error" class="error">{{ error }}</p>
  </form>
  <main v-else class="page" @paste="onPaste">
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
      <button type="button" class="ghost" @click="logout">退出</button>
    </header>
    <section v-if="turns.length" class="thread">
      <article v-for="(turn, index) in turns" :key="index" :class="turn.role">
        <p class="who">{{ turn.role === 'user' ? '提问' : '回答' }}</p>
        <div class="reply">
          <p v-if="turn.role === 'assistant' && turn.status && !turn.content" class="status">{{ turn.status }}</p>
          <p v-if="turn.role !== 'assistant'" class="content">{{ turn.content }}</p>
          <div v-else-if="turn.content" class="content md" v-html="renderMarkdown(turn.content)"></div>
          <details v-if="turn.hits && turn.hits.length" class="sources">
            <summary>参考来源 · {{ turn.hits.length }}{{ turn.hits.some(hit => hit.block_kind === 'image') ? '，含图片' : '' }}</summary>
            <div v-for="hit in turn.hits" :key="hit.chunk_id" class="hit">
              <img v-if="hit.preview" :src="hit.preview" alt="" />
              <div>
                <div class="path">{{ hit.heading_path || '正文' }} · {{ hit.block_kind === 'image' ? '图片' : '正文' }}</div>
                <div v-if="hit.text" class="md" v-html="renderMarkdown(hit.text)"></div>
              </div>
            </div>
          </details>
        </div>
      </article>
    </section>
    <label>
      租户
      <select v-model="tenantId" @change="switchTenant">
        <option value="">请选择</option>
        <option v-for="item in tenants" :key="item" :value="item">{{ item }}</option>
      </select>
    </label>
    <label>
      问题
      <textarea v-model="question" rows="4" placeholder="接着上一问也可以，或粘贴、上传一张图"></textarea>
    </label>
    <label>
      图片
      <span class="hint">选择文件，或在页面里直接粘贴（Ctrl / ⌘ + V）</span>
      <input ref="fileEl" type="file" accept="image/*" @change="onFile" />
      <div v-if="preview" class="picked">
        <img :src="preview" alt="待发送的图片" />
        <span class="name">{{ file && file.name }}</span>
        <button type="button" class="ghost" @click="clearImage">移除</button>
      </div>
    </label>
    <button :disabled="busy" @click="ask">提问</button>
    <p v-if="error" class="error">{{ error }}</p>
    </div>
  </main>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { renderMarkdown } from './markdown.js'

const AT = 'ng.kb.at'
const RT = 'ng.kb.rt'
const SESSION_KEY = 'ng.kb.session'
const TENANT_KEY = 'ng.kb.tenant'

const access = ref(localStorage.getItem(AT) || '')
const phone = ref('')
const code = ref('')
const loginTenant = ref('default_renter')
const cooldown = ref(0)
const captcha = ref({ id: '', svg: '', answer: '' })
const tenants = ref([])
const tenantId = ref(localStorage.getItem(TENANT_KEY) || '')
const question = ref('')
const file = ref(null)
const fileEl = ref(null)
const preview = ref('')
const busy = ref(false)
const error = ref('')
const turns = ref([])
const sessions = ref([])
const recent = ref([])
const sessionId = ref(localStorage.getItem(SESSION_KEY) || '')

function saveTokens(body) {
  access.value = body.access_token || ''
  localStorage.setItem(AT, access.value)
  if (body.refresh_token) localStorage.setItem(RT, body.refresh_token)
}

async function authFetch(url, options = {}, retried = false) {
  const headers = new Headers(options.headers || {})
  if (access.value) headers.set('Authorization', 'Bearer ' + access.value)
  const response = await fetch(url, { ...options, headers })
  if (response.status !== 401 || retried || !localStorage.getItem(RT) || String(url).startsWith('/auth/')) {
    return response
  }
  const refreshed = await fetch('/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: localStorage.getItem(RT) }),
  })
  if (!refreshed.ok) {
    logout()
    return response
  }
  saveTokens(await refreshed.json())
  return authFetch(url, options, true)
}

async function loadCaptcha() {
  const response = await fetch('/auth/captcha')
  const body = await response.json().catch(() => ({}))
  if (!response.ok || !body.svg) {
    error.value = '图形验证码加载失败'
    return
  }
  captcha.value = {
    id: body.challenge_id,
    svg: 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(body.svg))),
    answer: '',
  }
}

async function sendCode() {
  error.value = ''
  const payload = { phone: phone.value.trim() }
  if (captcha.value.id) {
    payload.captcha_id = captcha.value.id
    payload.captcha_answer = captcha.value.answer
  }
  const response = await fetch('/auth/sms', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await response.json().catch(() => ({}))
  if (response.status === 428) {
    await loadCaptcha()
    error.value = '请先填写图形验证码'
    return
  }
  if (!response.ok) {
    error.value = (body.detail && body.detail.message) || '验证码发送失败'
    return
  }
  cooldown.value = 60
  const timer = setInterval(() => {
    cooldown.value -= 1
    if (cooldown.value <= 0) clearInterval(timer)
  }, 1000)
}

async function submitLogin() {
  error.value = ''
  busy.value = true
  try {
    const response = await fetch('/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        phone: phone.value.trim(),
        code: code.value.trim(),
        no_create: true,
        tenant: loginTenant.value.trim(),
      }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) {
      error.value = (body.detail && body.detail.message) || '登录失败'
      return
    }
    saveTokens(body)
    tenantId.value = body.tenant || loginTenant.value.trim()
    localStorage.setItem(TENANT_KEY, tenantId.value)
    await loadTenants()
    await loadHistory()
  } finally {
    busy.value = false
  }
}

function logout() {
  const refresh = localStorage.getItem(RT)
  if (refresh) {
    fetch('/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refresh }),
    }).catch(() => {})
  }
  access.value = ''
  localStorage.removeItem(AT)
  localStorage.removeItem(RT)
  fresh()
  phone.value = ''
  code.value = ''
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

function tenantQuery() {
  return 'tenant_id=' + encodeURIComponent(tenantId.value)
}

async function loadTenants() {
  const response = await authFetch('/kb/tenants')
  if (!response.ok) return
  const body = await response.json()
  tenants.value = body.tenants || []
  if (!tenants.value.includes(tenantId.value) && tenants.value.length) {
    tenantId.value = tenants.value[0]
    localStorage.setItem(TENANT_KEY, tenantId.value)
  }
}

async function loadHistory() {
  if (!access.value || !tenantId.value) return
  const [rs, rq] = await Promise.all([
    authFetch('/kb/sessions?' + tenantQuery()),
    authFetch('/kb/recent?' + tenantQuery()),
  ])
  if (rs.ok) sessions.value = (await rs.json()).sessions || []
  if (rq.ok) recent.value = (await rq.json()).questions || []
}

async function openSession(id) {
  const r = await authFetch('/kb/sessions/' + encodeURIComponent(id) + '?' + tenantQuery())
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

function switchTenant() {
  localStorage.setItem(TENANT_KEY, tenantId.value)
  sessionId.value = ''
  turns.value = []
  localStorage.removeItem(SESSION_KEY)
  loadHistory()
}

onMounted(async () => {
  // 上一版把最近登录的手机号存在本地，现在交给浏览器自带的记忆，顺手清掉
  localStorage.removeItem('ng.kb.phones')
  if (!access.value) return
  await loadTenants()
  if (sessionId.value && tenantId.value) {
    const saved = await authFetch('/kb/sessions/' + sessionId.value + '?' + tenantQuery())
    if (!saved.ok) fresh()
    else {
      const body = await saved.json()
      turns.value = body.messages || []
    }
  }
  loadHistory()
})

onBeforeUnmount(clearImage)

function clearImage() {
  file.value = null
  if (preview.value) URL.revokeObjectURL(preview.value)
  preview.value = ''
  if (fileEl.value) fileEl.value.value = ''
}

function adoptImage(blob) {
  if (!blob) return
  const type = blob.type || 'image/png'
  const next = blob instanceof File && blob.name
    ? blob
    : new File([blob], '粘贴图片.' + (type.split('/')[1] || 'png').replace('jpeg', 'jpg'), { type })
  if (preview.value) URL.revokeObjectURL(preview.value)
  file.value = next
  preview.value = URL.createObjectURL(next)
  const input = fileEl.value
  if (input && typeof DataTransfer !== 'undefined') {
    const bag = new DataTransfer()
    bag.items.add(next)
    input.files = bag.files
  }
}

function onFile(event) {
  const picked = event.target.files && event.target.files[0]
  if (!picked) {
    clearImage()
    return
  }
  adoptImage(picked)
}

function imageFromClipboard(event) {
  const data = event.clipboardData
  if (!data) return null
  const files = data.files
  if (files) {
    for (const item of files) {
      if (item.type && item.type.startsWith('image/')) return item
    }
  }
  const items = data.items
  if (!items) return null
  for (const item of items) {
    if (item.type && item.type.startsWith('image/')) {
      const blob = item.getAsFile && item.getAsFile()
      if (blob) return blob
    }
  }
  return null
}

function onPaste(event) {
  const blob = imageFromClipboard(event)
  if (!blob) return
  event.preventDefault()
  adoptImage(blob)
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

async function hydrate(hits) {
  const out = []
  for (const hit of hits) {
    const copy = { ...hit }
    if (hit.image_url) {
      try {
        const response = await authFetch(hit.image_url)
        if (response.ok) copy.preview = URL.createObjectURL(await response.blob())
      } catch {
        /* 图片失败不影响正文 */
      }
    }
    out.push(copy)
  }
  return out
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
  if (sessionId.value) data.set('session_id', sessionId.value)
  if (picked) data.set('image', picked)
  clearImage()
  question.value = ''
  turns.value.push({ role: 'user', content: label })
  turns.value.push({ role: 'assistant', content: '', hits: [], status: '' })
  const answerIndex = turns.value.length - 1
  busy.value = true
  let gotSse = false
  try {
    const response = await authFetch('/kb/ask/stream', { method: 'POST', body: data })
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
      turn.hits = await hydrate(result.hits || [])
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

.gate {
  max-width: 420px;
  margin: 48px auto;
  padding: 24px;
  background: #fff;
  border: 1px solid var(--ng-line);
  border-radius: 14px;
}
.gate input { display: block; width: 100%; margin-top: 6px; box-sizing: border-box; }

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
.user .reply { width: fit-content; max-width: 82%; }
.user .content {
  background: var(--ng-accent-grad);
  color: #fff;
  padding: 11px 16px;
  border-radius: 14px 14px 4px 14px;
  white-space: pre-wrap;
  box-shadow: 0 6px 16px rgba(47, 125, 93, .18);
}

.assistant { display: flex; gap: 12px; align-items: flex-start; }
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
.assistant .reply { flex: 1; min-width: 0; }
.assistant .content {
  margin: 0;
  background: #fff;
  border: 1px solid var(--ng-line);
  border-radius: 4px 14px 14px 14px;
  padding: 14px 16px;
  box-shadow: var(--ng-shadow-sm);
}
.md { overflow-wrap: anywhere; line-height: 1.65; }
.md > :first-child { margin-top: 0; }
.md > :last-child { margin-bottom: 0; }
.md p { margin: 0.55em 0; }
.md h1, .md h2, .md h3, .md h4 { font-size: 15px; margin: 1em 0 0.35em; font-weight: 650; }
.md ul, .md ol { margin: 0.4em 0; padding-left: 1.3em; }
.md li { margin: 0.2em 0; }
.md a { color: var(--ng-accent); }
.md code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.88em; }
.md :not(pre) > code { background: #eef3ee; padding: 1px 5px; border-radius: 4px; }
.md pre {
  margin: 0.7em 0;
  padding: 12px 14px;
  background: #f3f6f3;
  border: 1px solid var(--ng-line);
  border-radius: 8px;
  overflow: auto;
  line-height: 1.5;
}
.md pre code { background: none; padding: 0; font-size: 12.5px; }
.md blockquote {
  margin: 0.6em 0;
  padding-left: 12px;
  border-left: 3px solid var(--ng-line);
  color: var(--ng-ink-2);
}
.md table { border-collapse: collapse; margin: 0.6em 0; display: block; max-width: 100%; overflow: auto; }
.md th, .md td { border: 1px solid var(--ng-line); padding: 4px 8px; }

.who { margin: 0 0 4px; color: var(--ng-ink-3); font-size: 13px; }

.sources {
  margin-top: 8px;
  border: 1px dashed var(--ng-line);
  border-radius: 10px;
}
.sources summary {
  cursor: pointer;
  padding: 8px 12px;
  color: var(--ng-ink-3);
  font-size: 13px;
}
.sources summary:hover { color: var(--ng-accent); }
.hit {
  display: flex;
  gap: 12px;
  background: #fbfdfb;
  border: 1px solid var(--ng-line);
  border-radius: 10px;
  padding: 10px 12px;
  margin: 0 12px 10px;
  min-width: 0;
}
.hit img { width: 168px; height: 126px; object-fit: contain; background: #f3f5f3; border-radius: 8px; flex: none; }
.hit > div { min-width: 0; flex: 1; }
.hit .md { font-size: 13px; color: var(--ng-ink-2); }
.path { color: var(--ng-ink-3); font-size: 13px; margin-bottom: 4px; }

/* 输入区做成一张卡片 */
label { display: block; margin: 14px 0; font-size: 14px; color: var(--ng-ink-2); }
.hint { display: block; margin-top: 4px; font-size: 12px; color: var(--ng-ink-3); }
.picked {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
}
.picked img {
  width: 96px;
  height: 72px;
  object-fit: contain;
  background: #f3f5f3;
  border: 1px solid var(--ng-line);
  border-radius: 8px;
}
.picked .name {
  font-size: 13px;
  color: var(--ng-ink-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

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
