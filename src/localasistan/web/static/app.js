"use strict";
/* LocalAsistan arayüzü — bağımlılıksız. Tüm metin önce kaçışlanır; innerHTML yalnızca bizim ürettiğimiz güvenli işaretlemeyle kullanılır. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* ---------------------------------------------------------------- API yardımcıları */
class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {} };
  if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  else if (method !== "GET") { init.headers["Content-Type"] = "application/json"; init.body = "{}"; }
  let response;
  try { response = await fetch(path, init); }
  catch (_) { throw new ApiError("Sunucuya ulaşılamıyor. Uygulama çalışıyor mu?", 0); }
  let data = null;
  try { data = await response.json(); } catch (_) { /* gövde yok */ }
  if (!response.ok) throw new ApiError((data && data.error) || `HTTP ${response.status}`, response.status);
  return data;
}

/* ---------------------------------------------------------------- Güvenli Markdown (hafif) */
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function inlineMd(escaped) {
  const codes = [];
  let s = escaped.replace(/`([^`\n]+)`/g, (_, code) => { codes.push(code); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)<>"']+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${codes[Number(n)]}</code>`);
}

function renderMarkdown(source) {
  const lines = source.replace(/\u0000/g, "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  const blockStart = (l) => /^```/.test(l) || /^#{1,4}\s/.test(l) || /^\s*[-*]\s+/.test(l) || /^\s*\d+[.)]\s+/.test(l) || /^>\s?/.test(l);
  const splitRow = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }
    let m = /^(#{1,4})\s+(.*)$/.exec(line);
    if (m) { const level = m[1].length + 1; out.push(`<h${level}>${inlineMd(escapeHtml(m[2]))}</h${level}>`); i++; continue; }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const re = ordered ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*]\s+(.*)$/;
      const items = [];
      while (i < lines.length && re.test(lines[i])) items.push(inlineMd(escapeHtml(re.exec(lines[i++])[1])));
      out.push(`<${ordered ? "ol" : "ul"}>${items.map((t) => `<li>${t}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") rows.push(splitRow(lines[i++]));
      const th = head.map((c) => `<th>${inlineMd(escapeHtml(c))}</th>`).join("");
      const body = rows.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(escapeHtml(c))}</td>`).join("")}</tr>`).join("");
      out.push(`<table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${inlineMd(escapeHtml(buf.join("\n"))).replace(/\n/g, "<br>")}</blockquote>`);
      continue;
    }
    if (line.trim() === "") { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() !== "" && !blockStart(lines[i])) buf.push(lines[i++]);
    if (buf.length === 0) buf.push(lines[i++]);
    out.push(`<p>${inlineMd(escapeHtml(buf.join("\n"))).replace(/\n/g, "<br>")}</p>`);
  }
  return out.join("");
}

/* ---------------------------------------------------------------- Durum */
const state = {
  session: null,
  busy: false,
  abort: null,
  status: null,
  tools: [],
  selectedTool: null,
  stick: true,
  assistant: null, // { node, raw, scheduled }
  cards: new Map(), // call_id -> { node, state, result }
  approvals: new Map(), // approval_id -> node
};
try { state.session = sessionStorage.getItem("la.session"); } catch (_) { /* gizli mod */ }

function rememberSession(id) {
  state.session = id;
  try { if (id) sessionStorage.setItem("la.session", id); else sessionStorage.removeItem("la.session"); } catch (_) { /* yoksay */ }
}

const EXAMPLES = [
  "Bugünkü takvimimi özetle",
  "Çalışma alanındaki dosyaları listele",
  "Yarın 14:00'e 'Diş randevusu' ekle",
  "Önümüzdeki hafta 1 saatlik boş zamanlarımı bul",
  "'giderler' adında bir tablo oluştur ve 3 örnek kayıt ekle",
  "notlar/ideas.md dosyasına bugünün fikirlerini yaz",
];

/* ---------------------------------------------------------------- Sekmeler */
function showTab(name) {
  for (const btn of $$(".tabs button")) btn.setAttribute("aria-selected", String(btn.dataset.tab === name));
  for (const tab of $$(".tab")) {
    const active = tab.id === `tab-${name}`;
    tab.classList.toggle("active", active);
    tab.hidden = !active;
  }
  if (name === "tools") loadTools();
  if (name === "resources") loadResources();
  if (name === "status") refreshStatus();
}

