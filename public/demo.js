const demo = {
  capsuleId: null,
  capsule: null,
  report: null,
  pollTimer: null,
  firstObligationId: null,
  sealObserved: false,
};

const byId = (id) => document.getElementById(id);
const startBtn = byId("startBtn");
const controlBtn = byId("controlBtn");
const commitBtn = byId("commitBtn");
const rollbackBtn = byId("rollbackBtn");
const exportBtn = byId("exportBtn");
const meter = document.querySelector(".meter");

startBtn.addEventListener("click", startDemo);
controlBtn.addEventListener("click", toggleProcess);
commitBtn.addEventListener("click", commitDemo);
rollbackBtn.addEventListener("click", rollbackDemo);
exportBtn.addEventListener("click", exportReport);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "The demo request failed.");
  return data;
}

async function startDemo() {
  clearInterval(demo.pollTimer);
  resetView();
  startBtn.disabled = true;
  setStatus("running", "Creating the execution capsule", "Capturing the baseline before the process starts.");
  setStage("stageLaunch", "active");

  try {
    const capsule = await api("/api/demo/start", { method: "POST", body: "{}" });
    demo.capsuleId = capsule.capsule_id;
    demo.capsule = capsule;
    renderCapsule(capsule);
    setStage("stageLaunch", "complete");
    setStage("stageObserve", "active");
    setStatus("running", "Preparing recovery obligations", "Each protected write waits for durable inverse material.");
    controlBtn.disabled = false;
    demo.pollTimer = setInterval(refreshDemo, 700);
  } catch (error) {
    setStatus("error", "Demo could not start", error.message);
    startBtn.disabled = false;
  }
}

async function refreshDemo() {
  if (!demo.capsuleId) return;
  try {
    const capsule = await api(`/api/capsules/${demo.capsuleId}`);
    demo.capsule = capsule;
    renderCapsule(capsule);
    const obligations = capsule.obligations || [];
    const sealedPending = obligations.find((item) => item.state === "sealed" && !item.exposed_at);

    if (obligations.length > 0) {
      setStage("stageObserve", "complete");
      setStage("stageGovern", "active");
    }

    if (sealedPending) {
      demo.sealObserved = true;
      const fileName = sealedPending.resource_id.split(/[\\/]/).pop();
      setStatus(
        "running",
        "Write held at the admission gate",
        `${fileName} cannot change until its recovery material is durably sealed.`,
      );
    } else if (obligations.some((item) => item.exposed_at) && capsule.file_changes.length > 0) {
      setStatus(
        "running",
        "Protected effects admitted with recovery proof",
        `${capsule.invariant?.exposed_effects || 0} writes completed only after sealing.`,
      );
    }

    if (capsule.file_changes.length > 0) {
      setStage("stageGovern", "complete");
      setStage("stageDecide", "active");
      commitBtn.disabled = false;
      rollbackBtn.disabled = false;
    }

    if (capsule.state === "invariant_suspended") {
      clearInterval(demo.pollTimer);
      setStage("stageGovern", "complete");
      setStage("stageDecide", "active");
      setStatus(
        "suspended",
        "Scheduler suspended the full process tree",
        (capsule.invariant?.reasons || ["A recovery obligation could not be maintained."]).join(" "),
      );
      byId("decisionTitle").textContent = "Choose what becomes permanent";
      byId("decisionCopy").textContent =
        "Resume for inspection, keep the observed changes, or restore the laptop.";
      controlBtn.textContent = "Resume process";
      controlBtn.disabled = false;
    } else if (["exited", "terminated"].includes(capsule.state)) {
      clearInterval(demo.pollTimer);
      setStage("stageDecide", "active");
      setStatus("", "Workload stopped", "Its recorded changes are ready for a final decision.");
    }
  } catch (error) {
    clearInterval(demo.pollTimer);
    setStatus("error", "Status check failed", error.message);
    startBtn.disabled = false;
  }
}

