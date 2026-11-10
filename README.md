# CapsuleOS

CapsuleOS is a Windows research prototype for **verified recovery-obligation
scheduling**. A cooperating protected workload asks CapsuleOS to prepare and
durably seal the inverse material for each persistent effect before the effect
is exposed. If recovery coverage, journal capacity, or resource versions cannot
be maintained, CapsuleOS denies the operation or suspends its process tree.

The immediately runnable product uses a FastAPI control plane, process
telemetry, a durable hash-linked obligation journal, conflict-safe rollback, and
signed recovery certificates. `native/` contains the Windows service,
Filter Manager protocol, and minifilter research source required for eventual
non-cooperative enforcement. No driver binary is shipped or loaded.

## Run

Install the dependencies once into the project:

```powershell
python -m pip install --target .deps -r requirements.txt
```

```powershell
.\run_capsuleos.ps1
```

Open the dashboard at:

```text
http://127.0.0.1:8765
```

## Professor Demo

Open the focused presentation view at:

```text
http://127.0.0.1:8765/demo
```

Click **Run full demo**. CapsuleOS launches a harmless four-process workload,
recalculates a Windows edge-PC recovery-capacity envelope before every protected
effect, seals four operation-specific recovery obligations, displays the process
tree and live memory, and enables commit or rollback. Rollback restores the
original file, removes three created files, terminates the tree, and displays a
verified certificate.

If port `8765` is already occupied during development, start another instance with:

```powershell
$env:CAPSULEOS_PORT='8767'; .\run_capsuleos.ps1
```

## Demo

Run a complete working demo:

```powershell
.\demo_capsuleos.ps1
```

If PowerShell script execution is disabled, use:

```cmd
demo_capsuleos.bat
```

The demo starts the FastAPI server if needed, prepares a monitored baseline file, launches `demos/demo_writer.py` inside a capsule, detects the file changes, rolls them back, verifies the baseline was restored, and writes `data/demo_report.json`.

## API

- `POST /capsules/run`
- `GET /capsules`
- `GET /capsules/{id}`
- `GET /capsules/{id}/changes`
- `GET /capsules/{id}/obligations`
- `GET /capsules/{id}/invariant`
- `GET /capsules/{id}/conflicts`
- `GET /capsules/{id}/edge-capacity`
- `POST /capsules/{id}/obligations/prepare`
- `POST /capsules/{id}/obligations/{obligation_id}/expose`
- `POST /capsules/{id}/suspend`
- `POST /capsules/{id}/resume`
- `POST /capsules/{id}/resolve-conflict`
- `POST /capsules/{id}/commit`
- `POST /capsules/{id}/commit-selected`
- `POST /capsules/{id}/rollback`
- `GET /capsules/{id}/recovery-certificate`
- `GET /capsules/{id}/report`
- `GET /driver/status`
- `GET /metrics/system`
- `GET /api/baselines`
- `GET /api/baselines/{profile_id}`
- `POST /api/baselines/{profile_id}/reset`

`POST /capsules/run` accepts a `recovery_contract` with protected paths,
registry locations, journal and pending-operation limits, admission timeout,
network policy, failure policy, enforcement mode, and an optional `edge_policy`.
Edge mode gates operations against live storage, memory and battery telemetry,
a reserved recovery pool, predicted rollback time, and a flash-write budget.

## Verification

```powershell
python -m unittest discover -s tests -v
```

The tests cover sealing before exposure, journal exhaustion, external-writer
conflicts, certificate verification, ledger reload, filesystem rollback, and
edge admission or denial under changing resource constraints.

Measured edge evidence can be regenerated with:

```powershell
python demos\benchmark_edge_admission.py
$env:CAPSULEOS_API='http://127.0.0.1:8765'
python demos\capture_edge_demo_evidence.py
```

The resulting JSON evidence is stored in `data/edge_admission_evidence.json`
and `data/edge_integration_evidence.json`.

The adaptive behavioral baseline can be regenerated with:

```powershell
$env:CAPSULEOS_API='http://127.0.0.1:8765'
python demos\benchmark_dynamic_baseline.py
```

It learns robust workload-specific bounds only from verified, conflict-free
runs. The resulting evidence is stored in
`data/dynamic_baseline_evidence.json`.

## Native Enforcement

See [`native/README.md`](native/README.md). Compile and test the minifilter only
inside a disposable Windows VM with Visual Studio, the WDK, test signing, and
Driver Verifier. The current laptop has not loaded kernel code.

## Research

The consolidated paper, conference, patent, Indian patentability, architecture,
and experiment analysis is in:

[`docs/CapsuleOS_Verified_Recovery_Patent_Dossier_2026.md`](docs/CapsuleOS_Verified_Recovery_Patent_Dossier_2026.md)

The professor-ready project report, including the 20-paper literature survey,
per-paper advantages and limitations, dynamic baseline methodology, measured
results, comparisons, and evidence boundaries, is in:

[`docs/CapsuleOS_Professor_Project_Report_2026.md`](docs/CapsuleOS_Professor_Project_Report_2026.md)

The same work in an IEEE-style research-paper structure is available at:

[`docs/CapsuleOS_Research_Paper_2026.md`](docs/CapsuleOS_Research_Paper_2026.md)

The rendered anonymous IEEE-style paper is available at:

[`output/pdf/CapsuleOS_Research_Paper_2026.pdf`](output/pdf/CapsuleOS_Research_Paper_2026.pdf)

Regenerate it from the verified JSON evidence with:

```powershell
python scripts\build_research_paper_pdf.py
```

## OS Syllabus Mapping

- Processes and PCB: per-process records and process tree tracking.
- Scheduling: priority, suspend/resume, runtime limits, and invariant-triggered
  intervention.
- Deadlocks/concurrency: hang/no-progress detection and child-process containment.
- Memory management: working set and memory budget tracking.
- File systems: preimage journaling, operation admission, rollback, and commit.
- Virtualization: a lightweight per-task recovery boundary.
- Protection/security: fail-closed protected effects, conflict preservation,
  tamper-evident history, and recovery certification.
