"use strict";
/* Shipping Desk frontend. Plain JavaScript, no build step, talks to the backend only through /api (see config.js). */

const qs = new URLSearchParams(location.search);
if (qs.get("api")) { try { localStorage.setItem("sdv_api", qs.get("api")); } catch (_) {} }
let stored = ""; try { stored = localStorage.getItem("sdv_api") || ""; } catch (_) {}
const API = (qs.get("api") || stored || window.SDV_API || "").replace(/\/$/, "");

const LABEL = {shipper:"Shipper",consignee:"Consignee",notify_party:"Notify party",port_of_loading:"Port of loading",port_of_discharge:"Port of discharge",container_count:"Container count",gross_weight_kg:"Gross weight (kg)"};
const REASON = {unreadable:"Unreadable document",wrong_doc_type:"Wrong document type",missing_attachment:"Missing attachment",missing_value:"Missing value"};
const STATE = {empty:"empty",ready:"ready to process",preparing:"unpacking…",processing:"processing…",done:"done",error:"problem"};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, opt = {}) {
  let r;
  try { r = await fetch(API + path, opt); }
  catch (e) { throw new Error("Cannot reach the backend" + (API ? " at " + API : "") + ". Is it running?"); }
  const text = await r.text();
  let body = null; try { body = text ? JSON.parse(text) : null; } catch (_) {}
  if (!r.ok) throw new Error((body && body.error) || ("Request failed (" + r.status + ")"));
  return body;
}
const post = (path, obj) => api(path, {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(obj || {})});

function toast(msg) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t);
  setTimeout(() => t.remove(), 6000);
}

let config = {};
let view = null;      // the current page's teardown (stops polling)
let running = 0;      // incremented on every navigation; stale async work checks it

