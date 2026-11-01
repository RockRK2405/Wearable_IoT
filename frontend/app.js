"use strict";
const $ = (s) => document.querySelector(s);
const ECG_WIN_S = 6;

const S = {
  patients: new Map(),   // pid -> patient summary {patient_id,name,data_source,online,latest}
  latest: new Map(),     // pid -> latest reading
  ecg: new Map(),        // pid -> ring buffer
  alerts: [],            // recent alert episodes
  notifs: [],            // recent notifications
  selected: null,
  config: new Map(),     // pid -> profile (from /api/patients)
  dirty: false,          // user editing the form -> don't overwrite
  mode: "", simulated: true,
};

// ---------- helpers ----------
const fmt = (v, d = 0) => (v == null || Number.isNaN(v)) ? "—" : Number(v).toFixed(d);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function timeStr(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ---------- ECG ring buffer + playback ----------
function ecgBuf(pid, fs) {
  let b = S.ecg.get(pid);
  if (!b || b.fs !== fs) { b = { fs, data: new Float32Array(fs * 20), written: 0, play: 0, off: false }; S.ecg.set(pid, b); }
  return b;
}
function pushEcg(pid, fs, samples, off) {
  const b = ecgBuf(pid, fs), N = b.data.length; b.off = off;
  for (const v of samples) b.data[b.written++ % N] = v;
}
function drawEcg(cv, b, now) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.floor(w * dpr)) { cv.width = Math.floor(w * dpr); cv.height = Math.floor(h * dpr); }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  // grid
  g.strokeStyle = "#13203a"; g.lineWidth = 1;
  for (let x = 0; x < w; x += 28) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); }
  for (let y = 0; y < h; y += 28) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
  if (!b || b.written < 4) { g.fillStyle = "#8ea0bd"; g.font = "14px system-ui"; g.fillText("Waiting for ECG…", 14, 24); return; }
  if (b.off) { g.fillStyle = "#f59e0b"; g.font = "15px system-ui"; g.fillText("ECG leads off — attach electrodes", 14, 26); }

  const win = b.fs * ECG_WIN_S;
  const end = Math.floor(b.play);
  const start = Math.max(0, end - win);
  const n = end - start;
  if (n < 2) return;
  // auto-scale around visible samples
  let mn = Infinity, mx = -Infinity;
  for (let i = start; i < end; i++) { const v = b.data[i % b.data.length]; if (v < mn) mn = v; if (v > mx) mx = v; }
  if (!isFinite(mn)) { mn = -1; mx = 1; }
  const pad = (mx - mn) * 0.12 || 0.2; mn -= pad; mx += pad;
  const sy = (v) => h - ((v - mn) / (mx - mn)) * h;
  g.strokeStyle = b.off ? "#64748b" : "#34e1a8";
  g.lineWidth = 1.7; g.lineJoin = "round"; g.beginPath();
  for (let i = 0; i < n; i++) {
    const v = b.data[(start + i) % b.data.length];
    const x = (i / (win - 1)) * w, y = sy(v);
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  }
  g.stroke();
}
let lastFrame = performance.now();
function frame(now) {
  const dt = (now - lastFrame) / 1000; lastFrame = now;
  const b = S.selected ? S.ecg.get(S.selected) : null;
  if (b) {
    const cushion = Math.floor(0.35 * b.fs);
    const target = b.written - cushion;
    if (b.play > target) b.play = target;                    // never ahead of data
    if (target - b.play > b.fs * ECG_WIN_S * 1.5) b.play = target - b.fs * ECG_WIN_S; // catch up after stall
    b.play = Math.min(target, b.play + dt * b.fs);
    if (b.play < 0) b.play = 0;
  }
  drawEcg($("#ecgCanvas"), b, now);
  requestAnimationFrame(frame);
}

