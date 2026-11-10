from __future__ import annotations

import json
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from capsuleos.edge import collect_edge_telemetry, evaluate_edge_admission
from capsuleos.models import EdgeRecoveryPolicy


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "edge_admission_evidence.json"
SIZES = [0, 4 * 1024, 64 * 1024, 1024 * 1024, 8 * 1024 * 1024]
REPETITIONS = 100


def main() -> None:
    policy = EdgeRecoveryPolicy(enabled=True)
    telemetry = collect_edge_telemetry(ROOT / "workspace")
    scenarios: list[dict[str, object]] = []
    for size in SIZES:
        durations_ms: list[float] = []
        result: dict[str, object] = {}
        for _ in range(REPETITIONS):
            started = time.perf_counter_ns()
            result = evaluate_edge_admission(
                policy,
                telemetry,
                preimage_bytes=size,
                journal_reserved_bytes=0,
                flash_written_bytes=0,
            )
            durations_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        scenarios.append(
            {
                "preimage_bytes": size,
                "decision": result["decision"],
                "estimated_rollback_ms": result["estimated_rollback_ms"],
                "required_recovery_bytes": result["required_recovery_bytes"],
                "evaluation_latency_ms": {
                    "median": round(statistics.median(durations_ms), 6),
                    "p95": round(_percentile(durations_ms, 0.95), 6),
                    "maximum": round(max(durations_ms), 6),
                },
            }
        )

    evidence = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
        "method": {
            "repetitions_per_scenario": REPETITIONS,
            "note": "Measures deterministic capacity-decision computation only; it is not a VM isolation benchmark.",
        },
        "policy": policy.to_dict(),
        "telemetry": telemetry,
        "scenarios": scenarios,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * fraction))
    return ordered[index]


if __name__ == "__main__":
    main()