async function toggleProcess() {
  if (!demo.capsuleId || !demo.capsule) return;
  const shouldResume = ["suspended", "budget_suspended", "invariant_suspended"].includes(demo.capsule.state);
  controlBtn.disabled = true;
  try {
    const action = shouldResume ? "resume" : "suspend";
    const capsule = await api(`/api/capsules/${demo.capsuleId}/${action}`, {
      method: "POST",
      body: "{}",
    });
    demo.capsule = capsule;
    renderCapsule(capsule);
    controlBtn.textContent = shouldResume ? "Suspend process" : "Resume process";
    controlBtn.disabled = false;
    setStatus(
      shouldResume ? "running" : "suspended",
      shouldResume ? "Process tree resumed" : "Process tree suspended",
      shouldResume
        ? "The scheduling override is recorded in the audit trail."
        : "Every contained process has been paused together.",
    );
    clearInterval(demo.pollTimer);
    demo.pollTimer = setInterval(refreshDemo, 700);
  } catch (error) {
    setStatus("error", "Process control failed", error.message);
    controlBtn.disabled = false;
  }
}

async function rollbackDemo() {
  if (!demo.capsuleId) return;
  disableDecisions();
  clearInterval(demo.pollTimer);
  setStage("stageDecide", "complete");
  setStage("stageRecover", "active");
  setStatus("running", "Restoring the original machine state", "Terminating the process tree and applying the recovery journal.");

  try {
    const capsule = await api(`/api/capsules/${demo.capsuleId}/rollback`, {
      method: "POST",
      body: "{}",
    });
    await finishDemo(capsule, "rolled_back");
  } catch (error) {
    setStatus("error", "Rollback failed", error.message);
    rollbackBtn.disabled = false;
  }
}

async function commitDemo() {
  if (!demo.capsuleId) return;
  disableDecisions();
  clearInterval(demo.pollTimer);
  setStage("stageDecide", "complete");
  setStage("stageRecover", "active");
  setStatus("running", "Committing observed changes", "The journal is preserving the current file state.");

  try {
    const capsule = await api(`/api/capsules/${demo.capsuleId}/commit`, {
      method: "POST",
      body: "{}",
    });
    await finishDemo(capsule, "committed");
  } catch (error) {
    setStatus("error", "Commit failed", error.message);
    commitBtn.disabled = false;
  }
}

async function finishDemo(capsule, outcome) {
  demo.capsule = capsule;
  demo.report = await api(`/api/capsules/${demo.capsuleId}/report`);
  renderCapsule(capsule);
  setStage("stageRecover", "complete");
  exportBtn.disabled = false;
  startBtn.textContent = "Run full demo again";
  startBtn.disabled = false;

  const rollbackEvent = [...capsule.audit].reverse().find((item) => item.event === "changes_rolled_back");
  const fileResult = rollbackEvent?.detail?.file_result || {};
  const rolledBack = outcome === "rolled_back";
  setStatus(
    "complete",
    rolledBack ? "Complete machine state restored" : "Changes committed",
    rolledBack
      ? `${fileResult.restored || 0} original file restored; ${fileResult.deleted || 0} temporary files removed.`
      : "The observed changes were intentionally kept.",
  );

  byId("resultBand").hidden = false;
  const certificate = capsule.recovery_certificate || {};
  const complete = certificate.verified && certificate.recovery_complete;
  byId("resultTitle").textContent = complete
    ? (rolledBack ? "Recovery verified" : "Commit verified")
    : "Verification requires review";
  byId("resultCopy").textContent = rolledBack
    ? "CapsuleOS terminated the entire process tree and returned the watched workspace to its captured baseline."
    : "CapsuleOS closed the capsule and preserved the recorded changes as an explicit user decision.";
  byId("finalState").textContent = formatState(capsule.state);
  byId("restoredCount").textContent = String(fileResult.restored || 0);
  byId("removedCount").textContent = String(fileResult.deleted || 0);
  byId("auditCount").textContent = String(capsule.audit.length);
  byId("resultCopy").textContent += complete
    ? ` Certificate ${certificate.certificate_id.slice(0, 12)} verified.`
    : " The certificate records unresolved or unverifiable effects.";
}