/* ---------------------------------------------------------------- Üst durum çubuğu */
async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch (err) {
    state.status = null;
    setMcpIndicator("off", err.message);
    renderStatusTab(err.message);
    renderTicker();
    return;
  }
  const s = state.status;
  $("#version").textContent = `v${s.version}`;
  $("#model-chip").textContent = s.llm.model;
  $("#effort-chip").textContent = s.llm.thinking ? `düşünme: ${s.llm.effort}` : "düşünme kapalı";
  $("#policy-select").value = s.approval.policy;
  setMcpIndicator(s.mcp.connected ? "on" : "off", s.mcp.connected ? `Bağlı · ${s.mcp.tool_count ?? "?"} araç` : (s.mcp.last_error || "MCP sunucusu bağlı değil"));
  const banner = $("#banner");
  if (!s.llm.configured) {
    banner.textContent = "ANTHROPIC_API_KEY ayarlı değil: sohbet kapalı. Anahtarı .env dosyasına ekleyip yeniden başlatın. “Araçlar” sekmesi anahtarsız çalışır.";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
  renderStatusTab();
  renderTicker();
}

/* Üstteki fosforlu şerit: en önemli durumu tek cümlede söyler (hazır / uyarı / sorun). */
function renderTicker() {
  const s = state.status;
  const bar = $("#ticker");
  const text = $("#ticker-text");
  bar.className = "ticker";
  if (!s) { bar.classList.add("bad"); text.textContent = "Sunucuya ulaşılamıyor. Uygulama çalışıyor mu?"; return; }
  if (!s.mcp.connected) {
    bar.classList.add("bad");
    text.textContent = `MCP sunucusu bağlı değil${s.mcp.last_error ? `: ${s.mcp.last_error}` : ""}`;
    return;
  }
  if (s.approval.policy === "auto") { bar.classList.add("warn"); text.textContent = "Dikkat: onay kapalı. Yıkıcı araçlar sormadan çalışır."; return; }
  if (!s.llm.configured) { bar.classList.add("warn"); text.textContent = "Sohbet kapalı: ANTHROPIC_API_KEY eksik. Araçlar sekmesi çalışır."; return; }
  const policy = { ask_destructive: "yıkıcı işlemlerde onay", ask_writes: "her yazmada onay" }[s.approval.policy];
  text.textContent = `Hazır · ${s.mcp.tool_count ?? "?"} araç · ${s.environment.timezone} · ${policy}`;
}

function setMcpIndicator(kind, text) {
  const dot = $("#mcp-dot");
  dot.className = `dot dot-${kind}`;
  dot.title = text;
  $("#mcp-label").textContent = kind === "on" ? "MCP bağlı" : "MCP bağlı değil";
  $("#mcp-label").title = text;
}

function renderStatusTab(errorText) {
  const body = $("#status-body");
  body.replaceChildren();
  if (errorText) { body.append(el("div", { class: "banner error", text: errorText })); return; }
  const s = state.status;
  if (!s) return;
  const kv = (rows) => el("dl", { class: "kv" }, rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: String(v ?? "—") })]));
  const server = s.mcp.server || {};
  body.append(
    el("div", { class: "status-card" }, el("h3", { text: "MCP sunucusu" }), kv([
      ["Bağlantı", s.mcp.connected ? "bağlı" : "bağlı değil"],
      ["Sunucu", server.name ? `${server.title || server.name} ${server.version || ""}` : "—"],
      ["Protokol sürümü", server.protocol_version],
      ["Araç sayısı", s.mcp.tool_count],
      ["Yeniden bağlanma", s.mcp.restarts],
      ["Son hata", s.mcp.last_error || "yok"],
    ]), server.instructions ? el("details", {}, el("summary", { text: "Sunucu yönergeleri" }), el("pre", { class: "viewer", text: server.instructions })) : null),
    el("div", { class: "status-card" }, el("h3", { text: "LLM" }), kv([
      ["Sağlayıcı", s.llm.provider],
      ["Model", s.llm.model],
      ["API anahtarı", s.llm.configured ? "ayarlı" : "EKSİK"],
      ["Düşünme çabası (effort)", s.llm.effort],
      ["Uyarlanabilir düşünme", s.llm.thinking ? "açık" : "kapalı"],
      ["Ret durumunda yedek model", s.llm.refusal_fallback ? "açık" : "kapalı"],
      ["Tur başına en fazla araç turu", s.llm.max_iterations],
    ])),
    el("div", { class: "status-card" }, el("h3", { text: "Ortam" }), kv([
      ["Çalışma alanı (dosya araçları)", s.environment.workspace],
      ["Veri klasörü", s.environment.home],
      ["Saat dilimi", s.environment.timezone],
      ["Onay politikası", s.approval.policy],
    ])),
  );
}

