/* nutri_graph 前端交互
   两件事：登录闸门（AUTH_REQUIRED=true 时 /chat 只认已登录用户）+ 按证据等级渲染回答。
   不引任何依赖 —— 这页就是 nginx 直接吐的三个静态文件。 */

const thread = document.getElementById('thread');
const empty = document.getElementById('empty');
const input = document.getElementById('input');
const send = document.getElementById('send');
const dot = document.getElementById('health-dot');

const gate = document.getElementById('gate');
const loginForm = document.getElementById('login-form');
const phoneEl = document.getElementById('phone');
const codeEl = document.getElementById('code');
const sendCodeBtn = document.getElementById('send-code');
const gateMsg = document.getElementById('gate-msg');
const who = document.getElementById('who');
const whoMask = document.getElementById('who-mask');
const captchaField = document.getElementById('captcha-field');
const captchaImg = document.getElementById('captcha-img');
const captchaAnswer = document.getElementById('captcha-answer');

const LEVEL_LABEL = { A: '指南/共识', B: '实测/研究', C: '典籍/单一来源', D: '民间说法' };

/* ============================================================
   令牌管理
   存 localStorage 是权衡：这页没有第三方脚本、也没有把用户输入当 HTML 插入，
   XSS 面很小；换来的是刷新页面不用重新登录。真要更严就上 HttpOnly Cookie，
   但那需要后端配合改（现在令牌是 JSON 返回给前端的）。
   ============================================================ */
const K_AT = 'ng.at';
const K_RT = 'ng.rt';
const K_MASK = 'ng.mask';
const K_SID = 'ng.sid';
const K_DEV = 'ng.device';

function deviceFp() {
  let v = localStorage.getItem(K_DEV);
  if (!v) {
    v = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now());
    localStorage.setItem(K_DEV, v);
  }
  return v;
}

const tokens = {
  get access() { return localStorage.getItem(K_AT) || ''; },
  get refresh() { return localStorage.getItem(K_RT) || ''; },
  save(body) {
    if (body.access_token) localStorage.setItem(K_AT, body.access_token);
    if (body.refresh_token) localStorage.setItem(K_RT, body.refresh_token);
  },
  clear() {
    [K_AT, K_RT, K_MASK, K_SID].forEach((k) => localStorage.removeItem(k));
  }
};

/* ============================================================
   带自动刷新的请求
   401 时用 refresh 换一次新令牌（旋转），成功就重放原请求。
   刷新失败 = 会话真的没了 → 清空并弹登录。
   ============================================================ */
async function api(path, options = {}, retried = false) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (tokens.access && !opts.headers.Authorization) {
    opts.headers.Authorization = `Bearer ${tokens.access}`;
  }

  const r = await fetch(path, opts);
  if (r.status !== 401 || retried || !tokens.refresh) return r;

  const rr = await fetch('/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: tokens.refresh })
  });
  if (!rr.ok) {
    tokens.clear();
    showGate('登录已过期，请重新登录。');
    return r;
  }
  tokens.save(await rr.json());
  return api(path, options, true);   // 只重试一次，避免 refresh 死循环
}

/* ============================================================
   登录闸门
   ============================================================ */
function showGate(msg) {
  gate.hidden = false;
  who.hidden = true;
  gateMsg.textContent = msg || '';
  gateMsg.className = 'gate-msg' + (msg ? ' warn' : '');
  setTimeout(() => phoneEl.focus(), 40);
}

function hideGate(mask) {
  gate.hidden = true;
  whoMask.textContent = mask || localStorage.getItem(K_MASK) || '';
  who.hidden = false;
}

async function bootstrap() {
  if (!tokens.access) return showGate('');
  try {
    const r = await api('/auth/principal');
    if (!r.ok) return showGate('');
    const body = await r.json();
    if (body.kind === 'user') {
      localStorage.setItem(K_MASK, body.phone_mask || '');
      hideGate(body.phone_mask);
    } else {
      // 匿名身份在 AUTH_REQUIRED=true 下进不了 /chat，直接当未登录处理
      tokens.clear();
      showGate('');
    }
  } catch {
    showGate('连不上服务，稍后重试。');
  }
}

let countdown = 0;
let timer = null;

/* 图形验证码：拿到 challenge_id，展示 SVG 图片，发码时带上答案 */
let captchaId = '';

async function fetchCaptcha() {
  const r = await fetch('/auth/captcha');
  if (!r.ok) throw new Error('captcha fetch failed');
  const body = await r.json();
  captchaId = body.challenge_id;
  captchaImg.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(body.svg)));
  captchaField.hidden = false;
  captchaAnswer.value = '';
  captchaAnswer.focus();
}

function hideCaptcha() {
  captchaField.hidden = true;
  captchaId = '';
  captchaAnswer.value = '';
}

captchaImg.addEventListener('click', async () => {
  try { await fetchCaptcha(); } catch { /* 刷新失败保持原图 */ }
});