function renderCapsule(capsule) {
  const processes = capsule.processes || [];
  const obligations = capsule.obligations || [];
  const invariant = capsule.invariant || {};
  const memoryBytes = processes.reduce((sum, process) => sum + Number(process.memory_bytes || 0), 0);
  const covered = Number(invariant.covered_effects || 0);
  const exposed = Number(invariant.exposed_effects || 0);
  const total = Math.max(obligations.length, 1);

  byId("capsuleId").textContent = `Capsule ${capsule.capsule_id}`;
  byId("processCount").textContent = String(processes.length);
  byId("memoryValue").textContent = formatBytes(memoryBytes);
  byId("fileCount").textContent = String(obligations.length);
  byId("riskValue").textContent = invariant.satisfied === false ? "Violated" : "Satisfied";
  byId("riskLabel").textContent = `${invariant.unresolved_conflicts || 0} unresolved conflicts`;
  byId("priorityValue").textContent = formatState(capsule.priority);
  byId("memoryLimit").textContent = `${capsule.memory_limit_mb || 0} MB`;
  byId("runtimeLimit").textContent = `${capsule.runtime_limit_seconds || 0} sec`;
  byId("registryValue").textContent = capsule.registry_changes.length
    ? `${capsule.registry_changes.length} change detected`
    : "Protected; unchanged";
  const baseline = capsule.baseline_assessment || {};
  byId("baselineState").textContent = baseline.state === "warming"
    ? `Warming ${baseline.sample_count || 0}/${baseline.minimum_samples || 5}`
    : formatState(baseline.state || capsule.baseline_mode || "static");
  byId("journalState").textContent = obligations.length ? `${covered} effects covered` : "Contract active";
  renderEdgeCapacity(capsule.edge_capacity || {});

  byId("debtValue").textContent = String(covered);
  byId("budgetValue").textContent = String(obligations.length);
  const width = obligations.length ? Math.min(100, (covered / obligations.length) * 100) : 0;
  byId("meterFill").style.width = `${width}%`;
  byId("meterFill").classList.toggle("exceeded", invariant.satisfied === false);
  meter.setAttribute("aria-valuemax", String(total));
  meter.setAttribute("aria-valuenow", String(Math.min(covered, total)));
  byId("debtReason").textContent = invariant.satisfied === false
    ? (invariant.reasons || []).join(" | ")
    : `${exposed} effects completed after their recovery obligations were sealed.`;

  renderProcesses(processes, capsule.root_pid, capsule.state);
  renderChanges(obligations);
  renderModelProof(obligations, capsule.recovery_certificate || {});
  renderRisk(invariant.reasons || []);
  renderAudit(capsule.audit || []);
}

function renderEdgeCapacity(capacity) {
  const decision = capacity.decision || "awaiting";
  const decisionEl = byId("edgeDecision");
  decisionEl.textContent = formatState(decision);
  decisionEl.className = decision === "admit" ? "admit" : (decision === "deny" ? "deny" : "");
  byId("edgeReason").textContent = capacity.reasons?.length
    ? capacity.reasons[0]
    : (decision === "admit" ? "All constraints satisfied" : "Live decision");
  byId("edgeReserve").textContent = capacity.reserve_remaining_after_bytes == null
    ? "-"
    : formatCapacity(capacity.reserve_remaining_after_bytes);
  byId("edgeRollback").textContent = capacity.estimated_rollback_ms == null
    ? "-"
    : `${capacity.estimated_rollback_ms} ms`;
  byId("edgeDeadline").textContent = capacity.rollback_deadline_ms == null
    ? "Bounded deadline"
    : `Within ${capacity.rollback_deadline_ms} ms`;
  byId("edgeFlash").textContent = capacity.flash_budget_remaining_after_bytes == null
    ? "-"
    : formatCapacity(capacity.flash_budget_remaining_after_bytes);
}

