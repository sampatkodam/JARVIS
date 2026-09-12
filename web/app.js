async function api(url, options = {}) {
  const r = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
  return r.json();
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const LABELS = {
  waiting:"WAITING", running:"RUNNING", retrying:"RETRYING", paused:"PAUSED",
  pausing:"PAUSING", cancelling:"CANCELLING", failed:"FAILED", completed:"COMPLETED", cancelled:"CANCELLED"
};
const logCursors = {};

async function health() {
  const x = await api("/api/health");
  document.getElementById("health").textContent = x.status === "ok"
    ? `ONLINE · WORKER ${String(x.worker || "unknown").toUpperCase()}` : "OFFLINE";
}

async function createAndRun() {
  const goal = document.getElementById("goal").value.trim();
  if (!goal) return;
  await api("/api/tasks", {method:"POST", body:JSON.stringify({goal})});
  document.getElementById("goal").value = "";
  loadTasks();
}

async function runTask(id) { await api(`/api/tasks/${id}/run`, {method:"POST"}); loadTasks(); }
async function pauseTask(id) { await api(`/api/tasks/${id}/pause`, {method:"POST"}); loadTasks(); }
async function resumeTask(id) { await api(`/api/tasks/${id}/resume`, {method:"POST"}); loadTasks(); }
async function cancelTask(id) { await api(`/api/tasks/${id}/cancel`, {method:"POST"}); loadTasks(); }

async function loadLogs(id) {
  const after = logCursors[id] || 0;
  const d = await api(`/api/tasks/${id}/logs?after_id=${after}&limit=500`);
  const target = document.getElementById(`logs-${id}`);
  if (!target) return;
  for (const log of (d.logs || [])) {
    const line = document.createElement("div");
    line.className = `log log-${esc(log.level)}`;
    const time = new Date(log.created_at).toLocaleTimeString();
    line.innerHTML = `<span class="log-time">${esc(time)}</span><span class="log-level">${esc(log.level.toUpperCase())}</span><span>${esc(log.message)}</span>`;
    target.appendChild(line);
    logCursors[id] = Math.max(logCursors[id] || 0, Number(log.id));
  }
  target.scrollTop = target.scrollHeight;
}

function controlsFor(t) {
  if (t.status === "waiting" || t.status === "retrying") {
    return `<button class="pause" onclick="pauseTask('${t.id}')">Pause</button><button class="danger" onclick="cancelTask('${t.id}')">Cancel</button>`;
  }
  if (t.status === "running") {
    return `<button class="pause" onclick="pauseTask('${t.id}')">Pause</button><button class="danger" onclick="cancelTask('${t.id}')">Cancel</button>`;
  }
  if (t.status === "paused") {
    return `<button class="run" onclick="resumeTask('${t.id}')">Resume</button><button class="danger" onclick="cancelTask('${t.id}')">Cancel</button>`;
  }
  if (t.status === "pausing") {
    return `<button class="pause" disabled>Pausing…</button><button class="danger" onclick="cancelTask('${t.id}')">Cancel</button>`;
  }
  if (t.status === "cancelling") {
    return `<button class="danger" disabled>Cancelling…</button>`;
  }
  if (t.status === "failed" || t.status === "cancelled") {
    return `<button class="run" onclick="runTask('${t.id}')">Queue again</button>`;
  }
  return "";
}

async function loadTasks() {
  const tasks = await api("/api/tasks");
  const box = document.getElementById("tasks");
  if (!tasks.length) { box.innerHTML = '<p class="muted">No tasks yet.</p>'; return; }
  box.innerHTML = tasks.map(t => {
    const retry = t.retry_count ? `<span class="muted">retry ${esc(t.retry_count)}</span>` : "";
    const action = controlsFor(t);
    return `<article class="task">
      <div class="task-top"><div class="status status-${esc(t.status)}">${esc(LABELS[t.status] || t.status)}</div><div class="actions">${retry}${action}</div></div>
      <div class="goal">${esc(t.goal)}</div>
      ${t.result ? `<div class="step passed">${esc(t.result)}</div>` : ""}
      ${t.error ? `<div class="error">${esc(t.error)}</div>` : ""}
      <div class="muted">Updated ${esc(t.updated_at || "")}</div>
      <details open><summary>Live task log</summary><div id="logs-${t.id}" class="logs"></div></details>
      <div id="steps-${t.id}" class="muted">Loading steps…</div>
    </article>`;
  }).join("");

  for (const t of tasks) {
    await loadLogs(t.id);
    const d = await api(`/api/tasks/${t.id}`);
    const s = d.steps || [];
    const target = document.getElementById(`steps-${t.id}`);
    if (!target) continue;
    target.innerHTML = s.length
      ? s.map(x => `<div class="step"><b>${esc(x.status)}</b> — ${esc(x.description)}<br><span class="muted">${esc((x.critique || "").slice(0,400))}</span></div>`).join("")
      : "No execution steps yet.";
  }
}

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c])); }

health();
loadTasks();
setInterval(() => { health(); loadTasks(); }, 2000);