/* ---------------------------------------------------------------- Sohbet çizimi */
const messagesBox = () => $("#messages");

/* Kullanıcı en alttaysa (ya da yeni mesaj gönderdiyse) liste yeni içerikle birlikte kayar; yukarı okuyorsa rahatsız etmez. */
function scrollDown(force = false) {
  const box = messagesBox();
  if (force || state.stick) box.scrollTop = box.scrollHeight;
}

function hideEmpty() { $("#empty").classList.add("hidden"); }
function showEmptyIfNeeded() { $("#empty").classList.toggle("hidden", messagesBox().querySelectorAll(".msg, .tool-card, .note").length > 0); }

function addUser(text) {
  hideEmpty();
  messagesBox().append(el("div", { class: "msg user", text }));
  scrollDown(true);
}

function startAssistant() {
  hideEmpty();
  const node = el("div", { class: "msg assistant" });
  messagesBox().append(node);
  state.assistant = { node, raw: "", scheduled: false };
}

function appendAssistantText(text) {
  if (!state.assistant) startAssistant();
  const a = state.assistant;
  a.raw += text;
  if (!a.scheduled) {
    a.scheduled = true;
    requestAnimationFrame(() => { a.scheduled = false; a.node.innerHTML = renderMarkdown(a.raw); scrollDown(); });
  }
}

function endAssistant() {
  const a = state.assistant;
  if (a) { a.node.innerHTML = renderMarkdown(a.raw); a.scheduled = false; scrollDown(); }
  state.assistant = null;
}

function addNote(text, level = "warn") {
  hideEmpty();
  messagesBox().append(el("div", { class: `note ${level === "warning" ? "warn" : level}`, text }));
  scrollDown();
}

const KIND_LABEL = { read: "okuma", write: "yazma", destructive: "yıkıcı", unknown: "bilinmiyor" };

function prettyJson(value) {
  try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
}

function addToolCard(ev) {
  endAssistant();
  hideEmpty();
  const stateEl = el("span", { class: "tool-state", text: "çalışıyor…" });
  const details = el("details", {}, el("summary", { text: "Ayrıntılar" }), el("pre", { class: "tool-input", text: prettyJson(ev.input) }));
  const node = el("div", { class: "tool-card running", "data-call": ev.call_id },
    el("div", { class: "tool-head" },
      el("span", { class: "tool-name", text: ev.name }),
      el("span", { class: `badge ${ev.kind}`, text: KIND_LABEL[ev.kind] || ev.kind }),
      stateEl),
    details);
  messagesBox().append(node);
  state.cards.set(ev.call_id, { node, stateEl, details });
  scrollDown();
}

function addApproval(ev) {
  const card = state.cards.get(ev.call_id);
  const host = card ? card.node : messagesBox();
  const status = el("span", { class: "muted", text: "Yanıtınız bekleniyor…" });
  const approve = el("button", { class: "primary small", type: "button", text: "Onayla" });
  const reject = el("button", { class: "ghost small", type: "button", text: "Reddet" });
  const node = el("div", { class: "approval", role: "group", "aria-label": `${ev.name} onayı`, "data-approval": ev.approval_id },
    el("div", { class: "approval-head" }, el("strong", { text: "Onay gerekiyor" }), el("span", { class: "approval-tool", text: ev.title || ev.name })),
    el("pre", { text: prettyJson(ev.input) }),
    el("div", { class: "approval-actions" }, approve, reject, status));
  const answer = async (value) => {
    approve.disabled = reject.disabled = true;
    try { await api(`/api/chat/${state.session}/approvals/${ev.approval_id}`, { method: "POST", body: { approve: value } }); }
    catch (err) { status.textContent = `Gönderilemedi: ${err.message}`; approve.disabled = reject.disabled = false; }
  };
  approve.addEventListener("click", () => answer(true));
  reject.addEventListener("click", () => answer(false));
  host.append(node);
  state.approvals.set(ev.approval_id, node);
  scrollDown();
  approve.focus({ preventScroll: true });
}