/* ---------------------------------------------------------------- routing */
function route() {
  running++;
  if (view) { view(); view = null; }
  const m = location.hash.match(/^#\/batch\/([^/]+)(?:\/(.+))?$/);
  if (m) return batchPage(m[1], m[2] ? decodeURIComponent(m[2]) : null, running);
  return homePage(running);
}
window.addEventListener("hashchange", route);

/* ---------------------------------------------------------------- upload helpers */
const HIDDEN = (p) => p.split("/").some((s) => s.startsWith(".")) || p.includes("__MACOSX/");

async function readEntry(entry, base) {
  if (entry.isFile) {
    const file = await new Promise((ok, no) => entry.file(ok, no));
    return [{file, path: base + entry.name}];
  }
  const reader = entry.createReader(), kids = [];
  for (;;) {
    const part = await new Promise((ok, no) => reader.readEntries(ok, no));
    if (!part.length) break;
    kids.push(...part);
  }
  return (await Promise.all(kids.map((k) => readEntry(k, base + entry.name + "/")))).flat();
}
async function filesFromDrop(dt) {
  const items = [...dt.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (items.length) return (await Promise.all(items.map((e) => readEntry(e, "")))).flat();
  return [...dt.files].map((f) => ({file: f, path: f.name}));
}
function fromInput(list) {
  return [...list].map((f) => ({file: f, path: f.webkitRelativePath || f.name}));
}

async function uploadFiles(batchId, items, onProgress) {
  items = items.filter((i) => !HIDDEN(i.path));
  if (!items.length) throw new Error("No files to upload.");
  const chunks = []; let cur = [], size = 0;
  for (const it of items) {
    if (cur.length && (cur.length >= 40 || size + it.file.size > 8_000_000)) { chunks.push(cur); cur = []; size = 0; }
    cur.push(it); size += it.file.size;
  }
  if (cur.length) chunks.push(cur);
  let done = 0, next = 0;
  const total = items.length;
  async function worker() {
    while (next < chunks.length) {
      const chunk = chunks[next++];
      const fd = new FormData();
      fd.append("paths", JSON.stringify(chunk.map((c) => c.path)));
      chunk.forEach((c) => fd.append("file", c.file, "f.bin"));
      await api(`/api/batches/${batchId}/files`, {method: "POST", body: fd});
      done += chunk.length; onProgress(done, total);
    }
  }
  await Promise.all([worker(), worker(), worker()]);
}

/* ---------------------------------------------------------------- home */
async function homePage(token) {
  $("app").innerHTML = `
    <div class="hero"><h2>Check a shipping inbox</h2>
      <p>Give it the inbox and it classifies every email, compares each Shipping Instruction with its draft Bill of Lading on seven fields,
      and hands anything it cannot verify to a person. Nothing is guessed.</p></div>
    <div class="cards">
      <div class="card" id="c-folder"><h3>1. A folder</h3>
        <div class="muted">The folder that holds <code>inbox/</code> (email_*.json) and <code>attachments/</code>. Drop it here or choose it.</div>
        <label class="drop" id="drop" style="min-width:0"><input type="file" id="folder" webkitdirectory multiple><span>Drop a folder or click to choose</span></label></div>
      <div class="card"><h3>2. A .zip of it</h3>
        <div class="muted">The same folder, zipped.</div>
        <label class="drop" style="min-width:0"><input type="file" id="zip" accept=".zip"><span>Choose a .zip</span></label></div>
      <div class="card"><h3>3. A link</h3>
        <div class="muted">A direct link to a .zip, or the address of an inbox server (it must answer <code>GET /emails</code>).</div>
        <input type="url" id="link" placeholder="https://…"><button class="pri" id="go-link">Load link</button></div>
      <div class="card" id="c-sample" hidden><h3>Try the sample</h3>
        <div class="muted">520 synthetic emails bundled with the system.</div><button id="go-sample">Open the sample inbox</button></div>
    </div>
    <div class="panel job" id="up" hidden><b id="uptext"></b><div class="progress"><i id="upbar"></i></div></div>
    <h2 class="section">Your batches</h2>
    <div class="panel"><table><thead><tr><th>Name</th><th>Source</th><th>State</th><th>Emails</th><th>Mismatch</th><th>Review</th><th></th></tr></thead><tbody id="batches"></tbody></table><div class="empty" id="nobatches" hidden>Nothing yet.</div></div>
    <h2 class="section">Check two documents</h2>
    <section class="panel" id="upload" style="padding:14px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap">
      <div style="flex:1;min-width:240px"><div class="muted">Add a Shipping Instruction and a draft Bill of Lading (PDF, Word, Excel or text), in any order. The system works out which is which and compares them.</div></div>
      <label class="drop" id="pairdrop"><input type="file" id="pair" multiple accept=".pdf,.docx,.xlsx,.txt"><span id="picked">Drop two files here or click to choose</span></label>
      <button class="pri" id="check">Check</button>
      <div class="err" id="pairerr" style="flex-basis:100%" hidden></div>
    </section>`;
  view = () => {};
  try {
    config = await api("/api/config");
    $("mode").textContent = "Live"; $("apiinfo").textContent = API ? API.replace(/^https?:\/\//, "") : "";
  } catch (e) { $("mode").textContent = "Offline"; $("app").insertAdjacentHTML("afterbegin", `<div class="note v-mismatch">${esc(e.message)}</div>`); return; }
  if (token !== running) return;
  $("c-sample").hidden = !config.sample;

  const busy = (on, text, pct) => { $("up").hidden = !on; if (on) { $("uptext").textContent = text; $("upbar").style.width = (pct || 0) + "%"; } };
  async function intake(name, items) {
    try {
      busy(true, "Preparing…", 0);
      const b = await post("/api/batches", {source: {type: "upload"}, name});
      await uploadFiles(b.id, items, (d, t) => busy(true, `Uploading ${d} / ${t} files`, (100 * d) / t));
      await post(`/api/batches/${b.id}/run`, {});
      location.hash = "#/batch/" + b.id;
    } catch (e) { busy(false); toast(e.message); }
  }
  $("folder").onchange = (e) => { const items = fromInput(e.target.files); if (items.length) intake((items[0].path.split("/")[0] || "Folder"), items); };
  $("zip").onchange = (e) => { const f = e.target.files[0]; if (f) intake(f.name, [{file: f, path: f.name}]); };
  const drop = $("c-folder");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", async (e) => {
    const items = await filesFromDrop(e.dataTransfer);
    if (!items.length) return;
    if (items.length === 1 && /\.zip$/i.test(items[0].path)) return intake(items[0].path, items);
    intake(items[0].path.split("/")[0], items);
  });
  $("go-link").onclick = async () => {
    const url = $("link").value.trim(); if (!url) return toast("Paste a link first.");
    try {
      $("go-link").disabled = true;
      const b = await post("/api/batches", {source: {type: "url", url}});
      await post(`/api/batches/${b.id}/run`, {});
      location.hash = "#/batch/" + b.id;
    } catch (e) { toast(e.message); }
    $("go-link").disabled = false;
  };
  $("go-sample").onclick = async () => {
    try { const b = await post("/api/batches", {source: {type: "sample"}}); location.hash = "#/batch/" + b.id; } catch (e) { toast(e.message); }
  };

  // two-document check
  let pair = [];
  const showPair = () => { $("picked").textContent = pair.length ? pair.map((f) => f.name).join("  ·  ") : "Drop two files here or click to choose"; $("pairerr").hidden = true; };
  $("pair").onchange = (e) => { pair = [...e.target.files]; showPair(); };
  const pd = $("pairdrop");
  ["dragenter", "dragover"].forEach((ev) => pd.addEventListener(ev, (e) => { e.preventDefault(); pd.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => pd.addEventListener(ev, (e) => { e.preventDefault(); pd.classList.remove("over"); }));
  pd.addEventListener("drop", (e) => { pair = [...e.dataTransfer.files]; showPair(); });
  const b64 = (file) => new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1] || ""); r.onerror = no; r.readAsDataURL(file); });
  $("check").onclick = async () => {
    const err = (m) => { $("pairerr").textContent = m; $("pairerr").hidden = false; };
    if (pair.length < 2) return err("Choose both documents first (the Shipping Instruction and the draft Bill of Lading).");
    if (pair.length > 4) return err("Choose at most 4 files.");
    $("check").disabled = true; $("check").textContent = "Checking…";
    try {
      const files = await Promise.all(pair.map(async (f) => ({name: f.name, data: await b64(f)})));
      const res = await post("/api/batches/uploads/check", {files});
      location.hash = "#/batch/uploads/" + encodeURIComponent(res.email_id);
    } catch (e) { err("Could not check: " + e.message); }
    $("check").disabled = false; $("check").textContent = "Check";
  };

  // batches list
  async function loadBatches() {
    const list = await api("/api/batches");
    if (token !== running) return;
    $("nobatches").hidden = list.length > 0;
    $("batches").innerHTML = list.map((b) => {
      const s = b.stats || {}, st = s.by_status || {};
      return `<tr data-id="${esc(b.id)}" style="cursor:pointer"><td><b>${esc(b.id === "uploads" ? "Two-document checks" : b.name)}</b></td>
        <td class="muted">${esc(b.source.type === "url" ? "link" : b.source.type)}</td><td><span class="state">${s.emails ? "done" : "empty"}</span></td>
        <td>${s.emails || 0}</td><td>${st.MISMATCH || 0}</td><td>${st.NEEDS_REVIEW || 0}</td>
        <td class="right">${b.id === "sample" || b.id === "uploads" ? "" : `<button data-del="${esc(b.id)}" class="small">Delete</button>`}</td></tr>`;
    }).join("");
    document.querySelectorAll("#batches tr").forEach((tr) => tr.onclick = () => { location.hash = "#/batch/" + tr.dataset.id; });
    document.querySelectorAll("[data-del]").forEach((b) => b.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this batch and its results?")) return;
      try { await api("/api/batches/" + b.dataset.del, {method: "DELETE"}); loadBatches(); } catch (er) { toast(er.message); }
    });
  }
  loadBatches().catch((e) => toast(e.message));
}