async function doSendCode() {
  const phone = phoneEl.value.trim();
  if (!/^1[3-9]\d{9}$/.test(phone)) {
    gateMsg.textContent = '手机号格式不对（11 位，1 开头）';
    gateMsg.className = 'gate-msg warn';
    phoneEl.focus();
    return;
  }

  sendCodeBtn.disabled = true;
  gateMsg.textContent = '正在下发…';
  gateMsg.className = 'gate-msg';

  try {
    const payload = { phone, device_fp: deviceFp() };
    if (captchaId) {
      payload.captcha_id = captchaId;
      payload.captcha_answer = captchaAnswer.value.trim();
    }
    const r = await fetch('/auth/sms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const body = await r.json().catch(() => ({}));

    if (r.ok) {
      // 演示通道不真发短信：说明去哪儿看，别让人干等
      gateMsg.textContent =
        (body.dev_code ? `验证码：${body.dev_code}（调试模式回填）` : '已下发。')
        + ' 演示通道不发真实短信，验证码在服务器日志里：'
        + ' journalctl -u nutri-graph | grep 验证码 | tail -1';
      gateMsg.className = 'gate-msg ok';
      hideCaptcha();
      startCountdown(body.interval || 60);
      codeEl.focus();
    } else if (r.status === 428) {
      // 需要图形验证码：拉一张展示，让用户填答案后重发
      const detail = body.detail || {};
      if (detail.code === 'captcha_invalid') {
        // 答案错了，换一张新的
        try { await fetchCaptcha(); } catch { /* ignore */ }
        gateMsg.textContent = detail.message || '图形验证码不正确，请重试';
      } else {
        try { await fetchCaptcha(); } catch { /* ignore */ }
        gateMsg.textContent = detail.message || '请先完成图形验证码';
      }
      gateMsg.className = 'gate-msg warn';
      sendCodeBtn.disabled = false;
    } else if (r.status === 429) {
      gateMsg.textContent = (body.detail && body.detail.message) || '发得太频繁了，等一会儿再试。';
      gateMsg.className = 'gate-msg warn';
      sendCodeBtn.disabled = false;
    } else {
      gateMsg.textContent = (body.detail && body.detail.message) || `发送失败（${r.status}）`;
      gateMsg.className = 'gate-msg warn';
      sendCodeBtn.disabled = false;
    }
  } catch {
    gateMsg.textContent = '网络异常，重试一下。';
    gateMsg.className = 'gate-msg warn';
    sendCodeBtn.disabled = false;
  }
}

sendCodeBtn.addEventListener('click', doSendCode);

function startCountdown(sec) {
  countdown = sec;
  sendCodeBtn.textContent = `${countdown}s`;
  clearInterval(timer);
  timer = setInterval(() => {
    countdown -= 1;
    if (countdown <= 0) {
      clearInterval(timer);
      sendCodeBtn.disabled = false;
      sendCodeBtn.textContent = '获取验证码';
    } else {
      sendCodeBtn.textContent = `${countdown}s`;
    }
  }, 1000);
}

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const phone = phoneEl.value.trim();
  const code = codeEl.value.trim();
  if (!code) return;

  const btn = document.getElementById('login-btn');
  btn.disabled = true;
  gateMsg.textContent = '登录中…';
  gateMsg.className = 'gate-msg';

  try {
    const r = await fetch('/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone, code })
    });
    const body = await r.json().catch(() => ({}));

    if (r.ok) {
      tokens.save(body);
      localStorage.setItem(K_MASK, body.user.phone_mask || '');
      codeEl.value = '';
      gateMsg.textContent = '';
      hideGate(body.user.phone_mask);
      input.focus();
    } else {
      const d = body.detail || {};
      gateMsg.textContent = d.message || `登录失败（${r.status}）`;
      if (d.remaining_attempts !== undefined) {
        gateMsg.textContent += `，还可以试 ${d.remaining_attempts} 次`;
      }
      gateMsg.className = 'gate-msg warn';
    }
  } catch {
    gateMsg.textContent = '网络异常，重试一下。';
    gateMsg.className = 'gate-msg warn';
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('logout').addEventListener('click', async () => {
  const rt = tokens.refresh;
  if (rt) {
    fetch('/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt })
    }).catch(() => {});
  }
  tokens.clear();
  showGate('');
});

/* ============================================================
   健康状态
   ============================================================ */
async function checkHealth() {
  try {
    const r = await fetch('/health', { cache: 'no-store' });
    const body = await r.json();
    const checks = body.checks || {};
    const bad = Object.entries(checks).filter(([, v]) => !v.ok).map(([k]) => k);
    if (body.status === 'ok') {
      dot.className = 'dot ok';
      dot.title = '所有存储正常';
    } else if (bad.length === 1 && bad[0] === 'milvus') {
      // Milvus 只影响向量降级，图谱问答照常 —— 用黄色而不是红色，别吓人
      dot.className = 'dot warn';
      dot.title = 'Milvus 未连接：向量检索降级，图谱问答正常';
    } else {
      dot.className = 'dot bad';
      dot.title = '异常：' + bad.join('、');
    }
  } catch {
    dot.className = 'dot bad';
    dot.title = '接口不可达';
  }
}