function resolveApproval(ev) {
  const node = state.approvals.get(ev.approval_id);
  if (!node) return;
  node.classList.add("done");
  node.replaceChildren(el("span", { class: ev.approved ? "ok-text" : "muted", text: ev.approved ? "✓ Onaylandı" : `✗ Reddedildi${ev.reason ? ` (${ev.reason})` : ""}` }));
  state.approvals.delete(ev.approval_id);
}

function finishToolCard(ev) {
  const card = state.cards.get(ev.call_id);
  if (!card) return;
  card.node.classList.remove("running");
  card.node.classList.add(ev.is_error ? "err" : "ok");
  card.stateEl.textContent = ev.is_error ? "hata" : `✓ ${ev.duration_ms} ms`;
  let text = ev.text;
  try { text = prettyJson(JSON.parse(ev.text)); } catch (_) { /* düz metin */ }
  card.details.append(el("div", { class: "muted", text: ev.is_error ? "Hata:" : "Sonuç:" }), el("pre", { text: text + (ev.truncated ? "\n…(kısaltıldı)" : "") }));
  if (ev.is_error) card.details.open = true;
  scrollDown();
}

function setTyping(text) {
  const typing = $("#typing");
  typing.textContent = text || "";
  typing.classList.toggle("hidden", !text);
}

function setBusy(busy) {
  state.busy = busy;
  $("#send").disabled = busy;
  $("#stop").classList.toggle("hidden", !busy);
  $("#input").disabled = false;
  if (!busy) setTyping("");
}

function handleEvent(ev, { replay = false } = {}) {
  switch (ev.type) {
    case "session": rememberSession(ev.session_id); break;
    case "user_message": addUser(ev.text); break;
    case "turn_start": setTyping("Düşünüyor…"); break;
    case "status":
      setTyping(ev.state === "thinking" ? "Düşünüyor…" : ev.state === "preparing_tool" ? `Araç hazırlanıyor: ${ev.name}` : "");
      break;
    case "text_delta": setTyping(""); appendAssistantText(ev.text); break;
    case "tool_call": addToolCard(ev); setTyping(`Çalışıyor: ${ev.name}`); break;
    case "approval_required":
      if (replay) { state.cards.get(ev.call_id)?.node.append(el("div", { class: "muted", text: "Onay isteği yanıtlanmadan sona erdi." })); }
      else addApproval(ev);
      break;
    case "approval_resolved": resolveApproval(ev); break;
    case "tool_result": finishToolCard(ev); break;
    case "usage": {
      const parts = [`↑ ${ev.input_tokens ?? 0}`, `↓ ${ev.output_tokens ?? 0}`];
      if (ev.cache_read_input_tokens) parts.push(`önbellek ${ev.cache_read_input_tokens}`);
      endAssistant();
      state.lastUsage = parts.join(" · ");
      break;
    }
    case "notice": endAssistant(); addNote(ev.message, ev.level === "info" ? "info" : "warn"); break;
    case "error": endAssistant(); addNote(ev.message, "error"); break;
    case "turn_end":
      endAssistant();
      if (state.lastUsage && !replay) { messagesBox().append(el("div", { class: "usage", text: state.lastUsage })); scrollDown(); }
      state.lastUsage = null;
      setTyping("");
      break;
    default: break;
  }
}

/* ---------------------------------------------------------------- SSE ile mesaj gönderme */
async function sendMessage(text) {
  if (state.busy || !text.trim()) return;
  addUser(text);
  setBusy(true);
  setTyping("Gönderiliyor…");
  state.abort = new AbortController();
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.session, message: text }),
      signal: state.abort.signal,
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { message = (await response.json()).error || message; } catch (_) { /* yoksay */ }
      if (response.status === 404 && state.session) {
        rememberSession(null);
        addNote("Sohbet oturumu sunucuda bulunamadı (yeniden başlatılmış olabilir). Mesajınızı tekrar gönderin.", "error");
      } else {
        addNote(message, "error");
      }
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = frame.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
        if (!data) continue;
        try { handleEvent(JSON.parse(data)); } catch (err) { console.error("Bozuk olay", err, data); }
      }
    }
  } catch (err) {
    if (err.name === "AbortError") addNote("Durduruldu.", "info");
    else addNote(`Bağlantı koptu: ${err.message}`, "error");
  } finally {
    endAssistant();
    state.abort = null;
    state.approvals.clear();
    setBusy(false);
    refreshStatus();
    $("#input").focus();
  }
}

