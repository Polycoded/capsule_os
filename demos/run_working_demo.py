from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"
BASELINE_TEXT = "CapsuleOS baseline note. This text must survive rollback.\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CapsuleOS FastAPI end-to-end demo.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    print("CapsuleOS working demo")
    print(f"FastAPI base URL: {args.base_url}")

    prepare_workspace()
    print(f"Prepared monitored workspace: {WORKSPACE}")

    capsule = post(
        args.base_url,
        "/api/capsules/run",
        {
            "command": [sys.executable, "demos/demo_writer.py"],
            "cwd": str(ROOT),
            "watch_paths": [str(WORKSPACE)],
            "runtime_limit_seconds": 20,
            "memory_limit_mb": 256,
            "priority": "normal",
        },
    )
    capsule_id = capsule["capsule_id"]
    print(f"Launched capsule: {capsule_id}")

    capsule = wait_for_exit(args.base_url, capsule_id, args.timeout)
    changes = get(args.base_url, f"/api/capsules/{capsule_id}/changes")
    file_changes = changes["file_changes"]
    print(f"Capsule state: {capsule['state']}")
    print(f"Risk score: {capsule['risk_score']} ({', '.join(capsule['risk_reasons']) or 'no risk reasons'})")
    print(f"Detected file changes: {len(file_changes)}")
    for change in file_changes:
        print(f"  - {change['change_type']}: {change['path']}")

    rolled_back = post(args.base_url, f"/api/capsules/{capsule_id}/rollback", {})
    print(f"Rollback state: {rolled_back['state']}")

    verify_rollback()
    report = get(args.base_url, f"/api/capsules/{capsule_id}/report")
    report_path = ROOT / "data" / "demo_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Saved demo report: {report_path}")
    print("Demo passed: CapsuleOS detected changes and restored the monitored workspace.")
    return 0


def prepare_workspace() -> None:
    WORKSPACE.mkdir(exist_ok=True)
    for path in WORKSPACE.glob("capsule_created_*.txt"):
        path.unlink()
    (WORKSPACE / "protected_note.txt").write_text(BASELINE_TEXT, encoding="utf-8")


def verify_rollback() -> None:
    baseline = WORKSPACE / "protected_note.txt"
    actual = baseline.read_text(encoding="utf-8")
    if actual != BASELINE_TEXT:
        raise SystemExit("Rollback failed: protected_note.txt was not restored.")
    leftovers = sorted(WORKSPACE.glob("capsule_created_*.txt"))
    if leftovers:
        raise SystemExit(f"Rollback failed: created files remain: {leftovers}")


def wait_for_exit(base_url: str, capsule_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        capsule = get(base_url, f"/api/capsules/{capsule_id}")
        if capsule["state"] not in {"running", "suspended"}:
            return capsule
        time.sleep(1)
    raise SystemExit(f"Timed out waiting for capsule {capsule_id} to exit.")


def get(base_url: str, path: str) -> dict:
    request = urllib.request.Request(f"{base_url}{path}", method="GET")
    return send(request)


def post(base_url: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    return send(request)


def send(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"FastAPI server is not reachable: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
