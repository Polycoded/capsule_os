from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "edge_integration_evidence.json"
API = os.environ.get("CAPSULEOS_API", "http://127.0.0.1:8765").rstrip("/")


def main() -> None:
    started = _request("/api/demo/start", "POST")
    capsule_id = str(started["capsule_id"])
    deadline = time.time() + 15
    live = started
    while time.time() < deadline:
        live = _request(f"/api/capsules/{capsule_id}")
        if len(live.get("obligations", [])) >= 4:
            break
        time.sleep(0.25)

    rolled_back = _request(f"/api/capsules/{capsule_id}/rollback", "POST")
    certificate = rolled_back.get("recovery_certificate") or {}
    rollback_event = next(
        (
            item
            for item in reversed(rolled_back.get("audit", []))
            if item.get("event") == "changes_rolled_back"
        ),
        {},
    )
    evidence = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "capsule_id": capsule_id,
        "process_count": len(live.get("processes", [])),
        "obligation_count": len(live.get("obligations", [])),
        "edge_capacity": live.get("edge_capacity", {}),
        "invariant": live.get("invariant", {}),
        "final_state": rolled_back.get("state"),
        "rollback_result": rollback_event.get("detail", {}).get("file_result", {}),
        "certificate": {
            "verified": certificate.get("verified"),
            "recovery_complete": certificate.get("recovery_complete"),
            "obligations_total": certificate.get("obligations_total"),
            "obligations_verified": certificate.get("obligations_verified"),
            "unresolved_effects": certificate.get("unresolved_effects"),
        },
    }
    OUTPUT.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


def _request(path: str, method: str = "GET") -> dict[str, object]:
    payload = b"{}" if method == "POST" else None
    request = urllib.request.Request(
        API + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