function renderModelProof(obligations, certificate) {
  if (!obligations.length) {
    resetModelProof();
    return;
  }

  const first = obligations[0];
  if (!demo.firstObligationId) demo.firstObligationId = first.obligation_id;
  const tracked = obligations.find((item) => item.obligation_id === demo.firstObligationId) || first;
  const fileName = tracked.resource_id.split(/[\\/]/).pop();
  const digest = String(tracked.pre_version?.digest || "");
  const hasInverse = tracked.inverse_action && (
    tracked.backup_path ||
    tracked.inverse_action === "delete_created_resource"
  );
  const isSealed = Boolean(tracked.sealed_at);
  const isExposed = Boolean(tracked.exposed_at);
  const isVerified = tracked.state === "verified" && certificate.verified;

  setProofStep("proofIntercept", true, false);
  byId("proofInterceptValue").textContent = `${formatState(tracked.operation)} ${fileName}`;

  setProofStep("proofCapture", Boolean(hasInverse), !hasInverse);
  byId("proofCaptureValue").textContent = hasInverse
    ? (digest ? `SHA-256 ${digest.slice(0, 10)}...` : formatState(tracked.inverse_action))
    : "Capturing pre-operation state";

  setProofStep("proofSeal", isSealed, hasInverse && !isSealed);
  byId("proofSealValue").textContent = isSealed
    ? `${formatBytes(tracked.reserved_bytes || 0)} reserved`
    : "Flushing recovery record";

  setProofStep("proofAdmit", isExposed, isSealed && !isExposed);
  byId("proofAdmitValue").textContent = isExposed
    ? `Sequence #${tracked.sequence} exposed`
    : "Write remains blocked";

  setProofStep("proofVerify", isVerified, isExposed && !isVerified);
  byId("proofVerifyValue").textContent = isVerified
    ? `Certificate ${certificate.certificate_id.slice(0, 8)}`
    : "Pending commit or rollback";

  byId("gateState").className = `gate-state ${isVerified ? "verified" : (isExposed ? "admitted" : "holding")}`;
  byId("gateState").textContent = isVerified
    ? "Recovery certified"
    : (isExposed ? "Write admitted after seal" : "Write held before completion");
}

function setProofStep(id, complete, active) {
  const step = byId(id);
  step.classList.toggle("complete", complete);
  step.classList.toggle("active", active);
}

function resetModelProof() {
  ["proofIntercept", "proofCapture", "proofSeal", "proofAdmit", "proofVerify"].forEach((id) => {
    byId(id).className = "";
  });
  byId("proofInterceptValue").textContent = "No request";
  byId("proofCaptureValue").textContent = "Waiting";
  byId("proofSealValue").textContent = "Waiting";
  byId("proofAdmitValue").textContent = "Blocked";
  byId("proofVerifyValue").textContent = "Pending decision";
  byId("gateState").className = "gate-state";
  byId("gateState").textContent = "Waiting for operation";
  renderEdgeCapacity({});
}

function renderProcesses(processes, rootPid, capsuleState) {
  if (!processes.length) {
    byId("processList").className = "empty-state";
    byId("processList").textContent = "Process records will appear here.";
    return;
  }
  byId("processList").className = "process-list";
  byId("processList").innerHTML = processes
    .map((process) => {
      const isRoot = process.pid === rootPid;
      let state = process.state;
      if (["suspended", "budget_suspended", "invariant_suspended"].includes(capsuleState)) state = "suspended";
      if (["rolled_back", "committed", "terminated"].includes(capsuleState)) state = "terminated";
      return `
        <div class="process-row ${isRoot ? "root-process" : ""}">
          <span class="tree-mark">${isRoot ? "ROOT" : "CHILD"}</span>
          <span><strong>${escapeHtml(process.name || "process")}</strong><small>PID ${process.pid}</small></span>
          <span>${formatBytes(process.memory_bytes || 0)}<small>memory</small></span>
          <span class="state-tag">${formatState(state)}</span>
        </div>`;
    })
    .join("");
}