async function loadTranscript() {
  if (!state.session) return;
  try {
    const data = await api(`/api/chat/${state.session}`);
    for (const ev of data.transcript) handleEvent(ev, { replay: true });
    endAssistant();
  } catch (_) {
    rememberSession(null); // sunucu yeniden başlamış olabilir
  }
  showEmptyIfNeeded();
  scrollDown(true);
}

async function newChat() {
  if (state.busy) return;
  closeMenu();
  const old = state.session;
  rememberSession(null);
  state.cards.clear();
  state.approvals.clear();
  state.assistant = null;
  messagesBox().querySelectorAll(".msg, .tool-card, .note, .usage").forEach((n) => n.remove());
  $("#empty").classList.remove("hidden");
  if (old) { try { await api(`/api/chat/${old}`, { method: "DELETE" }); } catch (_) { /* yoksay */ } }
  $("#input").focus();
}

/* ---------------------------------------------------------------- Araçlar sekmesi */
async function loadTools() {
  try {
    const data = await api("/api/tools");
    state.tools = data.tools;
  } catch (err) {
    $("#tool-list").replaceChildren(el("li", { class: "banner error", text: err.message }));
    return;
  }
  renderToolList();
  if (state.selectedTool) selectTool(state.selectedTool);
}

function renderToolList() {
  const filter = $("#tool-filter").value.trim().toLowerCase();
  const list = $("#tool-list");
  list.replaceChildren();
  for (const tool of state.tools) {
    if (filter && !`${tool.name} ${tool.title} ${tool.description}`.toLowerCase().includes(filter)) continue;
    const button = el("button", { type: "button", "aria-current": String(tool.name === state.selectedTool), onclick: () => selectTool(tool.name) },
      el("span", { class: `badge ${tool.kind}`, text: KIND_LABEL[tool.kind] || tool.kind }), el("span", { class: "tool-name", text: tool.name }));
    list.append(el("li", {}, button));
  }
}

function resolveType(schema) {
  if (!schema) return { type: "json" };
  if (schema.enum) return { type: "enum", options: schema.enum };
  if (Array.isArray(schema.anyOf)) {
    const real = schema.anyOf.filter((s) => s.type !== "null");
    if (real.length === 1) return resolveType(real[0]);
    return { type: "json" };
  }
  if (["string", "integer", "number", "boolean"].includes(schema.type)) return { type: schema.type };
  return { type: "json" };
}

const MULTILINE_FIELDS = /^(content|sql|text|body|description|notes?|message)$/i;