// ---------- rendering ----------
function pickDefaultPatient() {
  if (S.selected && S.patients.has(S.selected)) return;
  const reals = [...S.patients.values()].filter((p) => p.data_source === "real");
  const list = reals.length ? reals : [...S.patients.values()];
  S.selected = list.length ? list[0].patient_id : null;
}
function renderPatientSelect() {
  const sel = $("#patientSel");
  const opts = [...S.patients.values()];
  sel.innerHTML = opts.map((p) => `<option value="${esc(p.patient_id)}">${esc(p.name || p.patient_id)}${p.data_source === "simulated" ? " (sim)" : ""}</option>`).join("")
    || `<option value="">No device connected yet</option>`;
  if (S.selected) sel.value = S.selected;
}
function setVitalClass(el, sev) { el.classList.remove("normal", "warning", "critical"); if (sev) el.classList.add(sev); }
function sevOfCode(findings, prefix) {
  const f = (findings || []).find((x) => x.code.startsWith(prefix));
  return f ? f.severity : null;
}
function renderLive() {
  const p = S.selected ? S.patients.get(S.selected) : null;
  const l = p ? (S.latest.get(S.selected) || p.latest) : null;
  $("#patientName").textContent = p ? (p.name || p.patient_id) : "—";
  const tag = $("#sourceTag"); tag.className = "src " + (p ? p.data_source : "");
  tag.textContent = p ? (p.data_source === "real" ? "live sensor" : "simulated") : "";

  const banner = $("#banner");
  if (!l) { banner.className = "banner none"; $("#statusPill").textContent = "NO DATA"; $("#statusMsg").textContent = "Waiting for readings…"; }
  else {
    banner.className = "banner " + l.status;
    $("#statusPill").textContent = l.status.toUpperCase();
    const msgs = (l.findings || []).map((f) => f.message);
    $("#statusMsg").textContent = msgs.length ? msgs.join("  •  ") : "All monitored values are within the configured limits.";
  }
  const hrSev = l ? (sevOfCode(l.findings, "HR_") || (l.hr_bpm != null ? "normal" : null)) : null;
  const spSev = l ? (sevOfCode(l.findings, "SPO2") || (l.spo2_pct != null ? "normal" : null)) : null;
  const tempSev = l ? (sevOfCode(l.findings, "TEMP_") || (l.temperature_c != null ? "normal" : null)) : null;
  const vibSev = l ? (sevOfCode(l.findings, "TREMOR_") || (l.vibration_intensity != null ? "normal" : null)) : null;

  setVitalClass($("#hrCard"), hrSev); setVitalClass($("#spCard"), spSev);
  setVitalClass($("#tempCard"), tempSev); setVitalClass($("#vibCard"), vibSev);

  $("#hr").textContent = l ? fmt(l.hr_bpm) : "—";
  $("#hrSub").textContent = l && l.hr_source === "ecg" ? "from ECG R-peaks" : l && l.hr_source === "ppg" ? "from SpO₂ pulse" : "no valid heart rate";
  $("#spo2").textContent = l ? fmt(l.spo2_pct) : "—";
  $("#temp").textContent = l ? fmt(l.temperature_c, 1) : "—";
  $("#tempSub").textContent = l && l.temperature_c >= 38.5 ? "fever / hyperthermia" : l && l.temperature_c <= 35.5 ? "hypothermia" : "normal core temp";
  $("#vib").textContent = l ? fmt(l.vibration_intensity, 2) : "—";
  $("#vibSub").textContent = l && l.vibration_intensity >= 0.70 ? "intense tremor / clonic jerk" : l && l.vibration_intensity >= 0.40 ? "elevated muscle tremor" : "resting / minimal tremor";

  const isSeiz = !!(l && l.seizure_detected);
  const scCard = $("#seizureCard");
  const stEl = $("#seizureStatus");
  scCard.classList.remove("seizure-alert", "warning", "critical");
  if (isSeiz) {
    stEl.textContent = "SEIZURE ALERT";
    scCard.classList.add("seizure-alert");
    $("#seizureSub").textContent = `CLONIC SEIZURE (${((l.seizure_prob || 0) * 100).toFixed(0)}% ML prob)`;
  } else if (l && l.seizure_class === "exercise") {
    stEl.textContent = "EXERCISE";
    scCard.classList.add("warning");
    $("#seizureSub").textContent = "physical activity (non-seizure)";
  } else {
    stEl.textContent = "NORMAL";
    $("#seizureSub").textContent = `ML prob ${l && l.seizure_prob != null ? ((l.seizure_prob) * 100).toFixed(0) : 0}% · Random Forest`;
  }

  $("#leads").textContent = l ? (l.leads_off ? "leads off" : "connected") : "—";
  $("#quality").textContent = l && l.ecg_quality != null ? fmt(l.ecg_quality, 2) : "—";
  $("#beats").textContent = l && l.beats_in_window != null ? l.beats_in_window : "—";
  const b = S.selected ? S.ecg.get(S.selected) : null;
  $("#ecgInfo").textContent = b ? `${b.fs} Hz` : "";
}
function renderAlerts() {
  const el = $("#alertList");
  if (!S.alerts.length) { el.innerHTML = `<p class="muted small empty">No alerts yet. All clear.</p>`; $("#alertCount").textContent = ""; return; }
  const items = [...S.alerts].slice(-40).reverse();
  $("#alertCount").textContent = `${items.length} shown`;
  el.innerHTML = items.map((a) => `
    <div class="item ${a.category === "sensor" ? "sensor" : a.severity}">
      <div class="it-top">
        <span><span class="tag ${a.severity}">${esc(a.severity)}</span> <span class="tag ${a.status}">${esc(a.status)}</span></span>
        <span class="it-time">${timeStr(a.started_ms)}</span>
      </div>
      <div class="it-msg">${esc(a.message)}</div>
    </div>`).join("");
}
function renderNotifs() {
  const el = $("#notifList");
  if (!S.notifs.length) { el.innerHTML = `<p class="muted small empty">None sent yet.</p>`; return; }
  const items = [...S.notifs].slice(-30).reverse();
  el.innerHTML = items.map((n) => `
    <div class="item ${n.severity === "critical" ? "critical" : "warning"}">
      <div class="it-top">
        <span><span class="tag ${n.status}">${esc(n.status)}</span> ${n.simulated ? '<span class="tag sim">simulated</span>' : ""} ${n.channel ? `<span class="muted small">${esc(n.channel)}</span>` : ""}</span>
        <span class="it-time">${timeStr(n.ts_ms)}</span>
      </div>
      <div class="it-msg">→ ${esc(n.contact_name || "")} ${esc(n.to)} · ${esc(n.code)}</div>
      ${n.maps_url ? `<div class="it-msg"><a href="${esc(n.maps_url)}" target="_blank" rel="noopener">map ↗</a></div>` : ""}
    </div>`).join("");
}

