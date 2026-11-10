from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capsuleos.client import gated_file_effect


workspace = Path(sys.argv[1]).resolve()
child_count = int(sys.argv[2])
memory_mb = int(sys.argv[3])
artifact_count = int(sys.argv[4])
hold_seconds = float(sys.argv[5])
workspace.mkdir(parents=True, exist_ok=True)

child_code = (
    "import sys,time; "
    "memory=bytearray(int(sys.argv[1])*1024*1024); "
    "time.sleep(float(sys.argv[2]))"
)
children = [
    subprocess.Popen(
        [sys.executable, "-c", child_code, str(memory_mb), str(hold_seconds + 2)]
    )
    for _ in range(child_count)
]

try:
    for index in range(artifact_count):
        target = workspace / f"adaptive_artifact_{index}.txt"
        gated_file_effect(
            target,
            "create",
            lambda target=target, index=index: target.write_text(
                f"adaptive workload artifact {index}\n",
                encoding="utf-8",
            ),
        )
    time.sleep(hold_seconds)
finally:
    for child in children:
        child.terminate()