function selectTool(name) {
  state.selectedTool = name;
  renderToolList();
  const tool = state.tools.find((t) => t.name === name);
  const pane = $("#tool-detail");
  if (!tool) { pane.replaceChildren(el("p", { class: "muted", text: "Araç bulunamadı." })); return; }
  const props = (tool.input_schema && tool.input_schema.properties) || {};
  const required = new Set((tool.input_schema && tool.input_schema.required) || []);
  const inputs = [];
  const form = el("form", { class: "tool-form", autocomplete: "off" });
  for (const [prop, schema] of Object.entries(props)) {
    const kind = resolveType(schema);
    const id = `f-${name}-${prop}`;
    let input;
    if (kind.type === "boolean") input = el("input", { type: "checkbox", id, ...(schema.default === true ? { checked: true } : {}) });
    else if (kind.type === "enum") input = el("select", { id }, kind.options.map((o) => el("option", { value: String(o), text: String(o) })));
    else if (kind.type === "integer" || kind.type === "number") input = el("input", { type: "number", id, step: kind.type === "integer" ? "1" : "any", placeholder: schema.default !== undefined ? String(schema.default) : "" });
    else if (kind.type === "json") input = el("textarea", { id, rows: 3, placeholder: schema.default !== undefined ? prettyJson(schema.default) : "JSON" });
    else if (kind.type === "string" && MULTILINE_FIELDS.test(prop)) input = el("textarea", { id, rows: 4, spellcheck: "false", placeholder: schema.default ? String(schema.default) : "" });
    else input = el("input", { type: "text", id, placeholder: schema.default !== undefined && schema.default !== null ? String(schema.default) : "" });
    inputs.push({ prop, kind, input, schema, required: required.has(prop), multiline: input.tagName === "TEXTAREA" && kind.type === "string" });
    form.append(el("div", { class: "field" },
      el("label", { for: id }, prop, required.has(prop) ? el("span", { class: "req", text: " *" }) : null),
      input,
      schema.description ? el("small", { text: schema.description }) : null));
  }
  const output = el("div", { "aria-live": "polite" });
  const runButton = el("button", { type: "submit", class: "primary", text: "Çalıştır" });
  form.append(runButton);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const args = {};
    let valid = true;
    for (const f of inputs) {
      f.input.classList.remove("invalid");
      const raw = f.kind.type === "boolean" ? f.input.checked : f.multiline ? f.input.value : f.input.value.trim();
      if (f.kind.type === "boolean") { if (raw !== (f.schema.default === true) || f.required) args[f.prop] = raw; continue; }
      if (raw.trim() === "") { if (f.required) { f.input.classList.add("invalid"); valid = false; } continue; }
      if (f.kind.type === "integer" || f.kind.type === "number") args[f.prop] = Number(raw);
      else if (f.kind.type === "json") {
        try { args[f.prop] = JSON.parse(raw); } catch (_) { f.input.classList.add("invalid"); valid = false; }
      } else args[f.prop] = raw; // çok satırlı alanlar (dosya içeriği, SQL) kırpılmadan gönderilir
    }
    if (!valid) { output.replaceChildren(el("div", { class: "banner error", text: "Geçersiz veya eksik alanlar var." })); return; }
    if (tool.kind === "destructive" && !window.confirm(`“${tool.name}” yıkıcı bir araçtır. Gerçekten çalıştırılsın mı?\n\n${prettyJson(args)}`)) return;
    runButton.disabled = true;
    output.replaceChildren(el("p", { class: "muted", text: "Çalışıyor…" }));
    try {
      const outcome = await api(`/api/tools/${encodeURIComponent(name)}/call`, { method: "POST", body: { arguments: args } });
      const shown = outcome.data !== null && outcome.data !== undefined ? prettyJson(outcome.data) : outcome.text;
      output.replaceChildren(el("div", { class: "result" },
        el("header", {}, el("span", { class: `badge ${outcome.is_error ? "destructive" : "read"}`, text: outcome.is_error ? "hata" : "başarılı" }), el("span", { class: "muted", text: `${outcome.duration_ms} ms` })),
        el("pre", { class: "result-body", text: shown })));
    } catch (err) {
      output.replaceChildren(el("div", { class: "banner error", text: err.message }));
    } finally { runButton.disabled = false; }
  });
  pane.replaceChildren(
    el("h3", { text: tool.title || tool.name, lang: "en" }),
    el("p", {}, el("span", { class: `badge ${tool.kind}`, text: KIND_LABEL[tool.kind] || tool.kind }), tool.needs_approval ? el("span", { class: "muted", text: "  · sohbette onay ister" }) : null),
    el("p", { class: "muted", text: tool.description }),
    form, output);
}

/* ---------------------------------------------------------------- Kaynaklar ve promptlar */
async function loadResources() {
  try {
    const res = await api("/api/resources");
    const list = $("#resource-list");
    list.replaceChildren(...res.resources.map((r) => el("li", {}, el("button", { type: "button", onclick: () => readResource(r.uri) },
      el("span", { class: "tool-name", text: r.uri }), el("span", { class: "muted", text: r.title || r.name })))));
    const templates = $("#template-list");
    templates.replaceChildren(...res.templates.map((t) => templateCard(t)));
  } catch (err) { $("#resource-list").replaceChildren(el("li", { class: "banner error", text: err.message })); }
  try {
    const data = await api("/api/prompts");
    $("#prompt-list").replaceChildren(...data.prompts.map((p) => promptCard(p)));
  } catch (err) { $("#prompt-list").replaceChildren(el("div", { class: "banner error", text: err.message })); }
}

async function readResource(uri) {
  const view = $("#resource-view");
  view.textContent = "Okunuyor…";
  try {
    const data = await api(`/api/resources/read?uri=${encodeURIComponent(uri)}`);
    view.textContent = data.contents.map((c) => c.text ?? `[ikili içerik: ${c.blob_base64_chars} karakter]`).join("\n");
  } catch (err) { view.textContent = `Hata: ${err.message}`; }
}