/* ---------------------------------------------------------------- batch */
async function batchPage(id, openCase, token) {
  const f = {view: "attention", q: ""};
  let cases = [], sel = openCase, pollTimer = null, lastLoad = 0, alive = true, info = null;
  view = () => { alive = false; clearTimeout(pollTimer); };
  $("app").innerHTML = `
    <div style="margin-bottom:10px"><a class="crumb" href="#/">← All batches</a></div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
      <h2 style="margin:0;font-size:18px" id="title">…</h2><span class="state" id="state"></span><span class="sp"></span>
      <button class="pri" id="run">Process again</button><button id="retry">Retry failed</button>
      <a class="btn" id="dl-sub" href="#">Download submission.json</a><a class="btn" id="dl-res" href="#">Download results</a>
    </div>
    <div class="panel job" id="job" hidden><b id="jobtext"></b><div class="progress"><i id="jobbar"></i></div></div>
    <div class="stats" id="stats"></div><div class="bar" id="bar"></div>
    <div class="grid">
      <div class="panel"><div class="scroll"><table><thead><tr><th>Email</th><th>Subject</th><th>Category</th><th>Result</th></tr></thead><tbody id="rows"></tbody></table></div><div class="empty" id="none" hidden>No cases in this view.</div></div>
      <div class="panel"><div class="detail" id="detail"><div class="empty">Select a case to see the evidence.</div></div></div>
    </div>`;
  $("dl-sub").href = `${API}/api/batches/${id}/submission`; $("dl-res").href = `${API}/api/batches/${id}/results`;
  $("mode").textContent = "Live";

  const tag = (c) => c.error ? `<span class="tag MISMATCH">FAILED</span>` : c.category !== "BL_COMPARISON" ? `<span class="tag other">–</span>` :
    `<span class="tag ${c.status}">${c.status.replace("_", " ")}</span>` + (c.review ? ` <span class="muted">✓ decided</span>` : "");

  function stats() {
    const cmp = cases.filter((c) => c.category === "BL_COMPARISON"), n = (s) => cmp.filter((c) => c.status === s).length;
    const wait = cmp.filter((c) => c.status !== "OK" && !c.review).length, failed = cases.filter((c) => c.error).length;
    const box = (k, v, cl = "") => `<div class="stat ${cl}"><b>${v}</b><span>${k}</span></div>`;
    $("stats").innerHTML = box("Emails", cases.length) + box("Document checks", cmp.length) + box("Clean", n("OK"), "ok") + box("Mismatches", n("MISMATCH"), "bad") +
      box("Needs review", n("NEEDS_REVIEW"), "rev") + box("Awaiting a person", wait, "rev") + (failed ? box("Failed to process", failed, "bad") : "");
  }
  function bar() {
    const views = [["attention","Needs attention"],["all","All emails"],["MISMATCH","Mismatches"],["NEEDS_REVIEW","Needs review"],["OK","Clean"],["decided","Decided"]];
    $("bar").innerHTML = views.map(([k, l]) => `<button class="chip ${f.view === k ? "on" : ""}" data-v="${k}">${l}</button>`).join("") +
      `<span class="sp"></span><input type="search" id="q" placeholder="Search id or subject" value="${esc(f.q)}">`;
    document.querySelectorAll(".chip").forEach((b) => b.onclick = () => { f.view = b.dataset.v; bar(); rows(); });
    $("q").oninput = (e) => { f.q = e.target.value; rows(); };
  }
  function visible() {
    return cases.filter((c) => {
      const cmp = c.category === "BL_COMPARISON";
      if (f.view === "attention" && !(cmp && c.status !== "OK" && !c.review) && !c.error) return false;
      if (["MISMATCH", "NEEDS_REVIEW", "OK"].includes(f.view) && !(cmp && c.status === f.view)) return false;
      if (f.view === "decided" && !c.review) return false;
      const q = f.q.trim().toLowerCase();
      return !q || c.email_id.toLowerCase().includes(q) || (c.subject || "").toLowerCase().includes(q);
    });
  }
  function rows() {
    const v = visible();
    $("rows").innerHTML = v.map((c) => `<tr data-id="${esc(c.email_id)}" class="${sel === c.email_id ? "sel" : ""}"><td><code>${esc(c.email_id)}</code></td><td class="subj" title="${esc(c.subject)}">${esc(c.subject)}</td><td class="muted">${esc(c.category)}</td><td>${tag(c)}</td></tr>`).join("");
    $("none").hidden = v.length > 0;
    document.querySelectorAll("#rows tr").forEach((tr) => tr.onclick = () => { sel = tr.dataset.id; rows(); detail(); });
  }
  async function detail() {
    if (!sel) return;
    let c;
    try { c = await api(`/api/batches/${id}/cases/${encodeURIComponent(sel)}`); } catch (e) { $("detail").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (!alive) return;
    let h = `<h2>${esc(c.subject)}</h2><div class="muted"><code>${esc(c.email_id)}</code> · ${esc(c.category)} · classified by <b>${esc(c.category_method)}</b> (${Math.round((c.category_confidence || 0) * 100)}%)</div>`;
    if (c.error) h += `<h3>Processing failure</h3><div class="note v-mismatch">${esc(c.error)}</div><div class="muted">This is a system failure, not a verdict. Use “Retry failed”.</div>`;
    if (c.category === "BL_COMPARISON" && !c.error) {
      h += `<h3>Result</h3><div>${tag(c)}${c.review_reason ? ` <b>${esc(REASON[c.review_reason] || c.review_reason)}</b>` : ""}</div>`;
      h += `<div class="note">${esc(c.evidence?.review_detail || c.summary)}</div>`;
      if ((c.comparisons || []).length) {
        h += `<h3>Field comparison (SI vs draft BL)</h3><table class="cmp"><thead><tr><th>Field</th><th>SI</th><th>BL</th><th></th></tr></thead><tbody>` +
          c.comparisons.map((x) => `<tr class="${x.verdict === "mismatch" ? "hot" : x.verdict === "uncertain" ? "unc" : ""}"><td>${esc(LABEL[x.field] || x.field)}</td><td>${esc(x.si_value)}</td><td>${esc(x.bl_value)}</td><td class="v-${esc(x.verdict)}">${esc(x.verdict)}${x.reason && x.verdict !== "mismatch" ? `<div class="muted" style="font-weight:400">${esc(x.reason)}</div>` : ""}</td></tr>`).join("") + `</tbody></table>`;
      }
      const docs = c.evidence?.documents || [];
      if (docs.length) h += `<h3>Documents</h3>` + docs.map((d) => `<div class="note"><code>${esc(d.name)}</code> · read as <b>${esc(d.type)}</b>${d.readable ? "" : ` · <span class="v-uncertain">${esc(d.error)}</span>`}</div>`).join("");
      const sr = c.evidence?.scan_reading;
      if (sr) h += `<h3>Scan reading (unverified)</h3><div class="note">${esc(sr.summary || "The scan was read, but no comparison was possible.")}</div>` +
        (sr.documents || []).map((d) => `<div class="note"><code>${esc(d.name)}</code> · ${esc(d.engine)}<br>` + Object.entries(d.fields).map(([k, v]) => `${esc(LABEL[k] || k)}: <b>${esc(v)}</b>`).join("<br>") + `</div>`).join("");
      if (c.status !== "OK") {
        h += `<h3>Decision</h3>`;
        if (c.review) h += `<div class="note"><b>${esc(c.review.decision.replace("_", " "))}</b>${c.review.note ? ` — ${esc(c.review.note)}` : ""}</div>`;
        h += `<textarea id="note" placeholder="Note for the record (optional)"></textarea><div class="acts">` +
          [["confirmed_defect", "Confirm defect", "pri"], ["dismissed", "Dismiss as false alarm", ""], ["reviewed_ok", "Reviewed – fine", ""]].map(([d, l, cl]) => `<button class="${cl}" data-d="${d}">${l}</button>`).join("") + `</div>`;
      }
    }
    const notes = c.evidence?.triage_notes || [];
    if (notes.length) h += `<h3>How it was classified</h3>` + notes.map((n) => `<div class="note">${esc(n)}</div>`).join("");
    $("detail").innerHTML = h;
    document.querySelectorAll(".acts button").forEach((b) => b.onclick = async () => {
      try { await post(`/api/batches/${id}/cases/${encodeURIComponent(c.email_id)}/review`, {decision: b.dataset.d, note: $("note").value}); await loadCases(); detail(); } catch (e) { toast(e.message); }
    });
  }
  async function loadCases() {
    cases = await api(`/api/batches/${id}/cases?view=summary`); lastLoad = Date.now();
    if (!alive) return; stats(); rows();
  }
  function paint(v) {
    info = v; $("title").textContent = id === "uploads" ? "Two-document checks" : v.meta.name;
    const st = $("state"); st.textContent = STATE[v.state] || v.state; st.className = "state " + v.state;
    const j = v.job, busy = j.running;
    $("job").hidden = !busy && !j.error;
    if (busy) {
      $("jobtext").textContent = j.phase === "preparing" ? "Unpacking the inbox…" : `Processing ${j.done} / ${j.total} emails`;
      $("jobbar").style.width = (j.total ? (100 * j.done) / j.total : 4) + "%";
    } else if (j.error) { $("jobtext").textContent = "Stopped: " + j.error; $("jobbar").style.width = "0"; }
    $("run").disabled = busy || v.state === "empty"; $("run").hidden = id === "uploads";
    $("retry").disabled = busy;
  }
  async function poll() {
    if (!alive || token !== running) return;
    try {
      const v = await api(`/api/batches/${id}/status`); paint(v);
      if (v.job.running) { if (Date.now() - lastLoad > 1200) await loadCases(); pollTimer = setTimeout(poll, 600); }
      else { await loadCases(); if (sel) detail(); }
    } catch (e) { toast(e.message); }
  }
  $("run").onclick = async () => { try { sel = null; $("detail").innerHTML = '<div class="empty">Select a case to see the evidence.</div>'; f.view = "all"; bar(); await post(`/api/batches/${id}/run`, {}); poll(); } catch (e) { toast(e.message); } };
  $("retry").onclick = async () => { $("retry").textContent = "Retrying…"; try { const r = await post(`/api/batches/${id}/retry`); $("retry").textContent = `Retried ${r.retried}, recovered ${r.recovered}`; await loadCases(); } catch (e) { $("retry").textContent = "Retry failed"; toast(e.message); } };

  bar();
  try { paint(await api(`/api/batches/${id}`)); } catch (e) { $("app").innerHTML = `<div class="empty">${esc(e.message)}</div><div style="text-align:center"><a href="#/">Back</a></div>`; return; }
  if (openCase) f.view = "all";
  await poll();
  if (sel) detail();
}

route();
