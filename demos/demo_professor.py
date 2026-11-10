from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capsuleos.client import gated_file_effect


tag = re.sub(r"[^a-zA-Z0-9]", "", sys.argv[1] if len(sys.argv) > 1 else "sample")
workspace = Path(__file__).resolve().parents[1] / "workspace"
workspace.mkdir(exist_ok=True)

# Three descendants make process-tree containment visible while remaining harmless.
child_code = "import time; memory=bytearray(6*1024*1024); time.sleep(30)"
children = [
    subprocess.Popen([sys.executable, "-c", child_code, f"worker-{index}"])
    for index in range(1, 4)
]

protected_note = workspace / "protected_note.txt"
gated_file_effect(
    protected_note,
    "write",
    lambda: protected_note.write_text(
        "This protected note was modified inside a CapsuleOS execution capsule.\n",
        encoding="utf-8",
    ),
    pause_after_seal_seconds=2.4,
)

for index in range(3):
    artifact = workspace / f"professor_demo_{tag}_{index}.txt"
    gated_file_effect(
        artifact,
        "create",
        lambda artifact=artifact, index=index: artifact.write_text(
            f"Temporary artifact created by capsule worker {index + 1}.\n",
            encoding="utf-8",
        ),
    )

# Stay active long enough for the monitor to enforce the reversibility budget.
try:
    time.sleep(30)
finally:
    for child in children:
        child.terminate()
