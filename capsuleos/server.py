from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import CapsuleEngine

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
ENGINE = CapsuleEngine(ROOT)

app = FastAPI(
    title="CapsuleOS",
    description="Verified recovery-obligation scheduling for protected process-tree effects.",
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EdgeRecoveryPolicyRequest(BaseModel):
    enabled: bool = False
    device_class: str = "windows_x86_edge"
    recovery_reserve_bytes: int = Field(32 * 1024 * 1024, ge=4096)
    minimum_free_storage_bytes: int = Field(256 * 1024 * 1024, ge=0)
    minimum_free_memory_bytes: int = Field(128 * 1024 * 1024, ge=0)
    minimum_battery_percent: int = Field(10, ge=0, le=100)
    max_rollback_time_ms: int = Field(5000, ge=1, le=300000)
    estimated_restore_throughput_bps: int = Field(25 * 1024 * 1024, ge=1)
    flash_write_budget_bytes: int = Field(256 * 1024 * 1024, ge=8192)


class RecoveryContractRequest(BaseModel):
    protected_paths: list[str] = Field(default_factory=list)
    registry_locations: list[str] = Field(default_factory=list)
    journal_limit_bytes: int = Field(64 * 1024 * 1024, ge=1024)
    max_pending_obligations: int = Field(64, ge=1, le=4096)
    admission_timeout_ms: int = Field(3000, ge=100, le=30000)
    network_policy: str = Field("block", pattern="^(block|observe|allow)$")
    failure_policy: str = Field("fail_closed", pattern="^(fail_closed|fail_open)$")
    enforcement_mode: str = Field("cooperative", pattern="^(cooperative|monitor|native)$")
    edge_policy: EdgeRecoveryPolicyRequest = Field(default_factory=EdgeRecoveryPolicyRequest)


class RunCapsuleRequest(BaseModel):
    command: str | list[str] = Field(..., description="Command string or argv list to launch inside a capsule.")
    cwd: str | None = Field(None, description="Working directory for the launched process.")
    watch_paths: list[str] | None = Field(None, description="Files or directories to snapshot and journal.")
    runtime_limit_seconds: int | None = Field(None, ge=1)
    memory_limit_mb: int | None = Field(None, ge=16)
    priority: str = Field("normal", pattern="^(idle|low|normal|high)$")
    reversibility_budget: int = Field(70, ge=1, le=100)
    auto_suspend_on_budget: bool = True
    recovery_contract: RecoveryContractRequest | None = None
    baseline_mode: str = Field("adaptive", pattern="^(static|adaptive)$")
    baseline_profile: str | None = Field(None, max_length=120)
    update_baseline: bool = True
    capsuleos_api: str | None = None


class PrepareObligationRequest(BaseModel):
    resource_id: str
    operation: str = Field("write", pattern="^(create|write|truncate|rename|delete|replace)$")
    process_id: int | None = None
    thread_id: int | None = None
    dependencies: list[str] = Field(default_factory=list)
    deadline_ms: int | None = Field(None, ge=1, le=300000)


class ResolveConflictRequest(BaseModel):
    conflict_id: str
    resolution: str = Field(..., pattern="^(preserve_host|accept_capsule|abort)$")


class RegistrySelection(BaseModel):
    hive_path: str
    value_name: str


class SelectiveCommitRequest(BaseModel):
    keep_files: list[str] = Field(default_factory=list)
    keep_registry: list[RegistrySelection] = Field(default_factory=list)


@app.post("/api/capsules/run", status_code=201)
@app.post("/capsules/run", status_code=201)
def run_capsule(payload: RunCapsuleRequest) -> dict[str, Any]:
    try:
        capsule = ENGINE.run_capsule(payload.model_dump())
        return capsule.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/capsules")
@app.get("/capsules")
def list_capsules() -> list[dict[str, Any]]:
    return ENGINE.list_capsules()


@app.get("/api/baselines")
@app.get("/baselines")
def list_baselines() -> list[dict[str, Any]]:
    return ENGINE.list_baselines()


@app.get("/api/baselines/{profile_id}")
@app.get("/baselines/{profile_id}")
def get_baseline(profile_id: str) -> dict[str, Any]:
    try:
        return ENGINE.get_baseline(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="baseline profile not found") from exc


@app.post("/api/baselines/{profile_id}/reset")
@app.post("/baselines/{profile_id}/reset")
def reset_baseline(profile_id: str) -> dict[str, Any]:
    return ENGINE.reset_baseline(profile_id)


@app.get("/api/capsules/{capsule_id}")
@app.get("/capsules/{capsule_id}")
def get_capsule(capsule_id: str) -> dict[str, Any]:
    try:
        return ENGINE.get_capsule(capsule_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc


@app.get("/api/capsules/{capsule_id}/changes")
@app.get("/capsules/{capsule_id}/changes")
def get_changes(capsule_id: str) -> dict[str, Any]:
    try:
        return ENGINE.changes(capsule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc


@app.get("/api/capsules/{capsule_id}/obligations")
@app.get("/capsules/{capsule_id}/obligations")
def get_obligations(capsule_id: str) -> dict[str, Any]:
    return _capsule_query(capsule_id, ENGINE.obligations)


@app.get("/api/capsules/{capsule_id}/invariant")
@app.get("/capsules/{capsule_id}/invariant")
def get_invariant(capsule_id: str) -> dict[str, Any]:
    return _capsule_query(capsule_id, ENGINE.invariant)


@app.get("/api/capsules/{capsule_id}/conflicts")
@app.get("/capsules/{capsule_id}/conflicts")
def get_conflicts(capsule_id: str) -> dict[str, Any]:
    return _capsule_query(capsule_id, ENGINE.conflicts)


@app.get("/api/capsules/{capsule_id}/edge-capacity")
@app.get("/capsules/{capsule_id}/edge-capacity")
def get_edge_capacity(capsule_id: str) -> dict[str, Any]:
    return _capsule_query(capsule_id, ENGINE.edge_capacity)


@app.post("/api/capsules/{capsule_id}/obligations/prepare", status_code=201)
@app.post("/capsules/{capsule_id}/obligations/prepare", status_code=201)
def prepare_obligation(capsule_id: str, payload: PrepareObligationRequest) -> dict[str, Any]:
    try:
        return ENGINE.prepare_obligation(capsule_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc


@app.post("/api/capsules/{capsule_id}/obligations/{obligation_id}/expose")
@app.post("/capsules/{capsule_id}/obligations/{obligation_id}/expose")
def expose_obligation(capsule_id: str, obligation_id: str) -> dict[str, Any]:
    try:
        return ENGINE.expose_obligation(capsule_id, obligation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule or obligation not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/capsules/{capsule_id}/resolve-conflict")
@app.post("/capsules/{capsule_id}/resolve-conflict")
def resolve_conflict(capsule_id: str, payload: ResolveConflictRequest) -> dict[str, Any]:
    try:
        return ENGINE.resolve_conflict(capsule_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule or conflict not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/capsules/{capsule_id}/suspend")
@app.post("/capsules/{capsule_id}/suspend")
def suspend_capsule(capsule_id: str) -> dict[str, Any]:
    return _capsule_action(capsule_id, ENGINE.suspend_capsule)


@app.post("/api/capsules/{capsule_id}/resume")
@app.post("/capsules/{capsule_id}/resume")
def resume_capsule(capsule_id: str) -> dict[str, Any]:
    return _capsule_action(capsule_id, ENGINE.resume_capsule)


@app.post("/api/capsules/{capsule_id}/terminate")
@app.post("/capsules/{capsule_id}/terminate")
def terminate_capsule(capsule_id: str) -> dict[str, Any]:
    return _capsule_action(capsule_id, ENGINE.terminate_capsule)


@app.post("/api/capsules/{capsule_id}/commit")
@app.post("/capsules/{capsule_id}/commit")
def commit_capsule(capsule_id: str) -> dict[str, Any]:
    return _capsule_action(capsule_id, ENGINE.commit)


@app.post("/api/capsules/{capsule_id}/commit-selected")
@app.post("/capsules/{capsule_id}/commit-selected")
def selective_commit_capsule(capsule_id: str, payload: SelectiveCommitRequest) -> dict[str, Any]:
    try:
        return ENGINE.selective_commit(capsule_id, payload.model_dump()).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/capsules/{capsule_id}/rollback")
@app.post("/capsules/{capsule_id}/rollback")
def rollback_capsule(capsule_id: str) -> dict[str, Any]:
    return _capsule_action(capsule_id, ENGINE.rollback)


@app.get("/api/capsules/{capsule_id}/report")
@app.get("/capsules/{capsule_id}/report")
def get_report(capsule_id: str) -> dict[str, Any]:
    try:
        return ENGINE.report(capsule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc


@app.get("/api/capsules/{capsule_id}/recovery-certificate")
@app.get("/capsules/{capsule_id}/recovery-certificate")
def get_recovery_certificate(capsule_id: str) -> dict[str, Any]:
    try:
        return ENGINE.recovery_certificate(capsule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/driver/status")
@app.get("/driver/status")
def get_driver_status() -> dict[str, Any]:
    return ENGINE.driver_status()


@app.get("/api/metrics/system")
@app.get("/metrics/system")
def get_system_metrics() -> dict[str, Any]:
    return ENGINE.metrics()


@app.get("/api/config")
def get_config() -> dict[str, str]:
    return {"root_path": str(ROOT), "workspace_path": str(ENGINE.workspace_dir)}


@app.post("/api/demo/start", status_code=201)
def start_professor_demo() -> dict[str, Any]:
    baseline = ENGINE.workspace_dir / "protected_note.txt"
    baseline.write_text("CapsuleOS baseline note.\n", encoding="utf-8")
    for old_artifact in ENGINE.workspace_dir.glob("professor_demo_*.txt"):
        old_artifact.unlink(missing_ok=True)
    demo_tag = uuid.uuid4().hex[:6]
    capsule = ENGINE.run_capsule(
        {
            "command": [sys.executable, str(ROOT / "demos" / "demo_professor.py"), demo_tag],
            "cwd": str(ROOT),
            "watch_paths": [str(ENGINE.workspace_dir)],
            "runtime_limit_seconds": 30,
            "memory_limit_mb": 128,
            "priority": "normal",
            "reversibility_budget": 18,
            "auto_suspend_on_budget": False,
            "baseline_mode": "adaptive",
            "baseline_profile": "professor_edge_demo",
            "update_baseline": True,
            "capsuleos_api": f"http://127.0.0.1:{os.environ.get('CAPSULEOS_PORT', '8765')}",
            "recovery_contract": {
                "protected_paths": [str(ENGINE.workspace_dir)],
                "journal_limit_bytes": 16 * 1024 * 1024,
                "max_pending_obligations": 16,
                "admission_timeout_ms": 3000,
                "network_policy": "block",
                "failure_policy": "fail_closed",
                "enforcement_mode": "cooperative",
                "edge_policy": {
                    "enabled": True,
                    "device_class": "windows_x86_edge",
                    "recovery_reserve_bytes": 16 * 1024 * 1024,
                    "minimum_free_storage_bytes": 128 * 1024 * 1024,
                    "minimum_free_memory_bytes": 96 * 1024 * 1024,
                    "minimum_battery_percent": 8,
                    "max_rollback_time_ms": 1500,
                    "estimated_restore_throughput_bps": 20 * 1024 * 1024,
                    "flash_write_budget_bytes": 64 * 1024 * 1024,
                },
            },
        }
    )
    return capsule.to_dict()


@app.get("/demo")
def professor_demo() -> FileResponse:
    return FileResponse(PUBLIC / "demo.html")


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(PUBLIC / "index.html")


app.mount("/", StaticFiles(directory=PUBLIC), name="public")


def _capsule_action(capsule_id: str, action: Any) -> dict[str, Any]:
    try:
        return action(capsule_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _capsule_query(capsule_id: str, query: Any) -> dict[str, Any]:
    try:
        return query(capsule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="capsule not found") from exc


def main() -> None:
    host = os.environ.get("CAPSULEOS_HOST", "127.0.0.1")
    port = int(os.environ.get("CAPSULEOS_PORT", "8765"))
    uvicorn.run("capsuleos.server:app", host=host, port=port, reload=False)
