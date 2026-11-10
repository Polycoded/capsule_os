from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import uuid
from pathlib import Path
from typing import Any

from .models import (
    ConflictRecord,
    InvariantStatus,
    RecoveryContract,
    RecoveryObligation,
    ResourceVersion,
    utc_now,
)


FINAL_STATES = {"verified", "conflicted", "expired", "unrecoverable", "tampered"}
ADMITTED_STATES = {"sealed", "exposed", "reversing", "verified"}


class RecoveryLedger:
    """Durable, hash-chained recovery obligations for one capsule."""

    def __init__(self, root: Path, capsule_id: str, contract: RecoveryContract):
        self.root = root
        self.capsule_id = capsule_id
        self.contract = contract
        self.ledger_path = root / "obligations.jsonl"
        self.backup_dir = root / "obligation_backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.obligations: list[RecoveryObligation] = []
        self.conflicts: list[ConflictRecord] = []
        self.integrity_ok = True
        self._last_hash = "0" * 64
        self._sequence = 0
        self._load()

    def prepare_file(
        self,
        path_value: str,
        operation: str,
        process_id: int | None = None,
        thread_id: int | None = None,
        dependencies: list[str] | None = None,
        admission: dict[str, Any] | None = None,
    ) -> RecoveryObligation:
        path = Path(path_value).resolve()
        if not self._is_protected(path):
            return self._failed_obligation(
                path,
                operation,
                "unrecoverable",
                "resource is outside the recovery contract",
                process_id,
                thread_id,
            )

        if admission and admission.get("decision") != "admit":
            obligation = self._failed_obligation(
                path,
                operation,
                "unrecoverable",
                "; ".join(admission.get("reasons") or ["edge capacity admission denied"]),
                process_id,
                thread_id,
            )
            obligation.admission_decision = "denied"
            obligation.estimated_rollback_ms = int(
                admission.get("estimated_rollback_ms") or 0
            )
            obligation.capacity_snapshot = dict(admission)
            self._append("obligation_edge_denied", obligation.to_dict())
            return obligation

        pending = sum(1 for item in self.obligations if item.state in {"preparing", "sealed"})
        if pending >= self.contract.max_pending_obligations:
            return self._failed_obligation(
                path,
                operation,
                "expired",
                "maximum pending obligations reached",
                process_id,
                thread_id,
            )

        pre = resource_version(path)
        required = pre.size or 0
        if self.reserved_bytes + required > self.contract.journal_limit_bytes:
            return self._failed_obligation(
                path,
                operation,
                "unrecoverable",
                "recovery journal capacity would be exceeded",
                process_id,
                thread_id,
            )

        obligation = RecoveryObligation(
            obligation_id=uuid.uuid4().hex,
            sequence=self._next_sequence(),
            capsule_id=self.capsule_id,
            resource_type="file",
            resource_id=str(path),
            operation=operation,
            state="preparing",
            process_id=process_id,
            thread_id=thread_id,
            dependencies=list(dependencies or []),
            inverse_action=_inverse_action(operation, pre.exists),
            reserved_bytes=required,
            pre_version=pre.to_dict(),
            verification_predicate="resource matches captured pre-operation version",
            admission_decision=(
                "edge_admitted" if admission and admission.get("enabled") else "standard"
            ),
            estimated_rollback_ms=int(
                (admission or {}).get("estimated_rollback_ms") or 0
            ),
            capacity_snapshot=dict(admission or {}),
        )
        self.obligations.append(obligation)
        self._append("obligation_preparing", obligation.to_dict())

        try:
            if pre.exists:
                backup = self.backup_dir / f"{obligation.sequence:08d}_{obligation.obligation_id}.bin"
                shutil.copy2(path, backup)
                obligation.backup_path = str(backup)
                if resource_version(backup).digest != pre.digest:
                    raise OSError("recovery copy digest mismatch")
            obligation.state = "sealed"
            obligation.sealed_at = utc_now()
            self._append("obligation_sealed", obligation.to_dict())
        except OSError as exc:
            obligation.state = "unrecoverable"
            obligation.failure_reason = str(exc)
            self._append("obligation_unrecoverable", obligation.to_dict())
        return obligation

    def expose(self, obligation_id: str) -> RecoveryObligation:
        obligation = self.get(obligation_id)
        if obligation.state != "sealed":
            raise ValueError("only a sealed obligation can be exposed")
        obligation.post_version = resource_version(Path(obligation.resource_id)).to_dict()
        obligation.state = "exposed"
        obligation.exposed_at = utc_now()
        self._append("obligation_exposed", obligation.to_dict())
        return obligation

    def get(self, obligation_id: str) -> RecoveryObligation:
        for obligation in self.obligations:
            if obligation.obligation_id == obligation_id:
                return obligation
        raise KeyError(obligation_id)

    def invariant(self) -> InvariantStatus:
        pending = [item for item in self.obligations if item.state in {"preparing", "sealed"}]
        failures = [item for item in self.obligations if item.state in {"expired", "unrecoverable", "tampered"}]
        open_conflicts = [item for item in self.conflicts if item.state == "open"]
        reasons: list[str] = []
        if failures:
            reasons.append(f"{len(failures)} recovery obligations are not enforceable")
        if open_conflicts:
            reasons.append(f"{len(open_conflicts)} resource-version conflicts require resolution")
        if self.reserved_bytes > self.contract.journal_limit_bytes:
            reasons.append("reserved recovery storage exceeds the contract limit")
        if not self.integrity_ok:
            reasons.append("recovery obligation ledger integrity verification failed")
        satisfied = not reasons
        return InvariantStatus(
            satisfied=satisfied,
            state="satisfied" if satisfied else "violated",
            covered_effects=sum(1 for item in self.obligations if item.state in ADMITTED_STATES),
            exposed_effects=sum(1 for item in self.obligations if item.exposed_at is not None),
            pending_obligations=len(pending),
            unresolved_conflicts=len(open_conflicts),
            reserved_bytes=self.reserved_bytes,
            journal_limit_bytes=self.contract.journal_limit_bytes,
            reasons=reasons,
        )

    def detect_conflicts(self) -> list[ConflictRecord]:
        existing = {item.obligation_id for item in self.conflicts if item.state == "open"}
        for obligation in self.obligations:
            if obligation.state != "exposed" or obligation.obligation_id in existing:
                continue
            expected = obligation.post_version
            observed = resource_version(Path(obligation.resource_id)).to_dict()
            if expected and not versions_equal(expected, observed):
                conflict = ConflictRecord(
                    conflict_id=uuid.uuid4().hex,
                    obligation_id=obligation.obligation_id,
                    resource_id=obligation.resource_id,
                    expected_version=expected,
                    observed_version=observed,
                )
                obligation.state = "conflicted"
                self.conflicts.append(conflict)
                self._append("conflict_detected", conflict.to_dict())
        return self.conflicts

    def resolve_conflict(self, conflict_id: str, resolution: str) -> ConflictRecord:
        if resolution not in {"preserve_host", "accept_capsule", "abort"}:
            raise ValueError("unsupported conflict resolution")
        conflict = next((item for item in self.conflicts if item.conflict_id == conflict_id), None)
        if not conflict:
            raise KeyError(conflict_id)
        obligation = self.get(conflict.obligation_id)
        conflict.state = "resolved"
        conflict.resolution = resolution
        conflict.resolved_at = utc_now()
        if resolution == "accept_capsule":
            obligation.post_version = dict(conflict.observed_version)
            obligation.state = "exposed"
            self._append("obligation_exposed", obligation.to_dict())
        elif resolution == "abort":
            obligation.state = "unrecoverable"
            obligation.failure_reason = "capsule aborted after resource-version conflict"
            self._append("obligation_unrecoverable", obligation.to_dict())
        self._append("conflict_resolved", conflict.to_dict())
        return conflict

    def mark_reversed(self, verification: dict[str, bool]) -> None:
        for obligation in reversed(self.obligations):
            if obligation.state not in {"exposed", "sealed", "reversing"}:
                continue
            obligation.state = "verified" if verification.get(obligation.resource_id, False) else "unrecoverable"
            obligation.verified_at = utc_now()
            if obligation.state == "unrecoverable":
                obligation.failure_reason = "post-rollback verification failed"
            self._append("obligation_verified", obligation.to_dict())

    def mark_committed(self) -> None:
        for obligation in self.obligations:
            if obligation.state in {"sealed", "exposed"}:
                obligation.state = "verified"
                obligation.verified_at = utc_now()
                self._append("obligation_committed", obligation.to_dict())

    def mark_selectively_finalized(
        self,
        kept_resources: set[str],
        verification: dict[str, bool],
    ) -> None:
        for obligation in reversed(self.obligations):
            if obligation.state not in {"sealed", "exposed", "reversing"}:
                continue
            if obligation.resource_id in kept_resources:
                obligation.state = "verified"
                obligation.verified_at = utc_now()
                self._append("obligation_committed", obligation.to_dict())
                continue
            obligation.state = (
                "verified"
                if verification.get(obligation.resource_id, False)
                else "unrecoverable"
            )
            obligation.verified_at = utc_now()
            if obligation.state == "unrecoverable":
                obligation.failure_reason = "selective rollback verification failed"
            self._append("obligation_verified", obligation.to_dict())

    def certificate(self, outcome: str, verification: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "certificate_id": uuid.uuid4().hex,
            "capsule_id": self.capsule_id,
            "outcome": outcome,
            "issued_at": utc_now(),
            "ledger_head": self._last_hash,
            "ledger_integrity_verified": self.integrity_ok,
            "obligations_total": len(self.obligations),
            "obligations_verified": sum(1 for item in self.obligations if item.state == "verified"),
            "unresolved_conflicts": sum(1 for item in self.conflicts if item.state == "open"),
            "unresolved_effects": [
                item.to_dict()
                for item in self.obligations
                if item.state in {"conflicted", "unrecoverable", "expired", "tampered"}
            ],
            "verification": verification,
            "contract": self.contract.to_dict(),
        }
        payload["recovery_complete"] = (
            payload["obligations_verified"] == payload["obligations_total"]
            and payload["unresolved_conflicts"] == 0
            and not payload["unresolved_effects"]
            and payload["ledger_integrity_verified"]
        )
        key = _certificate_key(self.root.parent)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["signature_algorithm"] = "HMAC-SHA256"
        payload["signature"] = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        payload["verified"] = verify_certificate(payload, key)
        (self.root / "recovery_certificate.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._append("certificate_issued", {"certificate_id": payload["certificate_id"], "outcome": outcome})
        return payload

    @property
    def reserved_bytes(self) -> int:
        return sum(item.reserved_bytes for item in self.obligations if item.state not in {"expired", "unrecoverable"})

    def _failed_obligation(
        self,
        path: Path,
        operation: str,
        state: str,
        reason: str,
        process_id: int | None,
        thread_id: int | None,
    ) -> RecoveryObligation:
        obligation = RecoveryObligation(
            obligation_id=uuid.uuid4().hex,
            sequence=self._next_sequence(),
            capsule_id=self.capsule_id,
            resource_type="file",
            resource_id=str(path),
            operation=operation,
            state=state,
            process_id=process_id,
            thread_id=thread_id,
            failure_reason=reason,
        )
        self.obligations.append(obligation)
        self._append(f"obligation_{state}", obligation.to_dict())
        return obligation

    def _is_protected(self, path: Path) -> bool:
        for root_value in self.contract.protected_paths:
            root = Path(root_value).resolve()
            try:
                path.relative_to(root)
                return True
            except ValueError:
                if path == root:
                    return True
        return False

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _append(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "sequence": self._next_sequence(),
            "time": utc_now(),
            "event": event,
            "payload": payload,
            "previous_hash": self._last_hash,
        }
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        record["record_hash"] = hashlib.sha256(canonical).hexdigest()
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._last_hash = record["record_hash"]

    def _load(self) -> None:
        if not self.ledger_path.exists():
            return
        previous = "0" * 64
        latest_obligations: dict[str, RecoveryObligation] = {}
        latest_conflicts: dict[str, ConflictRecord] = {}
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                digest = record.pop("record_hash")
            except (json.JSONDecodeError, KeyError):
                self.integrity_ok = False
                break
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if record.get("previous_hash") != previous or hashlib.sha256(canonical).hexdigest() != digest:
                self.integrity_ok = False
                break
            previous = digest
            self._sequence = max(self._sequence, int(record.get("sequence", 0)))
            event = record.get("event", "")
            payload = record.get("payload", {})
            if event.startswith("obligation_") and payload.get("obligation_id"):
                latest_obligations[payload["obligation_id"]] = RecoveryObligation.from_dict(payload)
            elif event.startswith("conflict_") and payload.get("conflict_id"):
                latest_conflicts[payload["conflict_id"]] = ConflictRecord.from_dict(payload)
        self._last_hash = previous
        self.obligations = sorted(latest_obligations.values(), key=lambda item: item.sequence)
        self.conflicts = list(latest_conflicts.values())


def resource_version(path: Path) -> ResourceVersion:
    if not path.exists() or not path.is_file():
        return ResourceVersion(exists=False)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return ResourceVersion(exists=True, size=stat.st_size, digest=digest.hexdigest(), modified_ns=stat.st_mtime_ns)


def versions_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        bool(left.get("exists")) == bool(right.get("exists"))
        and left.get("size") == right.get("size")
        and left.get("digest") == right.get("digest")
    )


def verify_certificate(certificate: dict[str, Any], key: bytes) -> bool:
    payload = {
        key_name: value
        for key_name, value in certificate.items()
        if key_name not in {"signature_algorithm", "signature", "verified"}
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(certificate.get("signature", "")))


def _certificate_key(data_root: Path) -> bytes:
    key_path = data_root / "recovery_certificate.key"
    if not key_path.exists():
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(secrets.token_bytes(32))
    return key_path.read_bytes()


def _inverse_action(operation: str, existed: bool) -> str:
    if operation in {"create", "write", "truncate", "replace"}:
        return "restore_preimage" if existed else "delete_created_resource"
    if operation == "delete":
        return "recreate_from_preimage"
    if operation == "rename":
        return "restore_original_name_and_preimage"
    return "restore_preimage" if existed else "delete_created_resource"