// ---------- config form ----------
function fillForm(prof) {
  if (S.dirty) return;
  $("#pName").value = prof.name || "";
  const e = prof.emergency || {}, loc = prof.location || {};
  $("#cName").value = e.contact_name || "";
  $("#cPhone").value = e.contact_phone || "";
  $("#minSev").value = e.min_severity || "critical";
  $("#notifyEnabled").checked = e.notify_enabled !== false;
  $("#callEnabled").checked = !!e.call_enabled;
  $("#locLabel").value = loc.label || "";
  $("#lat").value = loc.lat != null ? loc.lat : "";
  $("#lng").value = loc.lng != null ? loc.lng : "";
  updateMapLink();
}
function updateMapLink() {
  const lat = parseFloat($("#lat").value), lng = parseFloat($("#lng").value), label = $("#locLabel").value.trim();
  const a = $("#mapLink");
  if (!Number.isNaN(lat) && !Number.isNaN(lng)) { a.href = `https://www.google.com/maps?q=${lat},${lng}`; a.style.display = ""; }
  else if (label) { a.href = "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(label); a.style.display = ""; }
  else a.style.display = "none";
}
async function loadConfig() {
  try {
    const list = await (await fetch("/api/patients")).json();
    S.config = new Map(list.map((p) => [p.patient_id, p]));
    if (S.selected && S.config.has(S.selected)) fillForm(S.config.get(S.selected));
  } catch (e) { /* ignore */ }
}
function msg(text, err) { const m = $("#saveMsg"); m.textContent = text; m.className = "savemsg" + (err ? " err" : ""); if (text) setTimeout(() => { m.textContent = ""; }, 4000); }

async function save() {
  if (!S.selected) { msg("No patient selected", true); return; }
  const body = {
    name: $("#pName").value.trim(),
    emergency: {
      contact_name: $("#cName").value.trim(), contact_phone: $("#cPhone").value.trim(),
      min_severity: $("#minSev").value, notify_enabled: $("#notifyEnabled").checked,
      call_enabled: $("#callEnabled").checked,
    },
    location: { label: $("#locLabel").value.trim(),
      lat: $("#lat").value === "" ? null : parseFloat($("#lat").value),
      lng: $("#lng").value === "" ? null : parseFloat($("#lng").value) },
  };
  const r = await fetch("/api/patients/" + encodeURIComponent(S.selected), {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (r.ok) { S.dirty = false; const p = await r.json(); S.config.set(p.patient_id, p); msg("Saved ✓"); }
  else { const e = await r.json().catch(() => ({})); msg(e.detail || "Save failed", true); }
}
async function sendTest() {
  if (!S.selected) { msg("No patient selected", true); return; }
  if (S.dirty) await save();
  const r = await fetch("/api/notifications/test", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ patient_id: S.selected }) });
  const d = await r.json().catch(() => ({}));
  if (r.ok && d.ok) msg(d.notification && d.notification.simulated ? "Test sent (simulated — see notifications)" : "Test alert sent ✓");
  else msg((d.detail || (d.notification && d.notification.detail) || "Test failed"), true);
}

