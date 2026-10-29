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
const gateDev = document.getElementById('gate-dev');
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
const K_CONSENT = 'ng.consent';   // 敏感个人信息单独同意：'1'=同意 / '0'=拒绝

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
  clearHistoryPanel();
  setTimeout(() => phoneEl.focus(), 40);
}

function hideGate(mask) {
  gate.hidden = true;
  whoMask.textContent = mask || localStorage.getItem(K_MASK) || '';
  who.hidden = false;
  loadHistory();
  maybeAskConsent();
}

/* ============================================================
   敏感个人信息单独同意
   健康信息（症状/过敏/生理期等）属敏感个人信息，需单独授权。
   未选择过 → 弹窗；同意 → 存 '1'；拒绝 → 存 '0'（降级为通用回答）。
   拒绝不等于不能登录，产品仍可用，只是不提取个人健康特征。
   ============================================================ */
const consentOverlay = document.getElementById('consent');
const consentAgree = document.getElementById('consent-agree');
const consentDeny = document.getElementById('consent-deny');

function maybeAskConsent() {
  if (!consentOverlay) return;
  if (localStorage.getItem(K_CONSENT) === null) {
    consentOverlay.hidden = false;
  }
}

function setConsent(v) {
  localStorage.setItem(K_CONSENT, v ? '1' : '0');
  if (consentOverlay) consentOverlay.hidden = true;
  input.focus();
}

if (consentAgree) consentAgree.addEventListener('click', () => setConsent(true));
if (consentDeny) consentDeny.addEventListener('click', () => setConsent(false));