function renderChanges(obligations) {
  if (!obligations.length) {
    byId("changeList").className = "empty-state";
    byId("changeList").textContent = "No persistent effect has requested admission.";
    return;
  }
  byId("changeList").className = "change-list";
  byId("changeList").innerHTML = obligations
    .slice(-5)
    .reverse()
    .map((obligation) => {
      const name = obligation.resource_id.split(/[\\/]/).pop();
      return `
        <div class="change-row">
          <span class="change-type ${obligation.state}">${obligation.operation}</span>
          <span><strong>${escapeHtml(name)}</strong><small>#${obligation.sequence}</small></span>
          <span>${formatState(obligation.state)}</span>
        </div>`;
    })
    .join("");
}

function renderRisk(reasons) {
  byId("riskReasons").innerHTML = reasons.length
    ? reasons.map((reason) => `<li>${escapeHtml(formatState(reason))}</li>`).join("")
    : "<li>Every observed protected effect has verified recovery coverage.</li>";
}

function renderAudit(audit) {
  const visible = audit.slice(-5).reverse();
  byId("auditList").innerHTML = visible.length
    ? visible.map((item) => `<li>${escapeHtml(auditLabel(item.event))}</li>`).join("")
    : "<li>Waiting for capsule launch.</li>";
}

function resetView() {
  demo.capsuleId = null;
  demo.capsule = null;
  demo.report = null;
  demo.firstObligationId = null;
  demo.sealObserved = false;
  document.querySelectorAll(".stages li").forEach((item) => item.className = "");
  byId("resultBand").hidden = true;
  byId("decisionTitle").textContent = "Actions unlock when changes are detected";
  byId("decisionCopy").textContent =
    "CapsuleOS can suspend, resume, commit, or reverse the complete process tree.";
  controlBtn.textContent = "Suspend process";
  commitBtn.disabled = true;
  rollbackBtn.disabled = true;
  controlBtn.disabled = true;
  exportBtn.disabled = true;
  resetModelProof();
}

function disableDecisions() {
  controlBtn.disabled = true;
  commitBtn.disabled = true;
  rollbackBtn.disabled = true;
}

function setStatus(kind, title, copy) {
  byId("statusDot").className = `status-dot ${kind}`.trim();
  byId("statusTitle").textContent = title;
  byId("statusCopy").textContent = copy;
}

function setStage(id, state) {
  const item = byId(id);
  item.classList.remove("active", "complete");
  item.classList.add(state);
}

function exportReport() {
  if (!demo.report) return;
  const blob = new Blob([JSON.stringify(demo.report, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `capsuleos-report-${demo.capsuleId}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCapacity(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024 * 1024) return formatBytes(value);
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

function formatState(value) {
  return String(value || "unknown")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function riskLabel(score) {
  if (score >= 60) return "high intervention";
  if (score >= 30) return "review required";
  if (score > 0) return "low concern";
  return "no findings";
}

function auditLabel(event) {
  const labels = {
    capsule_started: "Capsule launched and recovery contract registered",
    recovery_obligation_prepared: "Inverse material captured and obligation sealed",
    recovery_obligation_exposed: "Protected effect admitted after sealing",
    edge_capacity_evaluated: "Edge capacity recalculated before the protected effect",
    adaptive_baseline_intervention: "Dynamic baseline lowered priority after an anomaly",
    adaptive_baseline_finalized: "Verified run evaluated for baseline learning",
    recovery_invariant_violated: "Scheduler suspended process tree on invariant violation",
    automatic_suspension_overridden: "User overrode automatic suspension",
    capsule_resumed: "Process tree resumed",
    capsule_suspended: "Process tree manually suspended",
    capsule_terminated: "Process tree terminated",
    changes_rolled_back: "Journal restored the original state",
    changes_committed: "Observed changes committed",
  };
  return labels[event] || formatState(event);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
