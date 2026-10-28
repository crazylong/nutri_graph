/* 食物导入页
   纯原生，无构建。职责只有三件：
     1) 把文件发给 /kg/import/preview，把逐行结论画出来；
     2) 用户确认后调 /commit；
     3) 把 /kg/import/foods 渲染成卡片。

   为什么不在前端做校验：列名能容错到什么程度、单位能不能换算、
   数值上限是多少，这些规则只应该有一份（kg/importer.py）。
   前端再写一遍，两边一定会不一致，而且一定会有人绕过前端直接打接口。 */

(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };

  // 食物库一页多少条。搜索、分类筛选都走服务端（q / category / offset），
  // 不在前端过滤：导入量超过一页时前端过滤会把后面的数据静默藏掉。
  var PAGE = 60;

  var state = {
    meta: null,
    job: null,
    preview: null,
    foods: [],
    byCat: [],
    cat: "",
    q: "",
    total: 0,      // 服务端匹配总数（不是当前已渲染数）
    allTotal: 0,   // 全库总数，给「全部」这个 chip 用
    seq: 0,        // 请求序号，丢弃过期响应
    timer: null,   // 搜索防抖
  };

  var VERDICT = {
    ok: { cls: "ok", text: "可导入" },
    warn: { cls: "warn", text: "有提醒" },
    error: { cls: "bad", text: "不可导入" },
    duplicate_file: { cls: "skip", text: "重复跳过" },
    duplicate_db: { cls: "over", text: "覆盖" },
  };

  var UNIT = { energy_kcal: "kcal", protein_g: "g", fat_g: "g", carb_g: "g" };

  // ------------------------------------------------------------
  // 小工具
  // ------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function num(v, digits) {
    if (v === null || v === undefined || v === "") return "—";
    var n = Number(v);
    if (!isFinite(n)) return "—";
    var d = digits === undefined ? 1 : digits;
    var s = n.toFixed(d);
    return s.replace(/\.0+$/, "").replace(/(\.\d*[1-9])0+$/, "$1");
  }

  function setMsg(el, text, kind) {
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }

  function setStep(n) {
    var items = document.querySelectorAll("#steps li[data-step]");
    for (var i = 0; i < items.length; i++) {
      var s = Number(items[i].dataset.step);
      items[i].className = s < n ? "done" : s === n ? "on" : "";
    }
  }

  async function jsonOrError(res) {
    var body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (res.ok) return body;
    var msg = (body && body.detail && body.detail.message) || (body && body.detail) || "";
    if (typeof msg !== "string" || !msg) {
      msg = res.status === 413
        ? "上传的内容太大，被网关拦下了（文件请控制在 1MB 内）"
        : "请求失败（" + res.status + "），稍后重试";
    }
    var err = new Error(msg);
    err.code = body && body.detail && body.detail.code;
    throw err;
  }

  // ------------------------------------------------------------
  // 元信息 / 字段说明
  // ------------------------------------------------------------
  async function loadMeta() {
    try {
      var res = await fetch("/kg/import/meta");
      var body = await jsonOrError(res);
      state.meta = body;

      if (body.template) {
        $("#tpl-xlsx").href = body.template.xlsx;
        $("#tpl-csv").href = body.template.csv;
      }
      var L = body.limits || {};
      var kb = L.max_file_kb || 1024;
      var sizeTxt = kb % 1024 === 0 ? kb / 1024 + "MB" : kb + "KB";
      $("#meta-line").innerHTML =
        "模板含 <code>" + (body.columns ? body.columns.length : 23) + "</code> 列" +
        " · 单次最多 <code>" + (L.max_rows || 500) + "</code> 行" +
        " · 文件 <code>" + sizeTxt + "</code> 以内";

      renderSpec(body.columns || []);
      buildManualForm();
      if (body.enabled === false) {
        setMsg($("#upload-msg"), "导入功能当前已关闭。", "bad");
        $("#drop").classList.add("busy");
      }
    } catch (e) {
      // 元信息拿不到不挡上传，模板链接用默认文件名兜底
      renderSpec([]);
    }
  }

  function renderSpec(cols) {
    if (!cols.length) return;
    var html = "";
    cols.forEach(function (c) {
      html +=
        "<tr><td class='nm'>" + esc(c.header) + "</td>" +
        "<td>" + (c.required ? "<span class='badge ok'>必填</span>" : "") + "</td>" +
        "<td>" + esc(c.unit || "") + "</td>" +
        "<td style='color:var(--ink-3)'>" + esc(c.key) + "</td>" +
        "<td style='color:var(--ink-3)'>" + esc(c.hint || "") + "</td></tr>";
    });
    $("#spec-body").innerHTML = html;
  }

  // ------------------------------------------------------------
  // 上传
  // ------------------------------------------------------------
  var busy = false;

  function accept(file) {
    if (!file) return;
    if (busy) return;

    var name = (file.name || "").toLowerCase();
    if (/\.xls$/.test(name)) {
      setMsg($("#upload-msg"), "不支持老版 .xls，请在 Excel 里「另存为 .xlsx」后重试。", "bad");
      return;
    }
    if (!/\.(xlsx|xlsm|csv)$/.test(name)) {
      setMsg($("#upload-msg"), "只支持 .xlsx 或 .csv，请确认文件类型。", "bad");
      return;
    }
    var maxKb = (state.meta && state.meta.limits && state.meta.limits.max_file_kb) || 1024;
    if (file.size > maxKb * 1024) {
      setMsg($("#upload-msg"), "文件 " + Math.round(file.size / 1024) + "KB，超过 " + maxKb + "KB 上限。", "bad");
      return;
    }

    upload(file);
  }

  async function upload(file) {
    busy = true;
    $("#drop").classList.add("busy");
    setMsg($("#upload-msg"), "正在解析「" + file.name + "」…", "");
    $("#box-preview").hidden = true;
    $("#box-result").hidden = true;

    try {
      var fd = new FormData();
      fd.append("file", file, file.name);
      var res = await fetch("/kg/import/preview", { method: "POST", body: fd });
      var body = await jsonOrError(res);

      state.job = body.job_id;
      state.preview = body;
      renderPreview(body);
      setStep(2);
      setMsg($("#upload-msg"), "");
    } catch (e) {
      setMsg($("#upload-msg"), e.message, "bad");
    } finally {
      busy = false;
      $("#drop").classList.remove("busy");
      $("#file").value = "";
    }
  }

  // ------------------------------------------------------------
  // 预览渲染
  // ------------------------------------------------------------
  function renderPreview(body) {
    var s = body.summary || {};
    var pills = [
      { cls: "ok", n: s.importable || 0, t: "可导入" },
      { cls: "warn", n: s.warn || 0, t: "有提醒" },
      { cls: "bad", n: (s.error || 0) + (s.duplicate_file || 0), t: "会跳过" },
    ];
    var sh = "";
    pills.forEach(function (p) {
      sh += "<span class='pill " + p.cls + "'><b>" + p.n + "</b>" + p.t + "</span>";
    });
    sh += "<span class='pill'><b>" + (s.total || 0) + "</b>总行数</span>";
    if (s.duplicate_db) sh += "<span class='pill'><b>" + s.duplicate_db + "</b>覆盖已有</span>";
    $("#sum").innerHTML = sh;

    $("#preview-file").textContent =
      (body.filename || "") + (body.sheet ? " · 工作表「" + body.sheet + "」" : "");

    if (body.unknown_headers && body.unknown_headers.length) {
      $("#unknown-note").innerHTML =
        "<p class='msg' style='color:var(--warn)'>有 " + body.unknown_headers.length +
        " 列没认出来，已忽略：" + body.unknown_headers.map(esc).join("、") +
        "。检查是否拼错了列名，或下载最新模板。</p>";
    } else {
      $("#unknown-note").innerHTML = "";
    }

    var html = "";
    (body.rows || []).forEach(function (r) {
      var v = VERDICT[r.verdict] || VERDICT.error;
      var bad = r.verdict === "error";
      var nut = r.nutrients || {};
      var al = (r.aliases || []).join(" / ");

      html +=
        "<tr" + (bad ? " class='bad'" : "") + ">" +
        "<td class='rn'>" + r.row_no + "</td>" +
        "<td class='nm'>" + esc(r.name || "（空）") +
        (al ? "<div class='pv-al' title='" + esc(al) + "'>" + esc(al) + "</div>" : "") +
        "</td>" +
        "<td>" + esc(r.category || "—") + "</td>" +
        "<td class='num'>" + num(nut.energy_kcal) + "</td>" +
        "<td class='num'>" + num(nut.protein_g) + "</td>" +
        "<td class='num'>" + num(nut.fat_g) + "</td>" +
        "<td class='num'>" + num(nut.carb_g) + "</td>" +
        "<td><span class='badge " + v.cls + "'>" + v.text + "</span></td>" +
        "</tr>";

      // 提示单独占一行：塞在名称列里会把列撑宽，其他行就对不齐了
      var msgs = r.messages || [];
      if (msgs.length) {
        html += "<tr class='mrow" + (bad ? " bad" : "") + "'><td></td>" +
          "<td colspan='7'>";
        msgs.forEach(function (m) {
          var cls = m.level === "error" ? "e" : m.level === "warn" ? "w" : "";
          html += "<div class='rowmsg " + cls + "'>" + esc(m.text) + "</div>";
        });
        html += "</td></tr>";
      }
    });
    $("#pv-body").innerHTML = html;

    var can = s.importable || 0;
    var btn = $("#commit");
    btn.disabled = can === 0;
    btn.textContent = can ? "确认导入 " + can + " 条" : "没有可导入的行";

    var q = body.quota;
    $("#preview-hint").textContent = q
      ? "今天已用 " + q.used + "/" + q.limit + " 次"
      : "";

    $("#box-preview").hidden = false;
    $("#box-preview").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------------
  // 确认入库
  // ------------------------------------------------------------
  async function commit() {
    if (!state.job) return;
    var btn = $("#commit");
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "提交中…";
    setMsg($("#commit-msg"), "");

    try {
      var res = await fetch("/kg/import/" + state.job + "/commit", { method: "POST" });
      var body = await jsonOrError(res);

      $("#r-new").textContent = body.inserted || 0;
      $("#r-upd").textContent = body.updated || 0;
      $("#result-file").textContent = body.filename || "";
      $("#review-note").innerHTML =
        "已进入<b>补录审核队列</b>。审核通过后，这些食物会进图谱，" +
        "问答、图谱探索、食材对比都能查到；被驳回的不会进图谱。" +
        (body.review && body.review.console
          ? " <a href='" + body.review.console + "' target='_blank' rel='noopener'>打开审核台 ↗</a>"
          : "");
      $("#box-result").hidden = false;
      $("#box-preview").hidden = true;
      setStep(4);
      setMsg($("#commit-msg"), "");
      state.job = null;

      await Promise.all([loadFoods(true), loadStats()]);
      $("#box-result").scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (e) {
      setMsg($("#commit-msg"), e.message, "bad");
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  // ------------------------------------------------------------
  // 审核状态
  // ------------------------------------------------------------
  var STATUS = {
    draft:         { text: "草稿",   cls: "s-draft",   title: "还没提交审核" },
    pending_review:{ text: "待审核", cls: "s-pending", title: "在补录审核队列里，通过后进图谱" },
    approved:      { text: "已通过", cls: "s-ok",      title: "审核通过，合并进图谱后即可检索" },
    merged:        { text: "已入库", cls: "s-merged",  title: "已合并进图谱，问答与图谱探索可查" },
    rejected:      { text: "已驳回", cls: "s-bad",     title: "审核驳回，不会进图谱" }
  };

  async function loadStats() {
    try {
      var res = await fetch("/kg/import/stats");
      var body = await jsonOrError(res);
      var pending = body.pending || 0;
      var merged = body.merged || 0;
      var el = $("#queue-hint");
      if (!el) return;
      el.innerHTML =
        (pending
          ? "<b>" + pending + "</b> 条待审核"
          : "<b>0</b> 条待审核") +
        (merged ? " · <b>" + merged + "</b> 条已入库" : "") +
        (body.console
          ? " · <a href='" + body.console + "' target='_blank' rel='noopener'>审核台 ↗</a>"
          : "");
    } catch (e) {
      /* 提示信息，失败不挡流程 */
    }
  }

  // ------------------------------------------------------------
  // 食物库
  // ------------------------------------------------------------
  function foodsUrl(offset) {
    var p = ["limit=" + PAGE, "offset=" + offset];
    var q = state.q.trim();
    if (q) p.push("q=" + encodeURIComponent(q));
    if (state.cat) p.push("category=" + encodeURIComponent(state.cat));
    return "/kg/import/foods?" + p.join("&");
  }

  // reset=true 从第一页重拉（换关键词/换分类）；reset=false 追加下一页
  async function loadFoods(reset) {
    var seq = ++state.seq;
    var offset = reset ? 0 : state.foods.length;
    $("#library").classList.add("busy");

    try {
      var res = await fetch(foodsUrl(offset));
      var body = await jsonOrError(res);
      if (seq !== state.seq) return; // 已经发出更新的请求，这次结果作废

      var items = body.items || [];
      state.foods = reset ? items : state.foods.concat(items);
      state.total = body.count || 0;
      state.byCat = body.by_category || [];
      state.allTotal = state.byCat.reduce(function (s, c) { return s + c.count; }, 0);
      renderChips();
      renderGrid();
    } catch (e) {
      if (seq !== state.seq) return;
      if (reset) {
        state.foods = [];
        state.total = 0;
        $("#lib-grid").innerHTML = "";
      }
      $("#lib-empty").hidden = false;
      $("#lib-empty").textContent = "食物库读不出来：" + e.message;
      $("#lib-more").hidden = true;
    } finally {
      if (seq === state.seq) $("#library").classList.remove("busy");
    }
  }

  function renderChips() {
    var html = "<button class='chip" + (state.cat === "" ? " on" : "") +
      "' data-cat=''>全部<i>" + state.allTotal + "</i></button>";
    state.byCat.forEach(function (c) {
      html += "<button class='chip" + (state.cat === c.category ? " on" : "") +
        "' data-cat='" + esc(c.category) + "'>" + esc(c.category) +
        "<i>" + c.count + "</i></button>";
    });
    $("#lib-chips").innerHTML = html;
  }

  function renderGrid() {
    var list = state.foods;
    var shown = list.length;
    var filtered = !!(state.q.trim() || state.cat);

    $("#lib-count").textContent = state.total
      ? "共 " + state.total + " 条" + (shown < state.total ? " · 已显示 " + shown : "")
      : "";

    // 加载更多：还有没拉到的时候才显示
    var more = $("#lib-more");
    more.hidden = !(state.total > shown);
    more.textContent = "加载更多（还有 " + (state.total - shown) + " 条）";

    if (!shown) {
      $("#lib-grid").innerHTML = "";
      $("#lib-empty").hidden = false;
      $("#lib-empty").textContent = filtered
        ? "没有匹配的食物，换个关键词或分类试试。"
        : "还没有导入任何食物。\n下载模板，填两行再上传，这里就会出现卡片。";
      return;
    }
    $("#lib-empty").hidden = true;

    var html = "";
    list.forEach(function (f) {
      var n = f.nutrients || {};
      var g = function (k) { return n[k] ? n[k].v : null; };
      var al = (f.aliases || []).join(" / ");
      var when = (f.created_at || "").slice(0, 10);

      var st = STATUS[f.status] || STATUS.pending_review;
      html +=
        "<article class='fcard'>" +
        "<div class='top'><span class='nm'>" + esc(f.name) + "</span>" +
        "<span class='st " + st.cls + "' title='" + esc(st.title) + "'>" + st.text + "</span>" +
        "</div>" +
        "<div class='sub2'><span class='cat'>" + esc(f.category || "其他") + "</span>" +
        (f.canonical_id ? "<em>" + esc(f.canonical_id) + "</em>" : "") + "</div>" +
        (al ? "<div class='al' title='" + esc(al) + "'>" + esc(al) + "</div>" : "") +
        "<div class='stats'>" +
        "<div><span>能量</span><b>" + num(g("energy_kcal"), 0) + "<u> kcal</u></b></div>" +
        "<div><span>蛋白</span><b>" + num(g("protein_g")) + "<u> g</u></b></div>" +
        "<div><span>脂肪</span><b>" + num(g("fat_g")) + "<u> g</u></b></div>" +
        "<div><span>碳水</span><b>" + num(g("carb_g")) + "<u> g</u></b></div>" +
        "</div>" +
        "<div class='foot' title='" + esc(f.source || "") + "'>" +
        esc(f.source || "未标注来源") + (when ? " · " + when : "") +
        "</div>" +
        "</article>";
    });
    $("#lib-grid").innerHTML = html;
  }

  // ------------------------------------------------------------
  // 手工录入
  // ------------------------------------------------------------
  // 表单控件按 /kg/import/meta 的 columns / nutrients 生成，
  // 不在前端再抄一份字段表 —— 模板加一列，这里自动多一个框。
  function buildManualForm() {
    var meta = state.meta || {};
    var cats = meta.categories || [];
    var natures = meta.natures || [];

    $("#m-category").innerHTML =
      "<option value=''>选一个</option>" +
      cats.map(function (c) { return "<option value='" + esc(c) + "'>" + esc(c) + "</option>"; }).join("");
    $("#m-nature").innerHTML =
      "<option value=''>不填</option>" +
      natures.map(function (c) { return "<option value='" + esc(c) + "'>" + esc(c) + "</option>"; }).join("");

    var nuts = meta.nutrients || [];
    $("#m-nutrients").innerHTML = nuts.map(function (n) {
      return "<div class='mfield'>" +
        "<label for='m-" + n.key + "'>" + esc(n.label) +
        (n.required ? " <i>*</i>" : "") +
        " <span style='opacity:.6'>" + esc(n.unit || "") + "</span></label>" +
        "<input id='m-" + n.key + "' name='" + n.key + "' type='text' inputmode='decimal' " +
        "placeholder='" + (n.required ? "必填" : "未测定") + "' autocomplete='off'>" +
        "</div>";
    }).join("");
  }

  function manualPayload() {
    var meta = state.meta || {};
    var out = {
      name: $("#m-name").value,
      category: $("#m-category").value,
      nature: $("#m-nature").value,
      aliases: $("#m-aliases").value,
      english_name: $("#m-english").value,
      scientific_name: $("#m-scientific").value,
      source: $("#m-source").value,
      note: $("#m-note").value,
    };
    (meta.nutrients || []).forEach(function (n) {
      out[n.key] = $("#m-" + n.key).value;
    });
    return out;
  }

  function clearManualErrors() {
    $("#manual-errors").innerHTML = "";
    var bad = document.querySelectorAll("#manual-form .bad");
    for (var i = 0; i < bad.length; i++) bad[i].classList.remove("bad");
  }

  // 服务端返回的 field 是列名（如「能量(kcal)」），映射回输入框 id
  function fieldToInput(field) {
    var meta = state.meta || {};
    var cols = meta.columns || [];
    for (var i = 0; i < cols.length; i++) {
      if (cols[i].header === field) {
        var el = document.getElementById("m-" + cols[i].key);
        if (el) return el;
      }
    }
    return null;
  }

  async function submitManual(ev) {
    ev.preventDefault();
    var btn = $("#manual-submit");
    if (btn.disabled) return;

    clearManualErrors();
    setMsg($("#manual-msg"), "提交中…", "");
    btn.disabled = true;

    try {
      var res = await fetch("/kg/import/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(manualPayload()),
      });
      var body = null;
      try { body = await res.json(); } catch (e) { body = null; }

      if (!res.ok) {
        var detail = (body && body.detail) || {};
        var errs = detail.errors || [];
        if (errs.length) {
          $("#manual-errors").innerHTML = errs.map(function (m) {
            return "<div class='errline'><b>" + esc(m.field || "表单") + "</b>：" + esc(m.text) + "</div>";
          }).join("");
          errs.forEach(function (m) {
            var el = fieldToInput(m.field);
            if (el) el.classList.add("bad");
          });
        }
        setMsg($("#manual-msg"), detail.message || ("提交失败（" + res.status + "）"), "bad");
        btn.disabled = false;
        return;
      }

      var warn = (body.warnings || []).length;
      setMsg(
        $("#manual-msg"),
        "已提交审核：" + body.name +
          (body.action === "updated" ? "（覆盖了同名的待审记录）" : "") +
          (warn ? " · 有 " + warn + " 条提醒，审核时会看到" : "") +
          " · 审核台 ↗",
        "good"
      );
      $("#manual-form").reset();
      await Promise.all([loadFoods(true), loadStats()]);
      btn.disabled = false;
    } catch (e) {
      setMsg($("#manual-msg"), "网络出问题了：" + e.message, "bad");
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------
  // 事件绑定
  // ------------------------------------------------------------
  function bind() {
    var file = $("#file");
    file.addEventListener("change", function () {
      if (file.files && file.files[0]) accept(file.files[0]);
    });

    var drop = $("#drop");
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        e.stopPropagation();
        drop.classList.add("over");
      });
    });
    ["dragleave", "dragend"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        drop.classList.remove("over");
      });
    });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      e.stopPropagation();
      drop.classList.remove("over");
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) accept(f);
    });
    // 拖到页面别处时不要让浏览器直接打开文件（会丢掉当前页）
    ["dragover", "drop"].forEach(function (ev) {
      window.addEventListener(ev, function (e) {
        if (e.target.closest && e.target.closest("#drop")) return;
        e.preventDefault();
      });
    });

    $("#commit").addEventListener("click", commit);

    $("#manual-form").addEventListener("submit", submitManual);
    $("#manual-reset").addEventListener("click", function () {
      $("#manual-form").reset();
      clearManualErrors();
      setMsg($("#manual-msg"), "");
    });

    function resetAll() {
      state.job = null;
      state.preview = null;
      $("#box-preview").hidden = true;
      $("#box-result").hidden = true;
      setMsg($("#upload-msg"), "");
      setStep(1);
      $("#file").value = "";
    }
    $("#reset").addEventListener("click", resetAll);
    $("#again").addEventListener("click", function () {
      resetAll();
      $("#box-upload").scrollIntoView({ behavior: "smooth", block: "center" });
    });

    $("#jump").addEventListener("click", function () {
      $("#library").scrollIntoView({ behavior: "smooth", block: "start" });
    });

    // 搜索走服务端，300ms 防抖；输入变化立刻重置到第一页
    $("#lib-q").addEventListener("input", function (e) {
      state.q = e.target.value || "";
      clearTimeout(state.timer);
      state.timer = setTimeout(function () { loadFoods(true); }, 300);
    });

    $("#lib-chips").addEventListener("click", function (e) {
      var btn = e.target.closest(".chip");
      if (!btn) return;
      var cat = btn.dataset.cat || "";
      if (cat === state.cat) return;
      state.cat = cat;
      renderChips();
      loadFoods(true);
    });

    $("#lib-more").addEventListener("click", function () { loadFoods(false); });
  }

  // ------------------------------------------------------------
  bind();
  setStep(1);
  loadMeta();
  loadFoods(true);
  loadStats();
})();
