from pathlib import Path
import time


workspace = Path(__file__).resolve().parents[1] / "workspace"
workspace.mkdir(exist_ok=True)

baseline = workspace / "protected_note.txt"
if not baseline.exists():
    baseline.write_text("CapsuleOS baseline note.\n", encoding="utf-8")

baseline.write_text("This file was modified inside a capsule.\n", encoding="utf-8")

for index in range(3):
    (workspace / f"capsule_created_{index}.txt").write_text(
        f"Created by demo_writer at step {index}.\n",
        encoding="utf-8",
    )
    time.sleep(0.4)

print("demo_writer completed")
