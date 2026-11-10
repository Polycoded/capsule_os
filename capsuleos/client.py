from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


def gated_file_effect(
    path: Path,
    operation: str,
    effect: Callable[[], None],
    pause_after_seal_seconds: float = 0,
) -> str:
    """Cooperative pre-write gate used until the native minifilter is loaded."""
    capsule_id = os.environ.get("CAPSULEOS_ID")
    api_root = os.environ.get("CAPSULEOS_API", "http://127.0.0.1:8765").rstrip("/")
    if not capsule_id:
        raise RuntimeError("CAPSULEOS_ID is required for a protected effect")

    obligation = _request(
        f"{api_root}/api/capsules/{capsule_id}/obligations/prepare",
        {
            "resource_id": str(path.resolve()),
            "operation": operation,
            "process_id": os.getpid(),
        },
    )
    if obligation.get("state") != "sealed":
        raise PermissionError(obligation.get("failure_reason") or "recovery obligation was not sealed")

    if pause_after_seal_seconds:
        import time

        time.sleep(pause_after_seal_seconds)
    effect()
    _request(
        f"{api_root}/api/capsules/{capsule_id}/obligations/{obligation['obligation_id']}/expose",
        {},
    )
    return str(obligation["obligation_id"])


def _request(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CapsuleOS gate rejected the operation: {detail}") from exc