// ---------- websocket ----------
function applySnapshot(m) {
  S.mode = m.mode || S.mode;
  if (m.patients) { S.patients = new Map(m.patients.map((p) => [p.patient_id, p])); m.patients.forEach((p) => p.latest && S.latest.set(p.patient_id, p.latest)); }
  if (m.alerts) S.alerts = m.alerts.slice();
  pickDefaultPatient(); renderPatientSelect(); renderLive(); renderAlerts();
}
function onMessage(m) {
  switch (m.type) {
    case "snapshot": applySnapshot(m); loadConfig(); break;
    case "ecg":
      if (m.patient_id === S.selected) pushEcg(m.patient_id, m.fs, m.samples, m.leads_off);
      break;
    case "reading": {
      S.latest.set(m.patient_id, m);
      if (!S.patients.has(m.patient_id)) { S.patients.set(m.patient_id, { patient_id: m.patient_id, name: m.patient_name, data_source: m.data_source, online: true, latest: m }); pickDefaultPatient(); renderPatientSelect(); loadConfig(); }
      else { const p = S.patients.get(m.patient_id); p.latest = m; p.data_source = m.data_source; }
      if (m.patient_id === S.selected) renderLive();
      break;
    }
    case "alert": { const a = m.alert; const i = S.alerts.findIndex((x) => x.id === a.id); if (i >= 0) S.alerts[i] = a; else S.alerts.push(a); renderAlerts(); break; }
    case "notification": { S.notifs.push(m.notification); renderNotifs(); break; }
    case "device_state": break;
  }
}
let ws;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { $("#connDot").className = "dot on"; $("#connText").textContent = "live"; };
  ws.onclose = () => { $("#connDot").className = "dot off"; $("#connText").textContent = "reconnecting…"; setTimeout(connect, 1500); };
  ws.onmessage = (ev) => { try { onMessage(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
}
async function loadStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    S.mode = s.mode; S.simulated = s.notifier && s.notifier.simulated;
    $("#modeBadge").textContent = `${s.mode} · alerts: ${s.notifier ? s.notifier.provider : "?"}`;
    $("#disclaimer").textContent = s.disclaimer + (S.simulated ? "  Emergency notifier is in SIMULATED mode: messages are logged, not actually sent (set Twilio credentials to send real SMS)." : "");
  } catch (e) { /* ignore */ }
}

// ---------- wire up ----------
function init() {
  loadStatus();
  connect();
  requestAnimationFrame(frame);
  $("#patientSel").addEventListener("change", (e) => { S.selected = e.target.value; S.dirty = false; S.ecg.delete(S.selected); renderLive(); if (S.config.has(S.selected)) fillForm(S.config.get(S.selected)); });
  ["#pName", "#cName", "#cPhone", "#minSev", "#notifyEnabled", "#callEnabled", "#locLabel", "#lat", "#lng"].forEach((id) => {
    const el = $(id); const ev = el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(ev, () => { S.dirty = true; if (id === "#lat" || id === "#lng" || id === "#locLabel") updateMapLink(); });
  });
  $("#saveBtn").addEventListener("click", save);
  $("#testBtn").addEventListener("click", sendTest);
  $("#geoBtn").addEventListener("click", () => {
    if (!navigator.geolocation) { msg("Geolocation not supported", true); return; }
    msg("Getting location…");
    navigator.geolocation.getCurrentPosition(
      (pos) => { $("#lat").value = pos.coords.latitude.toFixed(6); $("#lng").value = pos.coords.longitude.toFixed(6); S.dirty = true; updateMapLink(); msg("Location filled ✓"); },
      (err) => msg("Location error: " + err.message, true), { enableHighAccuracy: true, timeout: 10000 });
  });
  // periodically refresh notifications (covers reconnects)
  setInterval(async () => { try { const d = await (await fetch("/api/notifications?limit=30")).json(); S.notifs = d.items.slice().reverse(); renderNotifs(); } catch (e) {} }, 8000);
}
init();
