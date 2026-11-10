from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProcessRecord:
    pid: int
    parent_pid: int | None
    name: str
    command_line: str
    state: str = "running"
    cpu_time_ms: int = 0
    memory_bytes: int = 0
    read_ops: int = 0
    write_ops: int = 0
    started_at: str = field(default_factory=utc_now)
    exited_at: str | None = None
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FileChange:
    path: str
    change_type: str
    size_before: int | None = None
    size_after: int | None = None
    hash_before: str | None = None
    hash_after: str | None = None
    backup_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RegistryChange:
    hive_path: str
    value_name: str
    change_type: str
    before: str | None = None
    after: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class EdgeRecoveryPolicy:
    enabled: bool = False
    device_class: str = "windows_x86_edge"
    recovery_reserve_bytes: int = 32 * 1024 * 1024
    minimum_free_storage_bytes: int = 256 * 1024 * 1024
    minimum_free_memory_bytes: int = 128 * 1024 * 1024
    minimum_battery_percent: int = 10
    max_rollback_time_ms: int = 5000
    estimated_restore_throughput_bps: int = 25 * 1024 * 1024
    flash_write_budget_bytes: int = 256 * 1024 * 1024

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EdgeRecoveryPolicy":
        values = data or {}
        return cls(
            enabled=bool(values.get("enabled", False)),
            device_class=str(values.get("device_class") or "windows_x86_edge"),
            recovery_reserve_bytes=int(
                values.get("recovery_reserve_bytes") or 32 * 1024 * 1024
            ),
            minimum_free_storage_bytes=int(
                values.get("minimum_free_storage_bytes") or 256 * 1024 * 1024
            ),
            minimum_free_memory_bytes=int(
                values.get("minimum_free_memory_bytes") or 128 * 1024 * 1024
            ),
            minimum_battery_percent=int(values.get("minimum_battery_percent", 10)),
            max_rollback_time_ms=int(values.get("max_rollback_time_ms") or 5000),
            estimated_restore_throughput_bps=int(
                values.get("estimated_restore_throughput_bps") or 25 * 1024 * 1024
            ),
            flash_write_budget_bytes=int(
                values.get("flash_write_budget_bytes") or 256 * 1024 * 1024
            ),
        )


@dataclass
class RecoveryContract:
    protected_paths: list[str] = field(default_factory=list)
    registry_locations: list[str] = field(default_factory=list)
    journal_limit_bytes: int = 64 * 1024 * 1024
    max_pending_obligations: int = 64
    admission_timeout_ms: int = 3000
    network_policy: str = "block"
    failure_policy: str = "fail_closed"
    enforcement_mode: str = "cooperative"
    edge_policy: EdgeRecoveryPolicy = field(default_factory=EdgeRecoveryPolicy)

    def to_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["edge_policy"] = self.edge_policy.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RecoveryContract":
        values = data or {}
        return cls(
            protected_paths=list(values.get("protected_paths") or []),
            registry_locations=list(values.get("registry_locations") or []),
            journal_limit_bytes=int(values.get("journal_limit_bytes") or 64 * 1024 * 1024),
            max_pending_obligations=int(values.get("max_pending_obligations") or 64),
            admission_timeout_ms=int(values.get("admission_timeout_ms") or 3000),
            network_policy=str(values.get("network_policy") or "block"),
            failure_policy=str(values.get("failure_policy") or "fail_closed"),
            enforcement_mode=str(values.get("enforcement_mode") or "cooperative"),
            edge_policy=EdgeRecoveryPolicy.from_dict(values.get("edge_policy")),
        )


@dataclass
class ResourceVersion:
    exists: bool
    size: int | None = None
    digest: str | None = None
    modified_ns: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RecoveryObligation:
    obligation_id: str
    sequence: int
    capsule_id: str
    resource_type: str
    resource_id: str
    operation: str
    state: str
    process_id: int | None = None
    thread_id: int | None = None
    parent_obligation_id: str | None = None
    dependencies: list[str] = field(default_factory=list)
    inverse_action: str = ""
    backup_path: str | None = None
    reserved_bytes: int = 0
    pre_version: dict[str, Any] = field(default_factory=dict)
    post_version: dict[str, Any] = field(default_factory=dict)
    verification_predicate: str = ""
    admission_decision: str = "standard"
    estimated_rollback_ms: int = 0
    capacity_snapshot: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    sealed_at: str | None = None
    exposed_at: str | None = None
    verified_at: str | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecoveryObligation":
        return cls(**data)


@dataclass
class ConflictRecord:
    conflict_id: str
    obligation_id: str
    resource_id: str
    expected_version: dict[str, Any]
    observed_version: dict[str, Any]
    state: str = "open"
    resolution: str | None = None
    detected_at: str = field(default_factory=utc_now)
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConflictRecord":
        return cls(**data)


@dataclass
class InvariantStatus:
    satisfied: bool = True
    state: str = "satisfied"
    covered_effects: int = 0
    exposed_effects: int = 0
    pending_obligations: int = 0
    unresolved_conflicts: int = 0
    reserved_bytes: int = 0
    journal_limit_bytes: int = 0
    reasons: list[str] = field(default_factory=list)
    checked_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Capsule:
    capsule_id: str
    command: list[str]
    cwd: str
    watch_paths: list[str]
    state: str = "running"
    risk_score: int = 0
    risk_reasons: list[str] = field(default_factory=list)
    risk_components: dict[str, int] = field(default_factory=dict)
    reversibility_budget: int = 70
    reversibility_debt: int = 0
    reversibility_state: str = "within_budget"
    reversibility_reasons: list[str] = field(default_factory=list)
    auto_suspend_on_budget: bool = True
    started_at: str = field(default_factory=utc_now)
    ended_at: str | None = None
    committed_at: str | None = None
    selectively_committed_at: str | None = None
    rolled_back_at: str | None = None
    root_pid: int | None = None
    runtime_limit_seconds: int | None = None
    memory_limit_mb: int | None = None
    priority: str = "normal"
    recovery_contract: RecoveryContract = field(default_factory=RecoveryContract)
    invariant: InvariantStatus = field(default_factory=InvariantStatus)
    obligations: list[RecoveryObligation] = field(default_factory=list)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    recovery_certificate: dict[str, Any] | None = None
    edge_capacity: dict[str, Any] = field(default_factory=dict)
    admission_history: list[dict[str, Any]] = field(default_factory=list)
    baseline_mode: str = "static"
    baseline_profile_id: str = ""
    update_baseline: bool = True
    baseline_assessment: dict[str, Any] = field(default_factory=dict)
    behavior_metrics: dict[str, float] = field(default_factory=dict)
    processes: dict[int, ProcessRecord] = field(default_factory=dict)
    file_changes: list[FileChange] = field(default_factory=list)
    registry_changes: list[RegistryChange] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def log(self, event: str, detail: dict[str, Any] | None = None) -> None:
        self.audit.append({"time": utc_now(), "event": event, "detail": detail or {}})

    def to_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["processes"] = [p.to_dict() for p in self.processes.values()]
        data["file_changes"] = [c.to_dict() for c in self.file_changes]
        data["registry_changes"] = [c.to_dict() for c in self.registry_changes]
        data["recovery_contract"] = self.recovery_contract.to_dict()
        data["invariant"] = self.invariant.to_dict()
        data["obligations"] = [item.to_dict() for item in self.obligations]
        data["conflicts"] = [item.to_dict() for item in self.conflicts]
        return data
