from pathlib import Path
import time


workspace = Path(__file__).resolve().parents[1] / "workspace"
workspace.mkdir(exist_ok=True)

for index in range(4):
    (workspace / f"budget_change_{index}.txt").write_text(
        f"Reversibility-budget test change {index}.\n",
        encoding="utf-8",
    )

# Remain active so the monitor can enforce the scheduling policy.
time.sleep(20)
