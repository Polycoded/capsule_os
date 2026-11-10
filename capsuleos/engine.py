from __future__ import annotations

import json
import os
import shlex
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .adaptive import AdaptiveBaselineStore
from .edge import collect_edge_telemetry, evaluate_edge_admission
from .journal import FileJournal, RegistryJournal
from .models import Capsule, ProcessRecord, RecoveryContract, utc_now
from .recovery import RecoveryLedger, resource_version, versions_equal
from .windows_ops import descendants, launch_process, list_processes, resume, set_priority, suspend, system_metrics, terminate


class CapsuleEngine:
    def __init__(self, root: Path):
        self.root = root
        self.data_dir = root / "data" / "capsules"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = root / "workspace"
        self.workspace_dir.mkdir(exist_ok=True)
        self.capsules: dict[str, Capsule] = {}
        self.file_journals: dict[str, FileJournal] = {}
        self.registry_journals: dict[str, RegistryJournal] = {}
        self.recovery_ledgers: dict[str, RecoveryLedger] = {}
        self.baselines = AdaptiveBaselineStore(root / "data" / "baselines")
        self.processes: dict[str, Any] = {}
        self.lock = threading.RLock()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def run_capsule(self, payload: dict[str, Any]) -> Capsule:
        command = payload.get("command")
        if isinstance(command, str):
            command = shlex.split(command, posix=False)
        if not isinstance(command, list) or not command:
            raise ValueError("command must be a non-empty string or list")

        cwd = str(Path(payload.get("cwd") or self.root).resolve())
        watch_paths = payload.get("watch_paths") or [str(self.workspace_dir)]
        watch_paths = [str(Path(p).resolve()) for p in watch_paths]
        capsule_id = uuid.uuid4().hex[:12]
        capsule_dir = self.data_dir / capsule_id
        capsule_dir.mkdir(parents=True, exist_ok=True)

        file_journal = FileJournal(capsule_dir, watch_paths)
        file_journal.capture_baseline()
        registry_journal = RegistryJournal(capsule_dir)

        contract_payload = dict(payload.get("recovery_contract") or {})
        contract_payload["protected_paths"] = [
            str(Path(path).resolve())
            for path in (contract_payload.get("protected_paths") or watch_paths)
        ]
        recovery_contract = RecoveryContract.from_dict(contract_payload)
        baseline_mode = str(payload.get("baseline_mode") or "adaptive")
        if baseline_mode not in {"static", "adaptive"}:
            raise ValueError("baseline_mode must be static or adaptive")
        baseline_profile_id = self.baselines.profile_id(
            command,
            str(payload.get("baseline_profile") or "") or None,
        )
        ledger = RecoveryLedger(capsule_dir, capsule_id, recovery_contract)
        process_env = os.environ.copy()
        process_env.update(
            {
                "CAPSULEOS_ID": capsule_id,
                "CAPSULEOS_API": str(payload.get("capsuleos_api") or "http://127.0.0.1:8765"),
                "CAPSULEOS_ENFORCEMENT_MODE": recovery_contract.enforcement_mode,
            }
        )
        proc = launch_process(command, cwd, process_env)
        capsule = Capsule(
            capsule_id=capsule_id,
            command=command,
            cwd=cwd,
            watch_paths=watch_paths,
            root_pid=proc.pid,
            runtime_limit_seconds=_optional_int(payload.get("runtime_limit_seconds")),
            memory_limit_mb=_optional_int(payload.get("memory_limit_mb")),
            priority=payload.get("priority") or "normal",
            reversibility_budget=int(payload.get("reversibility_budget") or 70),
            auto_suspend_on_budget=bool(payload.get("auto_suspend_on_budget", True)),
            recovery_contract=recovery_contract,
            baseline_mode=baseline_mode,
            baseline_profile_id=baseline_profile_id,
            update_baseline=bool(payload.get("update_baseline", True)),
            behavior_metrics={
                "peak_memory_bytes": 0,
                "peak_process_count": 1,
                "peak_write_ops": 0,
                "peak_file_changes": 0,
                "peak_obligations": 0,
                "peak_journal_bytes": 0,
                "runtime_ms": 0,
                "max_seal_latency_ms": 0,
            },
        )
        capsule.baseline_assessment = (
            self.baselines.assess(baseline_profile_id, capsule.behavior_metrics)
            if baseline_mode == "adaptive"
            else {
                "profile_id": baseline_profile_id,
                "state": "static",
                "anomalous": False,
                "explanation": "fixed policy thresholds are in use",
            }
        )
        capsule.processes[proc.pid] = ProcessRecord(
            pid=proc.pid,
            parent_pid=None,
            name=Path(command[0]).name,
            command_line=" ".join(command),
        )
        capsule.log(
            "capsule_started",
            {
                "pid": proc.pid,
                "command": command,
                "watch_paths": watch_paths,
                "recovery_contract": recovery_contract.to_dict(),
                "baseline_mode": baseline_mode,
                "baseline_profile_id": baseline_profile_id,
            },
        )
        set_priority(proc.pid, capsule.priority)

        with self.lock:
            self.capsules[capsule_id] = capsule
            self.file_journals[capsule_id] = file_journal
            self.registry_journals[capsule_id] = registry_journal
            self.recovery_ledgers[capsule_id] = ledger
            self.processes[capsule_id] = proc
        self._persist(capsule)
        return capsule

    def list_capsules(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._summary(c) for c in sorted(self.capsules.values(), key=lambda x: x.started_at, reverse=True)]

    def get_capsule(self, capsule_id: str) -> Capsule:
        with self.lock:
            capsule = self.capsules.get(capsule_id)
            if not capsule:
                raise KeyError(capsule_id)
            self._refresh_capsule(capsule_id)
            return capsule

    def changes(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.get_capsule(capsule_id)
        return {
            "capsule_id": capsule_id,
            "file_changes": [c.to_dict() for c in capsule.file_changes],
            "registry_changes": [c.to_dict() for c in capsule.registry_changes],
        }

    def obligations(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.get_capsule(capsule_id)
        return {
            "capsule_id": capsule_id,
            "obligations": [item.to_dict() for item in capsule.obligations],
            "invariant": capsule.invariant.to_dict(),
        }

    def invariant(self, capsule_id: str) -> dict[str, Any]:
        return self.get_capsule(capsule_id).invariant.to_dict()

    def edge_capacity(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.get_capsule(capsule_id)
        return {
            "capsule_id": capsule_id,
            "edge_policy": capsule.recovery_contract.edge_policy.to_dict(),
            "latest_capacity": capsule.edge_capacity,
            "admission_history": capsule.admission_history,
        }

    def list_baselines(self) -> list[dict[str, Any]]:
        return self.baselines.list()

    def get_baseline(self, profile_id: str) -> dict[str, Any]:
        profile = self.baselines.get(profile_id)
        if not profile.get("samples") and profile.get("updated_at") is None:
            raise KeyError(profile_id)
        return profile

    def reset_baseline(self, profile_id: str) -> dict[str, Any]:
        self.baselines.reset(profile_id)
        return {"profile_id": profile_id, "state": "reset"}

    def conflicts(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.get_capsule(capsule_id)
        return {
            "capsule_id": capsule_id,
            "conflicts": [item.to_dict() for item in capsule.conflicts],
        }

    def prepare_obligation(self, capsule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        capsule = self.get_capsule(capsule_id)
        ledger = self.recovery_ledgers[capsule_id]
        resource = Path(str(payload["resource_id"])).resolve()
        admission: dict[str, Any] | None = None
        edge_policy = capsule.recovery_contract.edge_policy
        if edge_policy.enabled:
            telemetry = collect_edge_telemetry(resource)
            pre_version = resource_version(resource)
            flash_written = sum(
                int(item.get("projected_flash_bytes") or 0)
                for item in capsule.admission_history
                if item.get("decision") == "admit"
            )
            recovery_reserved = sum(
                int(item.get("required_recovery_bytes") or 0)
                for item in capsule.admission_history
                if item.get("decision") == "admit"
            )
            admission = evaluate_edge_admission(
                edge_policy,
                telemetry,
                int(pre_version.size or 0),
                recovery_reserved,
                flash_written,
                _optional_int(payload.get("deadline_ms")),
            )
            capsule.edge_capacity = admission
            capsule.admission_history.append(admission)
            capsule.log(
                "edge_capacity_evaluated",
                {
                    "decision": admission["decision"],
                    "required_recovery_bytes": admission["required_recovery_bytes"],
                    "estimated_rollback_ms": admission["estimated_rollback_ms"],
                    "reserve_remaining_after_bytes": admission[
                        "reserve_remaining_after_bytes"
                    ],
                },
            )
        obligation = ledger.prepare_file(
            str(resource),
            str(payload.get("operation") or "write"),
            _optional_int(payload.get("process_id")),
            _optional_int(payload.get("thread_id")),
            list(payload.get("dependencies") or []),
            admission,
        )
        self._sync_recovery(capsule, ledger)
        capsule.log(
            "recovery_obligation_prepared",
            {
                "obligation_id": obligation.obligation_id,
                "resource": obligation.resource_id,
                "state": obligation.state,
                "admission_decision": obligation.admission_decision,
            },
        )
        if obligation.state != "sealed" and capsule.recovery_contract.failure_policy == "fail_closed":
            self._suspend_for_invariant(capsule, obligation.failure_reason or "obligation could not be sealed")
        self._persist(capsule)
        return obligation.to_dict()

    def expose_obligation(self, capsule_id: str, obligation_id: str) -> dict[str, Any]:
        capsule = self.capsules.get(capsule_id)
        if not capsule:
            raise KeyError(capsule_id)
        ledger = self.recovery_ledgers[capsule_id]
        obligation = ledger.expose(obligation_id)
        self._sync_recovery(capsule, ledger)
        capsule.log(
            "recovery_obligation_exposed",
            {"obligation_id": obligation_id, "resource": obligation.resource_id},
        )
        self._persist(capsule)
        return obligation.to_dict()

    def resolve_conflict(self, capsule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        capsule = self.capsules.get(capsule_id)
        if not capsule:
            raise KeyError(capsule_id)
        ledger = self.recovery_ledgers[capsule_id]
        conflict = ledger.resolve_conflict(str(payload["conflict_id"]), str(payload["resolution"]))
        self._sync_recovery(capsule, ledger)
        capsule.log("recovery_conflict_resolved", conflict.to_dict())
        self._persist(capsule)
        return conflict.to_dict()

    def recovery_certificate(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.get_capsule(capsule_id)
        if not capsule.recovery_certificate:
            raise ValueError("recovery certificate is available after commit or rollback")
        return capsule.recovery_certificate

    def driver_status(self) -> dict[str, Any]:
        return {
            "mode": "cooperative",
            "native_agent": "source_available",
            "minifilter": "not_loaded",
            "pre_completion_enforcement": "cooperative_api",
            "fail_closed_scope": "cooperating demo workloads only",
        }

    def suspend_capsule(self, capsule_id: str) -> Capsule:
        capsule = self.get_capsule(capsule_id)
        for pid in self._pid_set(capsule):
            suspend(pid)
        capsule.state = "suspended"
        capsule.log("capsule_suspended", {"pids": sorted(self._pid_set(capsule))})
        self._persist(capsule)
        return capsule

    def resume_capsule(self, capsule_id: str) -> Capsule:
        capsule = self.get_capsule(capsule_id)
        if capsule.state in {"budget_suspended", "invariant_suspended"}:
            capsule.auto_suspend_on_budget = False
            capsule.log(
                "automatic_suspension_overridden",
                {"debt": capsule.reversibility_debt, "invariant": capsule.invariant.state},
            )
        for pid in self._pid_set(capsule):
            resume(pid)
        capsule.state = "running"
        capsule.log("capsule_resumed", {"pids": sorted(self._pid_set(capsule))})
        self._persist(capsule)
        return capsule

    def terminate_capsule(self, capsule_id: str) -> Capsule:
        capsule = self.get_capsule(capsule_id)
        for pid in sorted(self._pid_set(capsule), reverse=True):
            terminate(pid)
        capsule.state = "terminated"
        capsule.ended_at = utc_now()
        capsule.log("capsule_terminated", {})
        self._persist(capsule)
        return capsule

    def commit(self, capsule_id: str) -> Capsule:
        with self.lock:
            return self._commit_locked(capsule_id)

    def _commit_locked(self, capsule_id: str) -> Capsule:
        capsule = self.get_capsule(capsule_id)
        if capsule.state in {"running", "suspended", "budget_suspended", "invariant_suspended"}:
            self.terminate_capsule(capsule_id)
            capsule = self.get_capsule(capsule_id)
        capsule.file_changes = self.file_journals[capsule_id].detect_changes()
        capsule.registry_changes = self.registry_journals[capsule_id].detect_changes()
        ledger = self.recovery_ledgers[capsule_id]
        ledger.detect_conflicts()
        self._sync_recovery(capsule, ledger)
        if any(item.state == "open" for item in capsule.conflicts):
            raise ValueError("commit blocked by unresolved resource conflicts")
        ledger.mark_committed()
        capsule.state = "committed"
        capsule.committed_at = utc_now()
        capsule.log("changes_committed", {"files": len(capsule.file_changes), "registry": len(capsule.registry_changes)})
        capsule.recovery_certificate = ledger.certificate(
            "committed",
            {"commit_barrier": True, "resource_versions_revalidated": True},
        )
        self._sync_recovery(capsule, ledger)
        self._finalize_baseline(capsule, "committed")
        self._persist(capsule)
        return capsule

    def selective_commit(self, capsule_id: str, payload: dict[str, Any]) -> Capsule:
        with self.lock:
            return self._selective_commit_locked(capsule_id, payload)

    def _selective_commit_locked(self, capsule_id: str, payload: dict[str, Any]) -> Capsule:
        capsule = self.get_capsule(capsule_id)
        if capsule.state in {"running", "suspended", "budget_suspended", "invariant_suspended"}:
            self.terminate_capsule(capsule_id)
            capsule = self.get_capsule(capsule_id)

        requested_files = {str(Path(path).resolve()) for path in payload.get("keep_files", [])}
        requested_registry = {
            (item["hive_path"], item["value_name"])
            for item in payload.get("keep_registry", [])
        }
        keep_files, inferred_files = self._dependency_closure(
            capsule,
            requested_files,
            requested_registry,
        )
        ledger = self.recovery_ledgers[capsule_id]
        ledger.detect_conflicts()
        self._sync_recovery(capsule, ledger)
        if any(item.state == "open" for item in capsule.conflicts):
            raise ValueError("selective commit blocked by unresolved resource conflicts")
        preserved_conflicts = {
            item.resource_id
            for item in capsule.conflicts
            if item.resolution == "preserve_host"
        }
        preservation_set = keep_files | preserved_conflicts
        file_result = self.file_journals[capsule_id].rollback(keep_paths=preservation_set)
        registry_result = self.registry_journals[capsule_id].rollback(keep_values=requested_registry)
        verification: dict[str, bool] = {}
        for obligation in ledger.obligations:
            if obligation.resource_type != "file" or obligation.resource_id in preservation_set:
                continue
            verification[obligation.resource_id] = versions_equal(
                obligation.pre_version,
                resource_version(Path(obligation.resource_id)).to_dict(),
            )
        ledger.mark_selectively_finalized(preservation_set, verification)
        capsule.state = "selectively_committed"
        capsule.selectively_committed_at = utc_now()
        capsule.log(
            "changes_selectively_committed",
            {
                "requested_files": sorted(requested_files),
                "inferred_dependency_files": sorted(inferred_files),
                "kept_registry": sorted([list(item) for item in requested_registry]),
                "file_result": file_result,
                "registry_result": registry_result,
            },
        )
        capsule.recovery_certificate = ledger.certificate(
            "selectively_committed",
            {
                "files": file_result,
                "registry": registry_result,
                "resource_verification": verification,
                "dependency_closed_preservation_set": sorted(preservation_set),
            },
        )
        self._sync_recovery(capsule, ledger)
        self._finalize_baseline(capsule, "selectively_committed")
        self._persist(capsule)
        return capsule

    def rollback(self, capsule_id: str) -> Capsule:
        with self.lock:
            return self._rollback_locked(capsule_id)

    def _rollback_locked(self, capsule_id: str) -> Capsule:
        capsule = self.get_capsule(capsule_id)
        if capsule.state in {"running", "suspended", "budget_suspended", "invariant_suspended"}:
            self.terminate_capsule(capsule_id)
            capsule = self.get_capsule(capsule_id)
        ledger = self.recovery_ledgers[capsule_id]
        ledger.detect_conflicts()
        self._sync_recovery(capsule, ledger)
        conflict_paths = {
            item.resource_id
            for item in capsule.conflicts
            if item.state == "open" or item.resolution == "preserve_host"
        }
        file_result = self.file_journals[capsule_id].rollback(keep_paths=conflict_paths)
        registry_result = self.registry_journals[capsule_id].rollback()
        capsule.file_changes = self.file_journals[capsule_id].changes
        capsule.registry_changes = self.registry_journals[capsule_id].changes
        capsule.state = "rolled_back"
        capsule.rolled_back_at = utc_now()
        capsule.log("changes_rolled_back", {"file_result": file_result, "registry_result": registry_result})
        verification: dict[str, bool] = {}
        for obligation in ledger.obligations:
            if obligation.resource_type != "file" or obligation.resource_id in conflict_paths:
                continue
            verification[obligation.resource_id] = versions_equal(
                obligation.pre_version,
                resource_version(Path(obligation.resource_id)).to_dict(),
            )
        ledger.mark_reversed(verification)
        capsule.recovery_certificate = ledger.certificate(
            "rolled_back",
            {
                "files": file_result,
                "registry": registry_result,
                "resource_verification": verification,
                "preserved_conflicts": sorted(conflict_paths),
            },
        )
        self._sync_recovery(capsule, ledger)
        self._finalize_baseline(capsule, "rolled_back")
        self._persist(capsule)
        return capsule

    def report(self, capsule_id: str) -> dict[str, Any]:
        capsule = self.get_capsule(capsule_id)
        report = capsule.to_dict()
        report["os_syllabus_mapping"] = {
            "processes": "PCB-like records and process tree tracking",
            "scheduling": "priority, suspension, runtime limits, memory limits",
            "memory": "working set and growth tracking",
            "file_system": "snapshot journal and rollback",
            "protection": "explicit commit/rollback boundary",
            "deadlock": "no-progress and hang signals",
            "reversibility_scheduling": "rollback debt is treated as a live scheduling constraint",
            "recovery_obligations": "persistent effects are paired with durable, verifiable inverse material",
            "recovery_invariant": "execution is suspended when verified recovery coverage cannot be maintained",
            "selective_commit": "unselected deltas are reversed while a dependency-closed preservation set is retained",
        }
        return report

    def metrics(self) -> dict[str, Any]:
        return system_metrics()

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(1)
            with self.lock:
                ids = list(self.capsules)
            for capsule_id in ids:
                try:
                    with self.lock:
                        self._refresh_capsule(capsule_id)
                except Exception:
                    continue

    def _refresh_capsule(self, capsule_id: str) -> None:
        capsule = self.capsules[capsule_id]
        if capsule.state in {"committed", "selectively_committed", "rolled_back"}:
            return

        proc = self.processes.get(capsule_id)
        process_map = list_processes()
        pid_set = descendants(capsule.root_pid, process_map) if capsule.root_pid else set()
        now = time.time()
        total_memory = 0
        total_write_ops = 0

        for pid in pid_set:
            info = process_map.get(pid)
            if not info:
                continue
            total_memory += info.memory_bytes
            total_write_ops += info.write_ops
            record = capsule.processes.get(pid)
            if not record:
                record = ProcessRecord(pid=pid, parent_pid=info.parent_pid, name=info.name, command_line=info.command_line)
                capsule.processes[pid] = record
            record.parent_pid = info.parent_pid
            record.name = info.name
            record.command_line = info.command_line
            record.memory_bytes = info.memory_bytes
            record.cpu_time_ms = int((info.kernel_time + info.user_time) / 10000)
            record.read_ops = info.read_ops
            record.write_ops = info.write_ops

        if proc and proc.poll() is not None and capsule.state in {"running", "suspended"}:
            capsule.state = "exited"
            capsule.ended_at = utc_now()
            capsule.log("capsule_exited", {"exit_code": proc.returncode})
            for record in capsule.processes.values():
                record.state = "exited"
                record.exit_code = proc.returncode
                record.exited_at = utc_now()

        capsule.file_changes = self.file_journals[capsule_id].detect_changes()
        capsule.registry_changes = self.registry_journals[capsule_id].detect_changes()
        ledger = self.recovery_ledgers[capsule_id]
        ledger.detect_conflicts()
        self._sync_recovery(capsule, ledger)
        self._score_risk(capsule, total_memory, total_write_ops)
        self._compute_reversibility_debt(capsule, total_write_ops)
        self._update_behavior_baseline(
            capsule,
            total_memory,
            total_write_ops,
            now,
        )

        if not capsule.invariant.satisfied and capsule.state == "running":
            self._suspend_for_invariant(capsule, "; ".join(capsule.invariant.reasons))

        if capsule.runtime_limit_seconds and _elapsed_seconds(capsule.started_at, now) > capsule.runtime_limit_seconds and capsule.state == "running":
            capsule.log("runtime_limit_exceeded", {"limit_seconds": capsule.runtime_limit_seconds})
            self.terminate_capsule(capsule_id)
            return
        if capsule.memory_limit_mb and total_memory > capsule.memory_limit_mb * 1024 * 1024 and capsule.state == "running":
            capsule.log("memory_limit_exceeded", {"limit_mb": capsule.memory_limit_mb, "observed_bytes": total_memory})
            for pid in pid_set:
                set_priority(pid, "idle")

        self._persist(capsule)

    def _score_risk(self, capsule: Capsule, total_memory: int, total_write_ops: int) -> None:
        components = {"files": 0, "persistence": 0, "process": 0, "memory": 0, "io": 0}
        reasons: list[str] = []
        created = len([c for c in capsule.file_changes if c.change_type == "created"])
        modified = len([c for c in capsule.file_changes if c.change_type == "modified"])
        registry = len(capsule.registry_changes)
        if created + modified >= 3:
            components["files"] = 25
            reasons.append("many file writes")
        if registry:
            components["persistence"] = 25
            reasons.append("startup registry changes")
        if len(capsule.processes) > 3:
            components["process"] = 15
            reasons.append("multi-process activity")
        if capsule.memory_limit_mb and total_memory > capsule.memory_limit_mb * 1024 * 1024:
            components["memory"] = 20
            reasons.append("memory budget pressure")
        if total_write_ops > 1000:
            components["io"] = 15
            reasons.append("high disk write activity")
        capsule.risk_components = components
        capsule.risk_score = min(sum(components.values()), 100)
        capsule.risk_reasons = reasons

    def _compute_reversibility_debt(self, capsule: Capsule, total_write_ops: int) -> None:
        namespace_cost = min(20, len(capsule.file_changes) * 3)
        registry_cost = min(20, len(capsule.registry_changes) * 10)
        fanout_cost = min(15, max(0, len(capsule.processes) - 1) * 3)
        io_cost = min(15, total_write_ops // 250)
        backup_bytes = 0
        unprotected = 0
        for change in capsule.file_changes:
            if change.change_type in {"modified", "deleted"}:
                if change.backup_path:
                    backup_bytes += change.size_before or 0
                else:
                    unprotected += 1
        journal_cost = min(15, backup_bytes // (1024 * 1024))
        unprotected_cost = min(40, unprotected * 20)
        debt = min(100, namespace_cost + registry_cost + fanout_cost + io_cost + journal_cost + unprotected_cost)

        reasons: list[str] = []
        if namespace_cost:
            reasons.append(f"{len(capsule.file_changes)} filesystem deltas")
        if registry_cost:
            reasons.append(f"{len(capsule.registry_changes)} persistence deltas")
        if fanout_cost:
            reasons.append(f"{len(capsule.processes)} processes in recovery scope")
        if journal_cost:
            reasons.append(f"{backup_bytes} journal bytes")
        if unprotected:
            reasons.append(f"{unprotected} changes lack restorable backups")
        if io_cost:
            reasons.append("write pressure is increasing recovery cost")

        capsule.reversibility_debt = debt
        capsule.reversibility_reasons = reasons
        if debt >= capsule.reversibility_budget:
            capsule.reversibility_state = "budget_exceeded"
        elif debt >= int(capsule.reversibility_budget * 0.75):
            capsule.reversibility_state = "near_budget"
        else:
            capsule.reversibility_state = "within_budget"

    def _update_behavior_baseline(
        self,
        capsule: Capsule,
        total_memory: int,
        total_write_ops: int,
        now: float,
    ) -> None:
        metrics = capsule.behavior_metrics
        metrics["peak_memory_bytes"] = max(
            metrics.get("peak_memory_bytes", 0), total_memory
        )
        metrics["peak_process_count"] = max(
            metrics.get("peak_process_count", 0), len(capsule.processes)
        )
        metrics["peak_write_ops"] = max(
            metrics.get("peak_write_ops", 0), total_write_ops
        )
        metrics["peak_file_changes"] = max(
            metrics.get("peak_file_changes", 0), len(capsule.file_changes)
        )
        metrics["peak_obligations"] = max(
            metrics.get("peak_obligations", 0), len(capsule.obligations)
        )
        metrics["peak_journal_bytes"] = max(
            metrics.get("peak_journal_bytes", 0), capsule.invariant.reserved_bytes
        )
        metrics["runtime_ms"] = max(
            metrics.get("runtime_ms", 0),
            _elapsed_seconds(capsule.started_at, now) * 1000,
        )
        seal_latencies = [
            _duration_ms(item.created_at, item.sealed_at)
            for item in capsule.obligations
            if item.sealed_at
        ]
        if seal_latencies:
            metrics["max_seal_latency_ms"] = max(
                metrics.get("max_seal_latency_ms", 0),
                max(seal_latencies),
            )

        if capsule.baseline_mode != "adaptive":
            return
        previous_state = capsule.baseline_assessment.get("state")
        capsule.baseline_assessment = self.baselines.assess(
            capsule.baseline_profile_id,
            metrics,
        )
        assessment = capsule.baseline_assessment
        if (
            assessment.get("anomalous")
            and previous_state != "anomalous"
            and capsule.state == "running"
        ):
            severe = any(
                float(item.get("severity_ratio") or 0) >= 1.5
                for item in assessment.get("anomalies", [])
            )
            if severe or len(assessment.get("anomalies", [])) >= 2:
                for pid in self._pid_set(capsule):
                    set_priority(pid, "idle")
                capsule.log(
                    "adaptive_baseline_intervention",
                    {
                        "action": "priority_lowered",
                        "anomalies": assessment.get("anomalies", []),
                    },
                )

    def _finalize_baseline(self, capsule: Capsule, outcome: str) -> None:
        if capsule.baseline_mode != "adaptive":
            return
        certificate = capsule.recovery_certificate or {}
        eligible = bool(
            capsule.update_baseline
            and certificate.get("verified")
            and certificate.get("recovery_complete")
            and not certificate.get("unresolved_effects")
        )
        profile = self.baselines.observe(
            capsule.baseline_profile_id,
            capsule.behavior_metrics,
            eligible,
            {
                "capsule_id": capsule.capsule_id,
                "outcome": outcome,
                "certificate_id": certificate.get("certificate_id"),
            },
        )
        capsule.baseline_assessment = self.baselines.assess(
            capsule.baseline_profile_id,
            capsule.behavior_metrics,
        )
        capsule.log(
            "adaptive_baseline_finalized",
            {
                "eligible": eligible,
                "sample_count": len(profile.get("samples", [])),
                "profile_state": profile.get("state"),
            },
        )

    def _dependency_closure(
        self,
        capsule: Capsule,
        requested_files: set[str],
        requested_registry: set[tuple[str, str]],
    ) -> tuple[set[str], set[str]]:
        keep_files = set(requested_files)
        inferred: set[str] = set()
        selected_registry = {
            (hive.casefold(), name.casefold())
            for hive, name in requested_registry
        }
        changed_paths = {change.path for change in capsule.file_changes}
        for change in capsule.registry_changes:
            key = (change.hive_path.casefold(), change.value_name.casefold())
            if key not in selected_registry or not change.after:
                continue
            after = change.after.casefold()
            for path in changed_paths:
                if path.casefold() in after and path not in keep_files:
                    keep_files.add(path)
                    inferred.add(path)
        return keep_files, inferred

    def _pid_set(self, capsule: Capsule) -> set[int]:
        process_map = list_processes()
        if capsule.root_pid:
            return descendants(capsule.root_pid, process_map)
        return set(capsule.processes)

    def _persist(self, capsule: Capsule) -> None:
        path = self.data_dir / capsule.capsule_id / "capsule.json"
        path.write_text(json.dumps(capsule.to_dict(), indent=2), encoding="utf-8")

    def _summary(self, capsule: Capsule) -> dict[str, Any]:
        return {
            "capsule_id": capsule.capsule_id,
            "command": capsule.command,
            "state": capsule.state,
            "risk_score": capsule.risk_score,
            "risk_reasons": capsule.risk_reasons,
            "risk_components": capsule.risk_components,
            "reversibility_budget": capsule.reversibility_budget,
            "reversibility_debt": capsule.reversibility_debt,
            "reversibility_state": capsule.reversibility_state,
            "reversibility_reasons": capsule.reversibility_reasons,
            "started_at": capsule.started_at,
            "ended_at": capsule.ended_at,
            "root_pid": capsule.root_pid,
            "process_count": len(capsule.processes),
            "file_change_count": len(capsule.file_changes),
            "registry_change_count": len(capsule.registry_changes),
            "obligation_count": len(capsule.obligations),
            "invariant_state": capsule.invariant.state,
            "unresolved_conflicts": capsule.invariant.unresolved_conflicts,
            "edge_mode": capsule.recovery_contract.edge_policy.enabled,
            "edge_admission_decision": capsule.edge_capacity.get("decision"),
            "baseline_mode": capsule.baseline_mode,
            "baseline_profile_id": capsule.baseline_profile_id,
            "baseline_state": capsule.baseline_assessment.get("state"),
        }

    def _sync_recovery(self, capsule: Capsule, ledger: RecoveryLedger) -> None:
        capsule.obligations = list(ledger.obligations)
        capsule.conflicts = list(ledger.conflicts)
        capsule.invariant = ledger.invariant()
        covered_paths = {
            str(Path(item.resource_id).resolve()).casefold()
            for item in capsule.obligations
            if item.resource_type == "file" and item.state in {"sealed", "exposed", "reversing", "verified"}
        }
        uncovered_files = [
            change.path
            for change in capsule.file_changes
            if str(Path(change.path).resolve()).casefold() not in covered_paths
        ]
        if uncovered_files:
            capsule.invariant.satisfied = False
            capsule.invariant.state = "violated"
            capsule.invariant.reasons.append(
                f"{len(uncovered_files)} filesystem effects have no sealed recovery obligation"
            )
        if capsule.registry_changes:
            capsule.invariant.satisfied = False
            capsule.invariant.state = "violated"
            capsule.invariant.reasons.append(
                f"{len(capsule.registry_changes)} registry effects are monitored but not pre-admitted"
            )

    def _suspend_for_invariant(self, capsule: Capsule, reason: str) -> None:
        pids = sorted(self._pid_set(capsule))
        for pid in pids:
            suspend(pid)
        capsule.state = "invariant_suspended"
        capsule.log("recovery_invariant_violated", {"reason": reason, "pids": pids})


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _elapsed_seconds(started_at: str, now: float) -> float:
    from datetime import datetime

    dt = datetime.fromisoformat(started_at)
    return now - dt.timestamp()


def _duration_ms(started_at: str, ended_at: str | None) -> float:
    if not ended_at:
        return 0
    from datetime import datetime

    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(ended_at)
    return max(0.0, (end - start).total_seconds() * 1000)
