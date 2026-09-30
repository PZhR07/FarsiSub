"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="farsisub-token"]').content;
const finished = new Set(["done", "failed", "cancelled"]);
let selected = null, rows = [], dirty = false, loaded = null, busy = false, modelsReady = false, polling = false;
let cueRevision = null, selectionEpoch = 0, editVersion = 0, saving = false;
let baseRows = [], uncertainSave = null, conflicts = new Map();
let page = 0, filteredRows = [], visibleBlocks = new Map(), activeCueId = null;
const PAGE_SIZE = 40;

async function api(url, options = {}, includeRevision = false) {
  const response = await fetch(url, {...options, headers: {"Content-Type": "application/json", "X-FarsiSub-Token": token, ...options.headers}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { const failure = new Error(data.error || `خطای ارتباط با برنامه (${response.status}). صفحه را تازه کنید.`); failure.status = response.status; throw failure; }
  return includeRevision ? {rows: data, revision: response.headers.get("ETag")} : data;
}
function error(message = "") { $("form-error").textContent = message; $("form-error").hidden = !message; }
function seconds(value) { const s = Math.floor(value); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; }
function markDirty(value) { dirty = value; $("save").disabled = !value || saving; }
function canSwitch() { return !dirty || window.confirm("تغییرات ذخیره نشده‌اند. بدون ذخیره ادامه می‌دهید؟"); }
const copyRows = data => data.map(row => ({...row, readability: [...(row.readability || [])]}));
const normalized = text => text.trim().replace(/\s+/g, " ");
function sameTexts(a, b) { return a.length === b.length && a.every((row, i) => row.id === b[i].id && normalized(row.persian) === normalized(b[i].persian)); }

function acceptSave(result, ticket) {
  baseRows = copyRows(result.rows); cueRevision = result.revision; uncertainSave = null;
  if (ticket.version === editVersion) {
    rows = copyRows(result.rows); markDirty(false); renderRows();
    $("save-message").textContent = "ذخیره شد. فایل SRT و زیرنویس پیش‌نمایش به‌روز هستند.";
  } else {
    markDirty(!sameTexts(rows, baseRows));
    $("save-message").textContent = "نسخهٔ قبلی ذخیره شد؛ تغییرات جدید شما هنوز نیاز به ذخیره دارند.";
  }
  $("subtitle-track").src = `/download/${ticket.id}/vtt?v=${Date.now()}`;
}

async function recoverSave(ticket) {
  const remote = await api(`/api/jobs/${ticket.id}/cues`, {}, true);
  if (selected !== ticket.id || selectionEpoch !== ticket.epoch) return;
  if (sameTexts(remote.rows, ticket.changes)) {
    acceptSave(remote, ticket); error(); return;
  }
  if (remote.rows.length !== rows.length || remote.rows.some((r, i) => r.id !== rows[i].id)) {
    throw new Error("ساختار زیرنویس تغییر کرده است؛ پیش از تازه‌کردن صفحه، تغییرات خود را کپی کنید.");
  }
  const baseline = ticket.base;
  conflicts = new Map();
  rows = rows.map((local, i) => {
    const server = remote.rows[i], before = baseline[i];
    const mine = normalized(local.persian), theirs = normalized(server.persian), old = normalized(before.persian);
    if (mine === old || mine === theirs) return {...server};
    if (theirs !== old) conflicts.set(local.id, server.persian);
    return {...server, persian: local.persian};
  });
  baseRows = copyRows(remote.rows); cueRevision = remote.revision; uncertainSave = null;
  editVersion++; markDirty(!sameTexts(rows, baseRows));
  if (conflicts.size) {
    const first = rows.findIndex(row => conflicts.has(row.id));
    $("search").value = ""; page = Math.floor(first / PAGE_SIZE);
    error("در بخش‌های مشخص‌شده دو ویرایش متفاوت وجود دارد؛ متن موردنظر را انتخاب کنید، سپس ذخیره کنید.");
  } else {
    error(); $("save-message").textContent = dirty ? "تغییرات بازیابی و ادغام شدند؛ دوباره ذخیره کنید." : "نسخهٔ ذخیره‌شده بازیابی شد.";
  }
  renderRows();
}

async function system() {
  const data = await api("/api/system");
  busy = Boolean(data.active);
  modelsReady = Object.values(data.models).every(Boolean);
  $("start").disabled = !modelsReady || busy;
  $("readiness").textContent = modelsReady ? "مدل‌ها آماده‌اند · پردازش کاملاً روی همین دستگاه انجام می‌شود." : "مدل‌ها هنوز آماده نیستند. یک بار setup.bat را با اتصال اینترنت اجرا کنید؛ پس از دانلود، استفاده آفلاین است.";
  $("readiness").classList.toggle("warning", !modelsReady);
  return data;
}
async function history() {
  const jobs = await api("/api/jobs");
  $("history").replaceChildren();
  if (!jobs.length) {
    const p = document.createElement("p"); p.className = "hint"; p.textContent = "هنوز فیلمی پردازش نشده است."; $("history").append(p);
  }
  const labels = {done: "آمادهٔ دریافت", failed: "نیاز به بررسی", cancelled: "متوقف شده", queued: "در انتظار", running: "در حال پردازش"};
  for (const job of jobs) {
    const button = document.createElement("button"); button.type = "button"; button.className = "history-item";
    const name = document.createElement("span"); name.textContent = job.name; name.dir = "auto";
    const status = document.createElement("small"); status.textContent = labels[job.status] || job.status;
    button.append(name, status); button.addEventListener("click", () => selectJob(job.id).catch(e => error(e.message)));
    $("history").append(button);
  }
}
async function selectJob(id) {
  if (id !== selected && !canSwitch()) return;
  if (id !== selected) { selectionEpoch++; editVersion = 0; cueRevision = null; rows = []; baseRows = []; conflicts = new Map(); uncertainSave = null; page = 0; markDirty(false); loaded = null; $("editor").hidden = true; $("video").pause(); }
  selected = id; localStorage.setItem("farsisub-job", id); error();
  await refresh();
}
async function refresh() {
  if (!selected) return;
  const id = selected;
  const epoch = selectionEpoch;
  const state = await api(`/api/jobs/${id}`);
  if (id !== selected || epoch !== selectionEpoch) return;
  $("job-panel").hidden = false;
  $("job-name").textContent = state.name;
  $("job-message").textContent = state.message || "در انتظار…";
  $("job-message").classList.toggle("error", state.status === "failed");
  $("percent").textContent = `${Math.round(state.progress || 0)}%`;
  $("progress").value = state.progress || 0;
  $("device-badge").textContent = state.device === "cuda" ? "NVIDIA GPU" : state.device === "cpu" ? "CPU" : "LOCAL";
  $("cancel").hidden = finished.has(state.status);
  $("retry").hidden = !["failed", "cancelled"].includes(state.status);
  $("job-warning").hidden = !state.warning;
  $("job-warning").textContent = state.warning || "";
  $("downloads").hidden = state.status !== "done";
  $("result-count").textContent = state.count ? `${state.count} بخش زیرنویس · مدت ${seconds(state.duration)}` : "";
  for (const li of document.querySelectorAll(".stages li")) li.classList.toggle("current", li.dataset.stage === state.stage);
  if (state.status === "done") {
    $("download-srt").href = `/download/${id}/srt`; $("download-english").href = `/download/${id}/english`; $("download-audio").href = `/download/${id}/audio`;
    if (loaded !== id) {
      const result = await api(`/api/jobs/${id}/cues`, {}, true);
      if (selected !== id || epoch !== selectionEpoch || loaded === id) return;
      rows = result.rows; baseRows = copyRows(rows); cueRevision = result.revision; loaded = id; $("editor").hidden = false; $("search").value = ""; page = 0; $("save-message").textContent = "";
      $("video").src = `/media/${id}`; $("subtitle-track").src = `/download/${id}/vtt`;
      renderRows(); markDirty(false);
    }
  }
}
function renderRows() {
  $("cue-list").replaceChildren();
  visibleBlocks = new Map(); activeCueId = null;
  const query = $("search").value.toLocaleLowerCase();
  filteredRows = query ? rows.filter(row => `${row.english} ${row.persian}`.toLocaleLowerCase().includes(query)) : rows;
  const pages = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));
  page = Math.max(0, Math.min(page, pages - 1));
  $("page-label").textContent = `صفحهٔ ${page + 1} از ${pages} · ${filteredRows.length} بخش`;
  $("page-prev").disabled = page === 0; $("page-next").disabled = page === pages - 1;
  for (const row of filteredRows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
    const block = document.createElement("div"); block.className = "cue"; block.dataset.id = row.id;
    const top = document.createElement("div"); top.className = "cue-top";
    const label = document.createElement("span"); label.textContent = `بخش ${row.id}`;
    const time = document.createElement("button"); time.type = "button"; time.className = "time-button"; time.dir = "ltr";
    time.textContent = `${seconds(row.start)} → ${seconds(row.end)}`;
    time.addEventListener("click", () => { $("video").currentTime = row.start; $("video").play().catch(() => {}); });
    top.append(label, time);
    const english = document.createElement("p"); english.className = "english"; english.textContent = row.english;
    const persian = document.createElement("textarea"); persian.value = row.persian; persian.setAttribute("aria-label", `ترجمهٔ بخش ${row.id}`);
    persian.addEventListener("input", () => { row.persian = persian.value; editVersion++; markDirty(true); $("save-message").textContent = "تغییرات هنوز ذخیره نشده‌اند."; });
    block.append(top, english, persian);
    for (const warning of row.readability || []) { const note = document.createElement("p"); note.className = "hint cue-warning"; note.textContent = warning; block.append(note); }
    if (conflicts.has(row.id)) {
      const note = document.createElement("p"); note.className = "conflict-text"; note.textContent = `متن نسخهٔ دیگر: ${conflicts.get(row.id)}`;
      const choices = document.createElement("div"); choices.className = "actions";
      for (const [label, useRemote] of [["نگه‌داشتن متن من", false], ["استفاده از متن دیگر", true]]) {
        const button = document.createElement("button"); button.type = "button"; button.className = "secondary"; button.textContent = label;
        button.addEventListener("click", () => {
          if (useRemote) row.persian = conflicts.get(row.id);
          conflicts.delete(row.id); editVersion++; markDirty(!sameTexts(rows, baseRows)); renderRows();
          if (!conflicts.size) { error(); $("save-message").textContent = dirty ? "اختلاف‌ها حل شدند؛ تغییرات را ذخیره کنید." : "نسخهٔ دیگر انتخاب شد."; }
        });
        choices.append(button);
      }
      block.append(note, choices);
    }
    visibleBlocks.set(row.id, block); $("cue-list").append(block);
  }
  updateActiveCue();
}
function updateActiveCue() {
  const now = $("video").currentTime;
  let low = 0, high = rows.length - 1, index = -1;
  while (low <= high) { const mid = (low + high) >> 1; if (rows[mid].start <= now) { index = mid; low = mid + 1; } else high = mid - 1; }
  const id = index >= 0 && now < rows[index].end ? rows[index].id : null;
  if (id === activeCueId) return;
  if (visibleBlocks.has(activeCueId)) visibleBlocks.get(activeCueId).classList.toggle("active", false);
  if (visibleBlocks.has(id)) visibleBlocks.get(id).classList.toggle("active", true);
  activeCueId = id;
}
$("page-prev").addEventListener("click", () => { page--; renderRows(); $("cue-list").scrollTop = 0; });
$("page-next").addEventListener("click", () => { page++; renderRows(); $("cue-list").scrollTop = 0; });
$("path").addEventListener("input", () => {
  const option = document.createElement("option"); option.value = "0"; option.textContent = "مسیر صوتی اول";
  $("audio-track").replaceChildren(option);
});
$("probe-audio").addEventListener("click", async () => {
  const path = $("path").value; $("probe-audio").disabled = true;
  try {
    const info = await api("/api/media-info", {method: "POST", body: JSON.stringify({path})});
    if ($("path").value !== path) return;
    $("audio-track").replaceChildren();
    for (const stream of info.audio) { const option = document.createElement("option"); option.value = String(stream.track); option.textContent = `${stream.track + 1} · ${stream.language} · ${stream.title || stream.codec} · ${stream.channels} کانال`; $("audio-track").append(option); }
    const english = info.audio.find(s => ["eng", "en"].includes(s.language));
    $("audio-track").value = String(english ? english.track : info.audio[0].track); error();
  } catch (e) { error(e.message); } finally { $("probe-audio").disabled = false; }
});
$("job-form").addEventListener("submit", async event => {
  event.preventDefault(); if (!canSwitch()) return;
  $("start").disabled = true; error();
  try { const result = await api("/api/jobs", {method: "POST", body: JSON.stringify({path: $("path").value, device: $("device").value, audio_track: Number($("audio-track").value), context: $("context-translation").checked})}); await selectJob(result.id); await history(); }
  catch (e) { error(e.message); }
  finally { await system().catch(e => error(e.message)); }
});
$("cancel").addEventListener("click", async () => {
  $("cancel").disabled = true;
  try { await api(`/api/jobs/${selected}/cancel`, {method: "POST", body: "{}"}); $("job-message").textContent = "در حال توقف؛ منتظر پایان بخش جاری باشید…"; }
  catch (e) { error(e.message); } finally { $("cancel").disabled = false; }
});
$("retry").addEventListener("click", async () => {
  try { await api(`/api/jobs/${selected}/retry`, {method: "POST", body: "{}"}); await refresh(); await system(); }
  catch (e) { error(e.message); }
});
$("save").addEventListener("click", async () => {
  if (saving || !dirty || !cueRevision || loaded !== selected) return;
  if (conflicts.size) { error("پیش از ذخیره، متن موردنظر را در بخش‌های دارای اختلاف انتخاب کنید."); return; }
  const ticket = uncertainSave || {id: selected, epoch: selectionEpoch, version: editVersion,
    changes: rows.map(row => ({id: row.id, persian: row.persian})), base: copyRows(baseRows)};
  saving = true; markDirty(dirty);
  try {
    if (uncertainSave) { await recoverSave(uncertainSave); return; }
    const result = await api(`/api/jobs/${ticket.id}/cues`, {method: "POST", headers: {"If-Match": cueRevision}, body: JSON.stringify(ticket.changes)}, true);
    if (selected !== ticket.id || selectionEpoch !== ticket.epoch) return;
    acceptSave(result, ticket); error();
  } catch (e) {
    if (selected === ticket.id && selectionEpoch === ticket.epoch) {
      markDirty(true);
      if (!e.status || e.status === 409) {
        uncertainSave = ticket;
        try { await recoverSave(ticket); }
        catch (recoveryError) { if (selected === ticket.id && selectionEpoch === ticket.epoch) error("ارتباط برقرار نشد؛ تغییرات شما حفظ شده‌اند. با ذخیرهٔ مجدد، وضعیت بررسی می‌شود."); }
      } else error(e.message);
    }
  } finally { saving = false; markDirty(dirty); }
});
$("search").addEventListener("input", () => { page = 0; renderRows(); });
$("video").addEventListener("timeupdate", updateActiveCue);
$("video").addEventListener("error", () => { $("video-hint").textContent = "مرورگر نمی‌تواند این فرمت ویدئو را پخش کند. زیرنویس را دانلود و همراه فیلم در VLC یا پخش‌کنندهٔ دلخواه باز کنید."; });
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });

(async () => {
  try { const state = await system(); await history(); const prior = state.active || localStorage.getItem("farsisub-job"); if (prior) await selectJob(prior); }
  catch (e) { error(e.message); }
  setInterval(async () => {
    if (polling) return; polling = true;
    try { await refresh(); await system(); await history(); }
    catch (e) { error(e.message); } finally { polling = false; }
  }, 2500);
})();
