async function api(url, options = {}) {
  const r = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
  return r.json();
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const LABELS = {waiting: "WAITING", running: "RUNNING", retrying: "RETRYING", failed: "FAILED", completed: "COMPLETED", cancelled: "CANCELLED"};

async function health() {
  const x = await api("/api/health");
  document.getElementById("health").textContent = x.status === "ok"
    ? `ONLINE · WORKER ${String(x.worker || "unknown").toUpperCase()}` : "OFFLINE";
}

async function createAndRun() {
  const goal = document.getElementById("goal").value.trim();
  if (!goal) return;
  await api("/api/tasks", {method: "POST", body: JSON.stringify({goal})});
  document.getElementById("goal").value = "";
  loadTasks();
}

async function runTask(id) {
  await api(`/api/tasks/${id}/run`, {method: "POST"});
  loadTasks();
}

async function cancelTask(id) {
  await api(`/api/tasks/${id}/cancel`, {method: "POST"});
  loadTasks();
}

async function loadTasks() {
  const tasks = await api("/api/tasks");
  const box = document.getElementById("tasks");
  if (!tasks.length) {
    box.innerHTML = '<p class="muted">No tasks yet.</p>';
    return;
  }
  box.innerHTML = tasks.map(t => {
    const retry = t.retry_count ? `<span class="muted">retry ${esc(t.retry_count)}</span>` : "";
    const action = TERMINAL.has(t.status)
      ? (t.status === "failed" || t.status === "cancelled" ? `<button class="run" onclick="runTask('${t.id}')">Queue again</button>` : "")
      : `<button class="run" onclick="cancelTask('${t.id}')">Cancel</button>`;
    return `<article class="task">
      <div class="task-top"><div class="status status-${esc(t.status)}">${esc(LABELS[t.status] || t.status)}</div><div class="actions">${retry}${action}</div></div>
      <div class="goal">${esc(t.goal)}</div>
      ${t.result ? `<div class="step passed">${esc(t.result)}</div>` : ""}
      ${t.error ? `<div class="error">${esc(t.error)}</div>` : ""}
      <div class="muted">Updated ${esc(t.updated_at || "")}</div>
      <div id="steps-${t.id}" class="muted">Loading steps…</div>
    </article>`;
  }).join("");

  for (const t of tasks) {
    const d = await api(`/api/tasks/${t.id}`);
    const s = d.steps || [];
    const target = document.getElementById(`steps-${t.id}`);
    if (!target) continue;
    target.innerHTML = s.length
      ? s.map(x => `<div class="step"><b>${esc(x.status)}</b> — ${esc(x.description)}<br><span class="muted">${esc((x.critique || "").slice(0, 400))}</span></div>`).join("")
      : "No execution steps yet.";
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[c]));
}

health();
loadTasks();
setInterval(() => { health(); loadTasks(); }, 2000);