async function bootstrap() {
  // 每次打开首页都是新会话。上次的编号只在点进某条历史时才恢复。
  localStorage.removeItem(K_SID);
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
  // 拿不到 SVG 就别去碰 img.src —— 空/坏的 data URI 会直接变成破图
  if (!body.svg) throw new Error('captcha svg missing');
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
    if (!r.ok) gateDev.hidden = true;

    if (r.ok) {
      // 真实下发：回显掩码号让用户确认发对了号，别让人对着空白输入框干等
      const to = body.phone_mask ? `已发送至 ${body.phone_mask}` : '已发送';
      const minutes = Math.max(1, Math.round((body.expires_in || 300) / 60));
      gateMsg.textContent = `${to}，${minutes} 分钟内有效。`;
      gateMsg.className = 'gate-msg ok';
      // dev_code 只有本地 console 通道或平台显式回传时才出现，生产恒为空
      gateDev.textContent = body.dev_code ? `调试通道回填验证码：${body.dev_code}` : '';
      gateDev.hidden = !body.dev_code;
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
    } else if (r.status === 503) {
      // 通道没配好（缺 AK / 签名 / SDK）—— 重试多少次都没用，别让用户白等
      gateMsg.textContent = '短信通道暂时不可用，请稍后再试或联系管理员。';
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

/* ============================================================
   表格：把图谱事实按意图渲染成表格
   吃的是 /chat 返回的 facts 字段（结构化），不是从回答文本里抠 ——
   模型换个说法文本就变了，字段不会。
   ============================================================ */
const FACT_SPEC = {
  nutrition: { title: '营养成分', cols: [['name', '营养素'], ['amount', '含量'], ['level', '证据'], ['claim', '依据']] },
  pair: { title: '推荐搭配', cols: [['name', '食材'], ['detail', '效果'], ['level', '证据'], ['claim', '依据']] },
  incompatible: { title: '不宜同食', cols: [['name', '食材'], ['detail', '原因'], ['level', '证据'], ['claim', '依据']] },
  substitute: { title: '可替代', cols: [['name', '食材'], ['detail', '理由'], ['level', '证据']] },
  contra_population: { title: '人群禁忌', cols: [['name', '人群'], ['symptom', '相关症状'], ['level', '证据'], ['claim', '依据']] },
  contra_item: { title: '禁忌事项', cols: [['name', '事项'], ['level', '证据'], ['claim', '依据']] },
  symptom_remedy: { title: '建议避开', cols: [['name', '食材'], ['population', '人群'], ['level', '证据'], ['claim', '依据']] },
  other: { title: '图谱事实', cols: [['name', '名称'], ['detail', '说明'], ['level', '证据']] },
};

function fmtNum(v) {
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
}

function clip(s, n = 46) {
  const t = String(s ?? '').trim();
  return t.length > n ? t.slice(0, n) + '…' : (t || '—');
}

function levelCell(row) {
  const frag = document.createDocumentFragment();
  const lv = row.evidence_level || '-';
  frag.appendChild(el('span', `chip lv-${lv}`, `${lv} 级`));
  if (row.provisional) frag.appendChild(el('span', 'chip flag', '单源'));
  return frag;
}

function factCell(key, row) {
  if (key === 'level') return levelCell(row);
  if (key === 'amount') {
    if (row.amount === null || row.amount === undefined) return document.createTextNode('—');
    return document.createTextNode(`${fmtNum(row.amount)}${row.unit ? ' ' + row.unit : ''}`);
  }
  return document.createTextNode(clip(row[key]));
}

function renderFacts(facts) {
  if (!facts || !facts.length) return null;
  const groups = new Map();
  facts.forEach((f) => {
    const k = FACT_SPEC[f.intent] ? f.intent : 'other';
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(f);
  });

  const wrap = el('div', 'facts');
  groups.forEach((rows, intent) => {
    const spec = FACT_SPEC[intent] || FACT_SPEC.other;
    const block = el('div', 'facts-block');
    block.appendChild(el('div', 'facts-title', spec.title));
    const table = el('table', 'facts-table');
    const thead = el('thead');
    const htr = el('tr');
    spec.cols.forEach(([, label]) => htr.appendChild(el('th', null, label)));
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = el('tbody');
    rows.forEach((r) => {
      const tr = el('tr');
      spec.cols.forEach(([key]) => {
        const td = el('td');
        td.appendChild(factCell(key, r));
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    block.appendChild(table);
    wrap.appendChild(block);
  });
  return wrap;
}

/* 探索接口没有 facts，直接把边列成一张关系表 */
function renderEdgeTable(g) {
  const edges = (g && g.edges) || [];
  if (!edges.length) return null;
  const names = new Map(((g && g.nodes) || []).map((n) => [n.id, n.name]));
  const block = el('div', 'facts-block');
  block.appendChild(el('div', 'facts-title', '关系明细'));
  const table = el('table', 'facts-table');
  const thead = el('thead');
  const htr = el('tr');
  ['起点', '关系', '终点', '证据', '依据'].forEach((t) => htr.appendChild(el('th', null, t)));
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = el('tbody');
  edges.forEach((e) => {
    const tr = el('tr');
    tr.appendChild(el('td', null, names.get(e.source) || e.source));
    const tdRel = el('td');
    tdRel.appendChild(el('span', e.provisional ? 'chip flag' : 'chip', e.rel_label || e.rel));
    tr.appendChild(tdRel);
    tr.appendChild(el('td', null, names.get(e.target) || e.target));
    const tdLv = el('td');
    tdLv.appendChild(levelCell(e));
    tr.appendChild(tdLv);
    tr.appendChild(el('td', null, clip(e.detail || e.claim)));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  block.appendChild(table);
  return block;
}

/* ============================================================
   图谱可视化：纯 SVG + 自写力导向布局（不引任何第三方库）
   ——这页是 nginx 直出的静态文件，多一个 CDN 依赖就多一个演示时打不开的风险。
   ============================================================ */
const NODE_META = {
  Ingredient: { text: '食材', color: '#2f7d5d' },
  Nutrient: { text: '营养素', color: '#2a5b83' },
  Population: { text: '人群', color: '#b4772a' },
  Symptom: { text: '症状', color: '#7a52a0' },
  Taboo: { text: '禁忌', color: '#b0442f' },
};
const LEVEL_COLOR = { A: '#22684c', B: '#2a5b83', C: '#b4772a', D: '#9a9a9a', '': '#aab2ac' };

function svgEl(tag, attrs = {}) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
  return n;
}

/* Fruchterman-Reingold：斥力 k²/d、引力 d²/k、温度递减。
   锚点（用户问到的食材）钉在中间，其余节点自由散开 —— 一眼能看出「围着谁在展开」。 */
function forceLayout(nodes, edges, w, h) {
  const pos = new Map();
  const anchors = nodes.filter((n) => n.anchor);
  const cx = w / 2;
  const cy = h / 2;
  const spread = Math.min(w * 0.36, 240);

  nodes.forEach((n, i) => {
    if (n.anchor) {
      const idx = anchors.indexOf(n);
      const t = anchors.length > 1 ? idx / (anchors.length - 1) - 0.5 : 0;
      pos.set(n.id, { x: cx + t * spread, y: cy, fx: cx + t * spread, fy: cy });
    } else {
      const a = (i / Math.max(nodes.length, 1)) * Math.PI * 2 + 0.7;
      const r = Math.min(w, h) * (0.26 + 0.14 * Math.random());
      pos.set(n.id, { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r, fx: null, fy: null });
    }
  });

  const k = Math.sqrt((w * h) / (nodes.length + 2));
  const links = edges.filter((e) => pos.has(e.source) && pos.has(e.target));
  const steps = 300;
  let temp = Math.min(w, h) / 9;

  for (let s = 0; s < steps; s += 1) {
    const disp = new Map(nodes.map((n) => [n.id, { x: 0, y: 0 }]));
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = pos.get(nodes[i].id);
        const b = pos.get(nodes[j].id);
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d = Math.hypot(dx, dy);
        if (d < 0.5) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d = 0.5; }
        const f = (k * k) / d;
        const da = disp.get(nodes[i].id);
        const db = disp.get(nodes[j].id);
        da.x += (dx / d) * f; da.y += (dy / d) * f;
        db.x -= (dx / d) * f; db.y -= (dy / d) * f;
      }
    }
    for (const e of links) {
      const a = pos.get(e.source);
      const b = pos.get(e.target);
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = ((d * d) / k) * 0.6;
      const da = disp.get(e.source);
      const db = disp.get(e.target);
      da.x -= (dx / d) * f; da.y -= (dy / d) * f;
      db.x += (dx / d) * f; db.y += (dy / d) * f;
    }
    for (const n of nodes) {
      const p = pos.get(n.id);
      const d0 = disp.get(n.id);
      d0.x += (cx - p.x) * 0.12;
      d0.y += (cy - p.y) * 0.12;
    }
    for (const n of nodes) {
      const p = pos.get(n.id);
      if (p.fx !== null) { p.x = p.fx; p.y = p.fy; continue; }
      const d0 = disp.get(n.id);
      const d = Math.hypot(d0.x, d0.y) || 0.01;
      const step = Math.min(d, temp);
      p.x += (d0.x / d) * step;
      p.y += (d0.y / d) * step;
      const pad = 38;
      p.x = Math.max(pad, Math.min(w - pad, p.x));
      p.y = Math.max(pad, Math.min(h - pad - 10, p.y));
    }
    temp *= 0.975;
  }
  return pos;
}

function legendItem(color, text, dashed) {
  const s = el('span', 'lg');
  const sw = el('i', 'sw');
  if (dashed) {
    sw.style.border = `1.5px dashed ${color}`;
    sw.style.background = 'transparent';
  } else {
    sw.style.background = color;
  }
  s.append(sw, document.createTextNode(text));
  return s;
}

function graphLegend() {
  const box = el('div', 'legend');
  Object.values(NODE_META).forEach((m) => box.appendChild(legendItem(m.color, m.text)));
  box.appendChild(el('span', 'lg-sep'));
  ['A', 'B', 'C', 'D'].forEach((lv) =>
    box.appendChild(legendItem(LEVEL_COLOR[lv], `${lv} 级 · ${LEVEL_LABEL[lv] || ''}`)));
  box.appendChild(legendItem('#b4772a', '单源未交叉验证', true));
  return box;
}

// 证据等级完整说明：面试展示时，让人一眼看懂 A/B/C/D 到底代表什么可信度
function evidenceLevelHelp() {
  const d = el('details', 'level-help');
  d.appendChild(el('summary', null, '证据等级说明'));
  const rows = [
    ['A', '指南 / 临床共识 / 权威机构警示', '权威指南或机构发布，可信度最高'],
    ['B', '实验测定 / 人群研究', '有实测数据或研究支撑'],
    ['C', '传统典籍 / 药食同源目录', '传统典籍或单一来源补录，未做多源交叉验证'],
    ['D', '民间流传 / 网络说法（未验证）', '未经验证，回答时通常会被过滤掉'],
  ];
  const ul = el('ul');
  rows.forEach(([lv, name, desc]) => {
    const li = el('li');
    li.appendChild(legendItem(LEVEL_COLOR[lv], `${lv} 级 · ${name}`));
    li.appendChild(el('span', 'lv-desc', desc));
    ul.appendChild(li);
  });
  d.appendChild(ul);
  const note = el('p', 'lv-note', '等级决定「这条关系能不能用、优先用哪条」：检索时按证据类型过滤（涉及医疗风险的人群禁忌只收 A/B 级），命中后按等级从高到低排序。');
  d.appendChild(note);
  return d;
}

let _graphSeq = 0;

function renderGraph(host, data, opts = {}) {
  host.innerHTML = '';
  const nodes = ((data && data.nodes) || []).filter((n) => n && n.id);
  const edges = ((data && data.edges) || []).filter(
    (e) => nodes.some((n) => n.id === e.source) && nodes.some((n) => n.id === e.target)
  );
  if (!nodes.length) {
    host.appendChild(el('p', 'muted', '（图谱里没有可画的内容）'));
    return null;
  }

  const w = Math.max(host.clientWidth || 640, 300);
  const h = opts.height || (nodes.length > 10 ? 360 : 280);
  const pos = forceLayout(nodes, edges, w, h);
  const seq = (_graphSeq += 1);
  const radiusOf = (id) => {
    const n = nodes.find((x) => x.id === id);
    if (!n) return 18;
    if (n.anchor) return 26;
    return n.label === 'Ingredient' ? 21 : 17;
  };

  const svg = svgEl('svg', { viewBox: `0 0 ${w} ${h}`, class: 'graph-svg', height: h, width: '100%' });
  const defs = svgEl('defs');
  const markerOf = new Map();
  [...new Set(edges.map((e) => LEVEL_COLOR[e.evidence_level] || LEVEL_COLOR['']))].forEach((c, i) => {
    const id = `g-arrow-${seq}-${i}`;
    const m = svgEl('marker', {
      id, viewBox: '0 0 8 8', refX: 7, refY: 4,
      markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse',
    });
    m.appendChild(svgEl('path', { d: 'M0,1 L7,4 L0,7 z', fill: c }));
    defs.appendChild(m);
    markerOf.set(c, `url(#${id})`);
  });
  svg.appendChild(defs);

  const gEdges = svgEl('g', { class: 'g-edges' });
  const gNodes = svgEl('g', { class: 'g-nodes' });
  svg.append(gEdges, gNodes);

  const showLabel = edges.length <= 22;
  const edgeEls = edges.map((e) => {
    const color = LEVEL_COLOR[e.evidence_level] || LEVEL_COLOR[''];
    const g = svgEl('g', { class: 'g-edge' + (e.provisional ? ' prov' : '') + (e.path ? ' path' : '') });
    const line = svgEl('line', {
      stroke: color,
      'stroke-width': e.path ? 2.4 : 1.6,
      'marker-end': markerOf.get(color) || '',
    });
    if (e.provisional) line.setAttribute('stroke-dasharray', '5 4');
    g.appendChild(line);
    let label = null;
    if (showLabel) {
      label = svgEl('text', { class: 'g-edge-label', fill: color, 'text-anchor': 'middle' });
      label.textContent = e.rel_label || e.rel || '';
      g.appendChild(label);
    }
    const title = svgEl('title');
    const srcName = (nodes.find((n) => n.id === e.source) || {}).name || e.source;
    const dstName = (nodes.find((n) => n.id === e.target) || {}).name || e.target;
    title.textContent =
      `${srcName} —${e.rel_label || e.rel}→ ${dstName}｜${e.evidence_level || '-'} 级`
      + `${e.provisional ? '（单源）' : ''}\n${e.detail || e.claim || ''}`;
    g.appendChild(title);
    gEdges.appendChild(g);
    return { e, line, label };
  });

  const nodeEls = nodes.map((n) => {
    const meta = NODE_META[n.label] || { text: n.label || '节点', color: '#8b968e' };
    const r = radiusOf(n.id);
    const g = svgEl('g', { class: 'g-node' + (n.anchor ? ' anchor' : '') + (n.hop === 2 ? ' far' : '') });
    g.appendChild(svgEl('circle', {
      r, fill: meta.color, stroke: '#fff', 'stroke-width': 2,
      'fill-opacity': n.hop === 2 ? 0.5 : 1,
    }));
    const text = svgEl('text', { class: 'g-node-label', 'text-anchor': 'middle', y: r + 15 });
    text.textContent = n.name;
    g.appendChild(text);
    const title = svgEl('title');
    title.textContent = `${meta.text}｜${n.name}${n.props && n.props.nature ? `（${n.props.nature}性）` : ''}\n${n.id}`;
    g.appendChild(title);
    gNodes.appendChild(g);
    return { n, g, r };
  });

  function apply() {
    edgeEls.forEach(({ e, line, label }) => {
      const a = pos.get(e.source);
      const b = pos.get(e.target);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 1;
      const ra = radiusOf(e.source) + 2;
      const rb = radiusOf(e.target) + 7;
      const x1 = a.x + (dx / d) * ra;
      const y1 = a.y + (dy / d) * ra;
      const x2 = b.x - (dx / d) * rb;
      const y2 = b.y - (dy / d) * rb;
      line.setAttribute('x1', x1); line.setAttribute('y1', y1);
      line.setAttribute('x2', x2); line.setAttribute('y2', y2);
      if (label) {
        label.setAttribute('x', (x1 + x2) / 2);
        label.setAttribute('y', (y1 + y2) / 2 - 5);
      }
    });
    nodeEls.forEach(({ n, g }) => {
      const p = pos.get(n.id);
      g.setAttribute('transform', `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})`);
    });
  }
  apply();

  // 拖拽：按住某个节点可以把它拽开，方便看被压住的边
  nodeEls.forEach(({ n, g }) => {
    let moved = false;
    g.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      moved = false;
      const p = pos.get(n.id);
      const rect = svg.getBoundingClientRect();
      const sx = w / (rect.width || w);
      const sy = h / (rect.height || h);
      const move = (ev2) => {
        moved = true;
        p.x = Math.max(24, Math.min(w - 24, (ev2.clientX - rect.left) * sx));
        p.y = Math.max(24, Math.min(h - 24, (ev2.clientY - rect.top) * sy));
        apply();
      };
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
    g.addEventListener('click', () => {
      if (moved) return;
      if (opts.onNodeClick) opts.onNodeClick(n);
    });
  });

  host.appendChild(svg);
  return { nodes, edges };
}

/* 回答卡片里的图谱：默认展开，一眼看到「答案是从哪张子图上得出来的」 */
function renderGraphBlock(g) {
  const n = (g.nodes || []).length;
  const m = (g.edges || []).length;
  const d = el('details', 'graph-block');
  d.open = true;
  d.appendChild(el('summary', null, `图谱结构 · ${n} 个节点 / ${m} 条关系`));
  const box = el('div', 'graph-box');
  d.appendChild(box);
  d.appendChild(graphLegend());
  d.appendChild(evidenceLevelHelp());

  const actions = el('div', 'graph-actions');
  const btn = el('button', 'ghost-btn', '在图谱探索中打开');
  btn.addEventListener('click', () => {
    const anchors = (g.nodes || []).filter((x) => x.anchor).map((x) => x.name);
    openDrawer(anchors.length ? anchors : undefined);
  });
  actions.appendChild(btn);
  d.appendChild(actions);

  // 要等元素进 DOM 才有宽度，否则 SVG 会按 0 宽布局
  requestAnimationFrame(() => renderGraph(box, g, { height: n > 10 ? 340 : 270 }));
  return d;
}

const scrollDown = () => { thread.scrollTop = thread.scrollHeight; };

function hideEmpty() {
  if (empty && empty.parentNode) empty.remove();
}

function addQuestion(text) {
  hideEmpty();
  const wrap = el('div', 'q');
  // data-role / data-text 供会话内检索用：过滤只匹配自己问过的话
  wrap.dataset.role = 'user';
  wrap.dataset.text = text || '';
  wrap.appendChild(el('div', 'bubble', text));
  thread.appendChild(wrap);
  refreshAskList();
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

  // 结构化表格：和上面那段自然语言吃的是同一批图数据
  const factsBlock = renderFacts(body.facts);
  if (factsBlock) card.appendChild(factsBlock);

  // 子图：把「这次回答用到图的哪一块」直接画出来
  if (body.subgraph && (body.subgraph.nodes || []).length) {
    card.appendChild(renderGraphBlock(body.subgraph));
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
   图谱探索抽屉：加多个食材（2~4 个），看它们之间的两两关系
   ============================================================ */
const drawer = document.getElementById('drawer');
const gGo = document.getElementById('g-go');
const gMsg = document.getElementById('g-msg');
const gGraph = document.getElementById('g-graph');
const gFacts = document.getElementById('g-facts');
const gTagHint = document.getElementById('g-tag-hint');

// 固定的 4 个输入框
const gInputs = [0, 1, 2, 3].map((i) => document.getElementById(`g-f${i}`));

const MAX_FOODS = 4;
const MIN_FOODS = 2;

// 收集所有非空输入框里的食材（去重、保序）
function collectFoods() {
  const out = [];
  gInputs.forEach((inp) => {
    const term = (inp.value || '').trim();
    if (term && !out.includes(term)) out.push(term);
  });
  return out;
}

function updateTagHint() {
  const n = collectFoods().length;
  gTagHint.textContent = n === 0
    ? `填入 2~4 个食材，看它们之间的两两关系`
    : n < MIN_FOODS
      ? `已填 ${n} 个，至少再填 ${MIN_FOODS - n} 个`
      : `已填 ${n} 个，可再加 ${MAX_FOODS - n} 个`;
  gTagHint.className = 'tag-hint' + (n < MIN_FOODS ? ' warn' : '');
}

async function loadGraph(foods) {
  const list = (foods || collectFoods()).filter(Boolean);
  if (list.length < MIN_FOODS) {
    gMsg.className = 'gate-msg warn';
    gMsg.textContent = `至少填 ${MIN_FOODS} 个食材（当前 ${list.length} 个）`;
    return;
  }
  gGo.disabled = true;
  gMsg.className = 'gate-msg';
  gMsg.textContent = '正在查图谱…';
  gGraph.innerHTML = '';
  gFacts.innerHTML = '';

  try {
    const r = await api('/kg/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ foods: list }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = body.detail || {};
      gMsg.className = 'gate-msg warn';
      gMsg.textContent = d.message || `查询失败（${r.status}）`;
      return;
    }

    const g = body.subgraph || { nodes: [], edges: [] };
    const resolved = body.resolved || [];
    const missing = body.missing || [];

    // 未收录的食材一次性汇总提示，不逐条反馈
    if (missing.length) {
      const missTerms = missing.map((m) => `「${m.term}」`).join('、');
      gMsg.className = 'gate-msg warn';
      gMsg.textContent = `${missTerms} 未收录，已反馈给管理员。`;
      gMsg.setAttribute('title', missing.map((m) => m.term).join('、'));
    } else if (!(g.nodes || []).length) {
      gMsg.className = 'gate-msg';
      gMsg.textContent = '这些食材在现有图谱里暂时连不上关系。';
    } else {
      const names = resolved.map((r) => `「${r.name}」`).join('、');
      const truncated = (g.meta || {}).truncated ? '　（节点较多，已按规模截断）' : '';
      gMsg.className = 'gate-msg';
      gMsg.textContent = `${names} 的两两关系` + truncated;
    }

    renderGraph(gGraph, g, {
      height: (g.nodes || []).length > 10 ? 380 : 300,
      onNodeClick: (n) => {
        // 点食材节点：填进第一个空输入框，继续对照
        if (n.label === 'Ingredient') {
          const empty = gInputs.find((inp) => !inp.value.trim());
          if (empty) {
            empty.value = n.name;
            updateTagHint();
          }
        }
      },
    });
    const table = renderEdgeTable(g);
    if (table) {
      gFacts.innerHTML = '';
      const box = el('div', 'facts');
      box.appendChild(table);
      gFacts.appendChild(box);
    }
  } catch {
    gMsg.className = 'gate-msg warn';
    gMsg.textContent = '网络异常，重试一下。';
  } finally {
    gGo.disabled = false;
  }
}

function openDrawer(foods) {
  if (!drawer) return;
  drawer.hidden = false;
  // 清空所有输入框
  gInputs.forEach((inp) => { inp.value = ''; });
  // 有预设食材（快捷按钮）就填进去
  if (foods && foods.length) {
    foods.forEach((f, i) => {
      if (gInputs[i]) gInputs[i].value = f;
    });
  }
  updateTagHint();
  gMsg.className = 'gate-msg';
  gMsg.textContent = '';
}

if (drawer) {
  document.getElementById('drawer-close').addEventListener('click', () => {
    drawer.hidden = true;
  });
  document.getElementById('open-graph').addEventListener('click', () => openDrawer());
  gGo.addEventListener('click', () => loadGraph());
  gInputs.forEach((inp) => {
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) {
        e.preventDefault();
        loadGraph();
      }
    });
    inp.addEventListener('input', updateTagHint);
  });
  document.querySelectorAll('.mini-sample').forEach((btn) =>
    btn.addEventListener('click', () => {
      const foods = (btn.dataset.foods || '').split(',').map((s) => s.trim()).filter(Boolean);
      openDrawer(foods);
    }));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !drawer.hidden) drawer.hidden = true;
  });

  // 左侧拖拽拉伸抽屉宽度（right 固定，改 width 即往左延伸）
  const resizer = document.getElementById('drawer-resizer');
  const MIN_W = 360;
  const MAX_W = 900;
  if (resizer) {
    resizer.addEventListener('mousedown', (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = drawer.getBoundingClientRect().width;
      resizer.classList.add('dragging');
      document.body.classList.add('drawer-resizing');

      const onMove = (ev) => {
        // right:0，所以往左拖（clientX 变小）宽度变大
        const w = startW + (startX - ev.clientX);
        drawer.style.width = `${Math.min(MAX_W, Math.max(MIN_W, w))}px`;
      };
      const onUp = () => {
        resizer.classList.remove('dragging');
        document.body.classList.remove('drawer-resizing');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
}

/* ============================================================
   历史会话
   登录成功和刷新后已有令牌，都从 hideGate 进来，所以两条路拉同一次。
   ============================================================ */
const historyEl = document.getElementById('history');
const sessionListEl = document.getElementById('session-list');
const recentListEl = document.getElementById('recent-list');
const askListEl = document.getElementById('ask-list');
const threadToolsEl = document.getElementById('thread-tools');
const filterInputEl = document.getElementById('thread-filter');
const filterCountEl = document.getElementById('filter-count');

function clearHistoryPanel() {
  if (sessionListEl) sessionListEl.replaceChildren();
  if (recentListEl) recentListEl.replaceChildren();
  if (historyEl) historyEl.classList.remove('open');
}

/* ============================================================
   会话内检索（需求 5 的 A + D）
   只操作**已经加载**的消息：会话内容是敏感个人信息，
   不为了一个搜索框再把原文送一次网。
   ============================================================ */
let filterHits = [];   // 命中的用户消息元素
let filterPos = -1;

function userBubbles() {
  return Array.from(thread.querySelectorAll('[data-role="user"]'));
}

function refreshAskList() {
  if (!askListEl) return;
  askListEl.replaceChildren();
  const items = userBubbles();
  if (threadToolsEl) threadToolsEl.hidden = items.length === 0;
  if (!items.length) {
    askListEl.appendChild(el('p', 'history-empty', '这一轮还没有提问'));
    return;
  }
  items.forEach((node, i) => {
    const btn = el('button', 'ask-item');
    btn.type = 'button';
    btn.appendChild(el('span', 'ask-no', String(i + 1)));
    btn.appendChild(el('span', 'ask-text', clip(node.dataset.text || '', 20)));
    btn.addEventListener('click', () => {
      node.scrollIntoView({ behavior: 'smooth', block: 'center' });
      flash(node);
    });
    askListEl.appendChild(btn);
  });
}

function flash(node) {
  node.classList.remove('flash');
  // 强制回流，连续点同一条也能再闪一次
  void node.offsetWidth;
  node.classList.add('flash');
  setTimeout(() => node.classList.remove('flash'), 1200);
}

function clearHighlights() {
  thread.querySelectorAll('mark.hit').forEach((m) => {
    const parent = m.parentNode;
    parent.replaceChild(document.createTextNode(m.textContent), m);
    parent.normalize();
  });
}

function applyFilter() {
  if (!filterInputEl) return;
  const q = (filterInputEl.value || '').trim();
  clearHighlights();
  filterHits = [];
  filterPos = -1;
  if (!q) {
    if (filterCountEl) filterCountEl.textContent = '';
    return;
  }
  const needle = q.toLowerCase();
  userBubbles().forEach((node) => {
    const bubble = node.querySelector('.bubble');
    if (!bubble) return;
    const text = node.dataset.text || bubble.textContent || '';
    if (!text.toLowerCase().includes(needle)) return;
    // 高亮：重建 bubble 的内容，命中处包 <mark class="hit">
    bubble.replaceChildren();
    const frag = document.createDocumentFragment();
    let from = 0;
    const lower = text.toLowerCase();
    let at = lower.indexOf(needle);
    while (at >= 0) {
      if (at > from) frag.appendChild(document.createTextNode(text.slice(from, at)));
      frag.appendChild(el('mark', 'hit', text.slice(at, at + needle.length)));
      from = at + needle.length;
      at = lower.indexOf(needle, from);
    }
    if (from < text.length) frag.appendChild(document.createTextNode(text.slice(from)));
    bubble.appendChild(frag);
    filterHits.push(node);
  });
  if (filterCountEl) {
    filterCountEl.textContent = filterHits.length ? `1/${filterHits.length}` : '没有命中';
  }
  if (filterHits.length) jumpFilter(0);
}

function jumpFilter(step) {
  if (!filterHits.length) return;
  filterPos = (filterPos + step + filterHits.length) % filterHits.length;
  const node = filterHits[filterPos];
  node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  flash(node);
  if (filterCountEl) {
    filterCountEl.textContent = `${filterPos + 1}/${filterHits.length}`;
  }
}

if (filterInputEl) {
  filterInputEl.addEventListener('input', applyFilter);
  filterInputEl.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      jumpFilter(ev.shiftKey ? -1 : 1);
    }
  });
  const prev = document.getElementById('filter-prev');
  const next = document.getElementById('filter-next');
  if (prev) prev.addEventListener('click', () => jumpFilter(-1));
  if (next) next.addEventListener('click', () => jumpFilter(1));
}

function formatWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`;
}

function showEmptyThread() {
  thread.replaceChildren();
  if (empty) thread.appendChild(empty);
  // 会话清空后"本轮问过"和过滤框也要跟着收起
  refreshAskList();
}

function addHistoryAnswer(message) {
  hideEmpty();
  thread.appendChild(renderAnswer({ ...message, answer: message.content || '' }));
}

async function loadHistory() {
  if (!tokens.access || !sessionListEl) return;
  try {
    const [rs, rq] = await Promise.all([
      api('/sessions'),
      api('/recent-questions'),
    ]);
    if (rs.ok) renderSessions((await rs.json()).sessions || []);
    if (rq.ok) renderRecent((await rq.json()).questions || []);
  } catch {
    /* 侧栏失败不挡提问 */
  }
}

function renderSessions(rows) {
  sessionListEl.replaceChildren();
  if (!rows.length) {
    sessionListEl.appendChild(el('p', 'history-empty', '还没有对话'));
    return;
  }
  const current = localStorage.getItem(K_SID) || '';
  rows.forEach((row) => {
    const btn = el('button', 'history-item' + (row.id === current ? ' active' : ''));
    btn.type = 'button';
    btn.appendChild(el('span', 'history-title', row.title || '未命名对话'));
    btn.appendChild(el('span', 'history-meta', formatWhen(row.last_at)));
    btn.addEventListener('click', () => openSession(row.id));
    sessionListEl.appendChild(btn);
  });
}

function renderRecent(rows) {
  recentListEl.replaceChildren();
  if (!rows.length) {
    recentListEl.appendChild(el('p', 'history-empty', '还没有提问'));
    return;
  }
  rows.forEach((row) => {
    const btn = el('button', 'recent-item', row.question);
    btn.type = 'button';
    btn.title = row.question;
    btn.addEventListener('click', () => {
      if (historyEl) historyEl.classList.remove('open');
      ask(row.question);
    });
    recentListEl.appendChild(btn);
  });
}

async function openSession(id) {
  const r = await api('/sessions/' + encodeURIComponent(id) + '/messages');
  if (!r.ok) {
    localStorage.removeItem(K_SID);
    showEmptyThread();
    loadHistory();
    return;
  }
  const body = await r.json();
  localStorage.setItem(K_SID, id);
  thread.replaceChildren();
  (body.messages || []).forEach((m) => {
    if (m.role === 'user') addQuestion(m.content);
    else addHistoryAnswer(m);
  });
  if (!(body.messages || []).length) showEmptyThread();
  if (filterInputEl) {
    filterInputEl.value = '';
    filterHits = [];
    filterPos = -1;
    if (filterCountEl) filterCountEl.textContent = '';
  }
  refreshAskList();
  if (historyEl) historyEl.classList.remove('open');
  loadHistory();
  scrollDown();
}

function startNewChat() {
  localStorage.removeItem(K_SID);
  showEmptyThread();
  if (historyEl) historyEl.classList.remove('open');
  loadHistory();
  if (gate.hidden) input.focus();
}

document.getElementById('new-chat').addEventListener('click', startNewChat);
document.getElementById('history-toggle').addEventListener('click', () => {
  historyEl.classList.toggle('open');
});

/* ============================================================
   提问
   ============================================================ */
let busy = false;

async function ask(question) {
  const q = (question ?? input.value).trim();
  if (!q || busy) return;
  if (gate.hidden === false) { phoneEl.focus(); return; }
  // 敏感信息同意未处理时，先把弹窗拿出来，别让提问绕过授权
  if (consentOverlay && !consentOverlay.hidden) { input.focus(); return; }

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
    // 健康信息是否允许收集：同意才允许后端提取个人健康特征
    const consent = localStorage.getItem(K_CONSENT);
    if (consent !== null) payload.consent = consent === '1';
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
      loadHistory();
    } else if (r.status === 404) {
      localStorage.removeItem(K_SID);
      thread.appendChild(renderError(r.status, '这条对话不存在，请新开一轮。'));
      loadHistory();
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