/* ============================================================
   渲染
   ============================================================ */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const scrollDown = () => { thread.scrollTop = thread.scrollHeight; };

function hideEmpty() {
  if (empty && empty.parentNode) empty.remove();
}

function addQuestion(text) {
  hideEmpty();
  const wrap = el('div', 'q');
  wrap.appendChild(el('div', 'bubble', text));
  thread.appendChild(wrap);
  scrollDown();
}

function addPending() {
  const wrap = el('div', 'a');
  const card = el('div', 'card');
  const typing = el('div', 'typing');
  typing.append(el('i'), el('i'), el('i'));
  card.appendChild(typing);
  wrap.append(el('div', 'avatar', '食'), card);
  thread.appendChild(wrap);
  scrollDown();
  return wrap;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderAnswer(body) {
  const wrap = el('div', 'a');
  const card = el('div', 'card');
  card.appendChild(el('p', 'answer', body.answer || '（没有返回内容）'));

  const counts = body.evidence_counts || {};
  const levels = Object.keys(counts).filter((k) => counts[k] > 0).sort();
  if (levels.length || body.graph_hit === false) {
    const meta = el('div', 'meta');
    levels.forEach((lv) => {
      meta.appendChild(el('span', `chip lv-${lv}`,
        `${lv} 级 · ${LEVEL_LABEL[lv] || ''} ${counts[lv]} 条`));
    });
    if (body.graph_hit === false) meta.appendChild(el('span', 'chip ghost', '未走图谱'));
    if ((body.answer || '').includes('单一来源')) {
      meta.appendChild(el('span', 'chip flag', '单一来源补录'));
    }
    card.appendChild(meta);
  }

  const basis = body.basis || [];
  if (basis.length) {
    const d = el('details', 'basis');
    d.appendChild(el('summary', null, `依据 ${basis.length} 条`));
    const ul = el('ul');
    basis.forEach((b) => ul.appendChild(el('li', null, b)));
    d.appendChild(ul);
    card.appendChild(d);
  }

  const reqs = body.food_requests || [];
  if (reqs.length) {
    const r = reqs[0];
    const n = el('div', 'notice');
    n.innerHTML =
      `<b>「${escapeHtml(r.food_key || r.term)}」已登记</b>　编号 #${r.request_id}` +
      `　状态 ${escapeHtml(r.status || '')}` +
      `<br>审核通过后就能查到它的营养数据与搭配建议。在这之前我不会给出未核实的内容。`;
    card.appendChild(n);
  }

  if (body.disclaimer) card.appendChild(el('p', 'disclaimer', body.disclaimer));

  wrap.append(el('div', 'avatar', '食'), card);
  return wrap;
}

function renderError(status, detail) {
  const wrap = el('div', 'a');
  const card = el('div', 'card');
  const msg = status === 429
    ? '请求太快了，等一会儿再问。'
    : `出错了（${status}）。${detail || ''}`;
  card.appendChild(el('p', 'answer', msg));
  wrap.append(el('div', 'avatar', '!'), card);
  return wrap;
}

/* ============================================================
   提问
   ============================================================ */
let busy = false;

async function ask(question) {
  const q = (question ?? input.value).trim();
  if (!q || busy) return;
  if (gate.hidden === false) { phoneEl.focus(); return; }

  busy = true;
  send.disabled = true;
  input.value = '';
  autoGrow();

  addQuestion(q);
  const pending = addPending();

  try {
    const payload = { question: q };
    const sid = localStorage.getItem(K_SID);
    if (sid) payload.session_id = sid;
    const r = await api('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    let body = null;
    try { body = await r.json(); } catch { /* 可能是 nginx 的 429 页 */ }

    pending.remove();
    if (r.ok && body) {
      if (body.session_id) localStorage.setItem(K_SID, body.session_id);
      thread.appendChild(renderAnswer(body));
    } else if (r.status === 401) {
      showGate('请先登录。');
    } else {
      thread.appendChild(renderError(r.status, body && (body.detail || body.error)));
    }
  } catch {
    pending.remove();
    thread.appendChild(renderError('网络', '连接被中断，请重试。'));
  } finally {
    busy = false;
    send.disabled = false;
    scrollDown();
    if (gate.hidden) input.focus();
  }
}

/* ============================================================
   输入框
   ============================================================ */
function autoGrow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 132) + 'px';
}

input.addEventListener('input', autoGrow);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    ask();
  }
});
send.addEventListener('click', () => ask());
document.querySelectorAll('.sample').forEach((b) =>
  b.addEventListener('click', () => ask(b.textContent)));

checkHealth();
bootstrap();
