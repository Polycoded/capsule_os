from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("CAPSULEOS_API", "http://127.0.0.1:8765").rstrip("/")
WORKSPACE = ROOT / "data" / "adaptive_benchmark_workspace"
OUTPUT = ROOT / "data" / "dynamic_baseline_evidence.json"
PROFILE_NAME = "capsuleos_dynamic_baseline_evidence"


def main() -> None:
    _reset_existing_profile()
    _clean_workspace()
    normal_runs = []
    for index in range(6):
        normal_runs.append(
            _run_case(
                f"training-{index + 1}",
                child_count=2,
                memory_mb=4,
                artifact_count=2,
                hold_seconds=1.2,
                update_baseline=True,
            )
        )
        _clean_workspace()

    holdout = _run_case(
        "normal-holdout",
        child_count=2,
        memory_mb=4,
        artifact_count=2,
        hold_seconds=1.2,
        update_baseline=False,
    )
    _clean_workspace()
    anomaly = _run_case(
        "memory-process-file-anomaly",
        child_count=7,
        memory_mb=20,
        artifact_count=8,
        hold_seconds=1.8,
        update_baseline=False,
    )
    _clean_workspace()

    profile_id = str(normal_runs[-1]["baseline_profile_id"])
    profile = _request(f"/api/baselines/{profile_id}")
    evidence = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "training_runs": 6,
            "normal_holdout_runs": 1,
            "anomaly_runs": 1,
            "profile_update_rule": "verified, complete, conflict-free runs only",
            "algorithm": "rolling median, MAD, p95 and metric-specific safety floor",
            "note": "All observations were collected from launched CapsuleOS process trees; no result values are fabricated.",
        },
        "profile_id": profile_id,
        "profile_state": profile["state"],
        "sample_count": len(profile["samples"]),
        "thresholds": profile["thresholds"],
        "training_runs": normal_runs,
        "holdout": holdout,
        "anomaly": anomaly,
    }
    OUTPUT.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


def _run_case(
    name: str,
    child_count: int,
    memory_mb: int,
    artifact_count: int,
    hold_seconds: float,
    update_baseline: bool,
) -> dict[str, object]:
    payload = {
        "command": [
            sys.executable,
            str(ROOT / "demos" / "demo_adaptive_workload.py"),
            str(WORKSPACE),
            str(child_count),
            str(memory_mb),
            str(artifact_count),
            str(hold_seconds),
        ],
        "cwd": str(ROOT),
        "watch_paths": [str(WORKSPACE)],
        "runtime_limit_seconds": 20,
        "memory_limit_mb": 512,
        "priority": "normal",
        "capsuleos_api": API,
        "baseline_mode": "adaptive",
        "baseline_profile": PROFILE_NAME,
        "update_baseline": update_baseline,
        "recovery_contract": {
            "protected_paths": [str(WORKSPACE)],
            "journal_limit_bytes": 32 * 1024 * 1024,
            "max_pending_obligations": 32,
            "network_policy": "block",
            "failure_policy": "fail_closed",
            "enforcement_mode": "cooperative",
        },
    }
    capsule = _request("/api/capsules/run", "POST", payload)
    capsule_id = str(capsule["capsule_id"])
    deadline = time.time() + 20
    latest = capsule
    while time.time() < deadline:
        latest = _request(f"/api/capsules/{capsule_id}")
        expected = artifact_count
        if (
            len(latest.get("obligations", [])) >= expected
            and latest.get("state") in {"exited", "invariant_suspended"}
        ):
            break
        time.sleep(0.25)

    live_assessment = latest.get("baseline_assessment", {})
    live_metrics = latest.get("behavior_metrics", {})
    finalized = _request(f"/api/capsules/{capsule_id}/rollback", "POST", {})
    certificate = finalized.get("recovery_certificate", {})
    return {
        "name": name,
        "capsule_id": capsule_id,
        "baseline_profile_id": latest.get("baseline_profile_id"),
        "metrics": live_metrics,
        "assessment": live_assessment,
        "process_count": len(latest.get("processes", [])),
        "obligation_count": len(latest.get("obligations", [])),
        "invariant_satisfied": latest.get("invariant", {}).get("satisfied"),
        "certificate_verified": certificate.get("verified"),
        "recovery_complete": certificate.get("recovery_complete"),
    }


def _request(
    path: str,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    body = json.dumps(payload or {}).encode("utf-8") if method == "POST" else None
    request = urllib.request.Request(
        API + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _clean_workspace() -> None:
    resolved = WORKSPACE.resolve()
    data_root = (ROOT / "data").resolve()
    if data_root not in resolved.parents:
        raise RuntimeError("benchmark workspace escaped the data directory")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def _reset_existing_profile() -> None:
    profiles = _request("/api/baselines")
    for profile in profiles:
        profile_id = str(profile.get("profile_id") or "")
        if profile_id.startswith("capsuleos-dynamic-baseline-evidence-"):
            _request(f"/api/baselines/{profile_id}/reset", "POST", {})


if __name__ == "__main__":
    main()
