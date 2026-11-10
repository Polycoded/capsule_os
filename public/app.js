const state = {
  selected: null,
  capsules: [],
  currentChanges: { file_changes: [], registry_changes: [] },
};

document.getElementById("refreshBtn").addEventListener("click", refresh);
document.getElementById("runForm").addEventListener("submit", runCapsule);
document.getElementById("commitSelectedBtn").addEventListener("click", commitSelected);

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error || "Request failed");
  return data;
}

async function runCapsule(event) {
  event.preventDefault();
  const payload = {
    command: document.getElementById("command").value,
    cwd: document.getElementById("cwd").value,
    watch_paths: document
      .getElementById("watchPaths")
      .value.split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean),
    memory_limit_mb: document.getElementById("memoryLimit").value || null,
    runtime_limit_seconds: document.getElementById("runtimeLimit").value || null,
    priority: document.getElementById("priority").value,
    reversibility_budget: Number(document.getElementById("reversibilityBudget").value),
    auto_suspend_on_budget: document.getElementById("autoSuspend").checked,
  };
  const capsule = await api("/capsules/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.selected = capsule.capsule_id;
  await refresh();
}

async function refresh() {
  const [capsules, metrics] = await Promise.all([api("/capsules"), api("/metrics/system")]);
  state.capsules = capsules;
  renderMetrics(metrics);
  renderCapsules();
  if (state.selected) {
    await selectCapsule(state.selected);
  }
}

function renderMetrics(metrics) {
  const cpu = metrics.cpuPercent ?? metrics.cpu_percent ?? "--";
  document.getElementById("cpuMetric").textContent = Number.isFinite(Number(cpu)) ? `${cpu}% CPU` : "CPU unavailable";
  const total = Number(metrics.totalMemory || 0);
  const free = Number(metrics.freeMemory || 0);
  if (total && free) {
    const used = total - free;
    document.getElementById("memoryMetric").textContent = `${formatBytes(used)} of ${formatBytes(total)} used`;
  } else {
    document.getElementById("memoryMetric").textContent = "Memory unavailable";
  }
}

function renderCapsules() {
  const list = document.getElementById("capsuleList");
  if (!state.capsules.length) {
    list.innerHTML = `<div class="empty">No capsules yet. Launch the demo writer to create the first journal.</div>`;
    return;
  }
  list.innerHTML = state.capsules
    .map((capsule) => {
      const riskClass = capsule.risk_score >= 50 ? "risk-high" : capsule.risk_score >= 25 ? "risk-mid" : "";
      return `
        <article class="capsule-row ${state.selected === capsule.capsule_id ? "selected" : ""}">
          <div>
            <div class="capsule-title">${escapeHtml(capsule.command.join(" "))}</div>
            <div class="meta">
              <span class="pill">${capsule.state}</span>
              <span class="pill ${riskClass}">risk ${capsule.risk_score}</span>
              <span class="pill ${capsule.reversibility_state === "budget_exceeded" ? "risk-high" : ""}">
                rollback debt ${capsule.reversibility_debt}/${capsule.reversibility_budget}
              </span>
              <span class="pill">${capsule.process_count} processes</span>
              <span class="pill">${capsule.file_change_count} file changes</span>
              <span class="pill">${capsule.registry_change_count} registry changes</span>
            </div>
          </div>
          <div class="row-actions">
            <button class="ghost" onclick="selectCapsule('${capsule.capsule_id}')">Inspect</button>
            <button class="ghost" onclick="postAction('${capsule.capsule_id}', 'suspend')">Suspend</button>
            <button class="ghost" onclick="postAction('${capsule.capsule_id}', 'resume')">Resume</button>
            <button class="ghost" onclick="postAction('${capsule.capsule_id}', 'commit')">Commit</button>
            <button class="danger" onclick="postAction('${capsule.capsule_id}', 'rollback')">Rollback</button>
          </div>
        </article>
      `;
    })
    .join("");
}

async function selectCapsule(id) {
  state.selected = id;
  renderCapsules();
  const [changes, report] = await Promise.all([api(`/capsules/${id}/changes`), api(`/capsules/${id}/report`)]);
  state.currentChanges = changes;
  renderChanges(changes);
  document.getElementById("reportBox").textContent = JSON.stringify(report, null, 2);
}

async function postAction(id, action) {
  await api(`/capsules/${id}/${action}`, { method: "POST", body: "{}" });
  state.selected = id;
  await refresh();
}

function renderChanges(changes) {
  const list = document.getElementById("changeList");
  const items = [
    ...changes.file_changes.map((item, index) => ({ type: item.change_type, target: item.path, kind: "file", index })),
    ...changes.registry_changes.map((item, index) => ({
      type: item.change_type,
      target: `${item.hive_path}\\${item.value_name}`,
      kind: "registry",
      index,
    })),
  ];
  document.getElementById("commitSelectedBtn").disabled = !items.length;
  if (!items.length) {
    list.innerHTML = `<div class="empty">No file or startup-registry changes detected yet.</div>`;
    return;
  }
  list.innerHTML = items
    .map(
      (item) => `
        <div class="change-item">
          <input class="change-choice" type="checkbox" data-kind="${item.kind}" data-index="${item.index}" aria-label="Keep ${escapeHtml(item.target)}" />
          <span class="pill">${item.type}</span>
          <code>${escapeHtml(item.target)}</code>
          <span class="pill">${item.kind}</span>
        </div>
      `,
    )
    .join("");
}

async function commitSelected() {
  if (!state.selected) return;
  const selected = [...document.querySelectorAll(".change-choice:checked")];
  const keepFiles = [];
  const keepRegistry = [];
  for (const choice of selected) {
    const index = Number(choice.dataset.index);
    if (choice.dataset.kind === "file") {
      keepFiles.push(state.currentChanges.file_changes[index].path);
    } else {
      const item = state.currentChanges.registry_changes[index];
      keepRegistry.push({ hive_path: item.hive_path, value_name: item.value_name });
    }
  }
  await api(`/capsules/${state.selected}/commit-selected`, {
    method: "POST",
    body: JSON.stringify({ keep_files: keepFiles, keep_registry: keepRegistry }),
  });
  await refresh();
}

async function initialize() {
  const config = await api("/config");
  document.getElementById("cwd").value = config.root_path;
  document.getElementById("watchPaths").value = config.workspace_path;
  await refresh();
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

initialize();
setInterval(refresh, 3000);