function templateCard(t) {
  const names = Array.from(t.uri_template.matchAll(/\{[+#./;?&]?([^}*]+)\*?\}/g)).map((m) => m[1]);
  const inputs = names.map((n) => el("input", { type: "text", "aria-label": n, placeholder: n }));
  const open = el("button", { type: "button", class: "ghost small", text: "Oku", onclick: () => {
    let i = 0;
    const uri = t.uri_template.replace(/\{[+#./;?&]?[^}]+\}/g, () => encodeURI(inputs[i++].value.trim()));
    readResource(uri);
  } });
  return el("div", { class: "card" }, el("h4", { text: t.title || t.name }), el("div", { class: "tool-name", text: t.uri_template }), el("div", { class: "muted", text: t.description || "" }), el("div", { class: "approval-actions" }, ...inputs, open));
}

function promptCard(p) {
  const inputs = p.arguments.map((a) => ({ a, input: el("input", { type: "text", "aria-label": a.name, placeholder: `${a.name}${a.required ? " *" : ""}` }) }));
  const view = $("#prompt-view");
  const gather = () => Object.fromEntries(inputs.filter((x) => x.input.value.trim() !== "").map((x) => [x.a.name, x.input.value.trim()]));
  const preview = el("button", { type: "button", class: "ghost small", text: "Önizle", onclick: async () => {
    try {
      const data = await api(`/api/prompts/${encodeURIComponent(p.name)}`, { method: "POST", body: { arguments: gather() } });
      view.textContent = data.messages.map((m) => `[${m.role}]\n${m.text}`).join("\n\n");
      view.dataset.text = data.messages.map((m) => m.text).join("\n\n");
    } catch (err) { view.textContent = `Hata: ${err.message}`; delete view.dataset.text; }
  } });
  const use = el("button", { type: "button", class: "primary small", text: "Sohbette kullan", onclick: async () => {
    try {
      const data = await api(`/api/prompts/${encodeURIComponent(p.name)}`, { method: "POST", body: { arguments: gather() } });
      $("#input").value = data.messages.map((m) => m.text).join("\n\n");
      showTab("chat");
      $("#input").focus();
    } catch (err) { view.textContent = `Hata: ${err.message}`; }
  } });
  return el("div", { class: "card" }, el("h4", { text: p.title || p.name }), el("div", { class: "muted", text: p.description || "" }),
    el("div", { class: "approval-actions" }, ...inputs.map((x) => x.input), preview, use));
}

/* ---------------------------------------------------------------- Komut çubuğu: metin alanı ve menüler */
function autosize() {
  const area = $("#input");
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight, 220)}px`;
}

function insertAtCursor(text) {
  const area = $("#input");
  const start = area.selectionStart ?? area.value.length;
  const end = area.selectionEnd ?? start;
  const before = area.value.slice(0, start);
  const separator = before && !/\s$/.test(before) ? " " : "";
  area.value = before + separator + text + area.value.slice(end);
  const caret = (before + separator + text).length;
  area.setSelectionRange(caret, caret);
  area.focus();
  autosize();
}

let openMenu = null;
function closeMenu() {
  openMenu = null;
  const box = $("#popover");
  box.classList.add("hidden");
  box.replaceChildren();
  for (const id of ["#btn-plus", "#btn-at"]) $(id).setAttribute("aria-expanded", "false");
}

function menuButton(label, hint, onclick, { disabled = false } = {}) {
  return el("button", { type: "button", role: "menuitem", disabled, onclick }, el("span", { text: label }), hint ? el("small", { text: hint }) : null);
}

async function toggleMenu(kind) {
  if (openMenu === kind) { closeMenu(); return; }
  closeMenu();
  openMenu = kind;
  const box = $("#popover");
  box.classList.remove("hidden");
  $(kind === "plus" ? "#btn-plus" : "#btn-at").setAttribute("aria-expanded", "true");
  if (kind === "plus") await fillPlusMenu(box);
  else await fillFileMenu(box);
}

async function fillPlusMenu(box) {
  const pick = (text) => () => { closeMenu(); $("#input").value = text; autosize(); $("#input").focus(); };
  box.replaceChildren(el("h4", { text: "Örnek görevler" }), ...EXAMPLES.map((text) => menuButton(text, "", pick(text))));
  try {
    const data = await api("/api/prompts");
    if (openMenu !== "plus") return;
    box.append(el("h4", { text: "MCP promptları" }));
    for (const prompt of data.prompts) {
      const needs = prompt.arguments.filter((a) => a.required).map((a) => a.name);
      if (needs.length) {
        box.append(menuButton(prompt.title || prompt.name, `gerekli: ${needs.join(", ")}`, () => { closeMenu(); showTab("resources"); }));
      } else {
        box.append(menuButton(prompt.title || prompt.name, "ekle", async () => {
          closeMenu();
          try {
            const rendered = await api(`/api/prompts/${encodeURIComponent(prompt.name)}`, { method: "POST", body: { arguments: {} } });
            $("#input").value = rendered.messages.map((m) => m.text).join("\n\n");
            autosize();
            $("#input").focus();
          } catch (err) { addNote(err.message, "error"); }
        }));
      }
    }
  } catch (_) { /* promptlar alınamadı: örnek görevler yeterli */ }
}

async function fillFileMenu(box) {
  const filter = el("input", { type: "search", placeholder: "Dosya ara…", "aria-label": "Dosya ara" });
  const list = el("div", {}, el("div", { class: "empty-note", text: "Dosyalar okunuyor…" }));
  box.replaceChildren(el("h4", { text: "Çalışma alanından dosya" }), filter, list);
  let files = [];
  try {
    const outcome = await api("/api/tools/fs_list/call", { method: "POST", body: { arguments: { path: ".", recursive: true, max_entries: 300 } } });
    files = ((outcome.data && outcome.data.entries) || []).filter((e) => e.type === "file");
  } catch (err) { list.replaceChildren(el("div", { class: "empty-note", text: err.message })); return; }
  const draw = () => {
    const needle = filter.value.trim().toLowerCase();
    const shown = files.filter((f) => f.path.toLowerCase().includes(needle)).slice(0, 60);
    list.replaceChildren(...(shown.length
      ? shown.map((f) => menuButton(f.path, `${f.size_bytes ?? 0} B`, () => { closeMenu(); insertAtCursor(`@${f.path}`); }))
      : [el("div", { class: "empty-note", text: files.length ? "Eşleşen dosya yok." : "Çalışma alanı boş. “localasistan seed” ile örnek veri yükleyebilirsiniz." })]));
  };
  filter.addEventListener("input", draw);
  draw();
  filter.focus();
}

/* ---------------------------------------------------------------- Başlatma */
function init() {
  for (const btn of $$(".tabs button")) btn.addEventListener("click", () => showTab(btn.dataset.tab));
  $("#tool-filter").addEventListener("input", renderToolList);
  $("#examples").replaceChildren(...EXAMPLES.map((text) => el("button", { type: "button", class: "example", text, onclick: () => { $("#input").value = text; $("#input").focus(); } })));

  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#input");
    const text = input.value;
    if (!text.trim()) return;
    input.value = "";
    autosize();
    closeMenu();
    sendMessage(text);
  });
  $("#input").addEventListener("input", autosize);
  $("#btn-plus").addEventListener("click", () => toggleMenu("plus"));
  $("#btn-at").addEventListener("click", () => toggleMenu("at"));
  $("#ticker-btn").addEventListener("click", () => showTab("status"));
  $("#tip-examples").addEventListener("click", (event) => { event.stopPropagation(); toggleMenu("plus"); });
  $("#tip-close").addEventListener("click", () => {
    $("#tip").classList.add("hidden");
    try { localStorage.setItem("la.tip", "closed"); } catch (_) { /* gizli mod */ }
  });
  try { if (localStorage.getItem("la.tip") === "closed") $("#tip").classList.add("hidden"); } catch (_) { /* gizli mod */ }
  document.addEventListener("click", (event) => { if (openMenu && !event.target.closest("#popover, #btn-plus, #btn-at")) closeMenu(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && openMenu) { closeMenu(); $("#input").focus(); } });
  $("#input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#stop").addEventListener("click", () => { if (state.abort) state.abort.abort(); });
  messagesBox().addEventListener("scroll", () => {
    const box = messagesBox();
    state.stick = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  });
  $("#new-chat").addEventListener("click", newChat);
  $("#policy-select").addEventListener("change", async (event) => {
    try { await api("/api/settings", { method: "POST", body: { approval: event.target.value } }); }
    catch (err) { addNote(err.message, "error"); }
    refreshStatus();
  });
  $("#reconnect").addEventListener("click", async () => {
    $("#reconnect").disabled = true;
    try { await api("/api/reconnect", { method: "POST" }); } catch (err) { renderStatusTab(err.message); }
    $("#reconnect").disabled = false;
    refreshStatus();
  });
  $("#refresh-status").addEventListener("click", refreshStatus);

  refreshStatus();
  loadTranscript();
}

document.addEventListener("DOMContentLoaded", init);
