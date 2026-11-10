from __future__ import annotations

import ctypes
import math
import os
import shutil
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .models import EdgeRecoveryPolicy, utc_now


LEDGER_OVERHEAD_BYTES = 4096
PREIMAGE_WRITE_AMPLIFICATION = 2


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


def collect_edge_telemetry(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(_existing_parent(path))
    telemetry: dict[str, Any] = {
        "captured_at": utc_now(),
        "free_storage_bytes": usage.free,
        "free_memory_bytes": _free_memory_bytes(),
        "battery_percent": None,
        "on_ac_power": None,
    }
    if os.name == "nt":
        status = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            telemetry["on_ac_power"] = status.ACLineStatus == 1
            if status.BatteryLifePercent != 255:
                telemetry["battery_percent"] = int(status.BatteryLifePercent)
    return telemetry


def evaluate_edge_admission(
    policy: EdgeRecoveryPolicy,
    telemetry: dict[str, Any],
    preimage_bytes: int,
    journal_reserved_bytes: int,
    flash_written_bytes: int,
    requested_deadline_ms: int | None = None,
) -> dict[str, Any]:
    required_recovery_bytes = max(0, preimage_bytes) + LEDGER_OVERHEAD_BYTES
    projected_flash_bytes = required_recovery_bytes * PREIMAGE_WRITE_AMPLIFICATION
    throughput = max(1, policy.estimated_restore_throughput_bps)
    estimated_rollback_ms = max(
        1,
        math.ceil((max(0, preimage_bytes) / throughput) * 1000) + 2,
    )
    deadline_ms = requested_deadline_ms or policy.max_rollback_time_ms
    reserve_remaining = max(0, policy.recovery_reserve_bytes - journal_reserved_bytes)
    flash_remaining = max(0, policy.flash_write_budget_bytes - flash_written_bytes)
    reasons: list[str] = []

    free_storage = int(telemetry.get("free_storage_bytes") or 0)
    free_memory = int(telemetry.get("free_memory_bytes") or 0)
    battery = telemetry.get("battery_percent")
    on_ac_power = telemetry.get("on_ac_power")
    if required_recovery_bytes > reserve_remaining:
        reasons.append("recovery reserve is insufficient for the operation")
    if free_storage - required_recovery_bytes < policy.minimum_free_storage_bytes:
        reasons.append("minimum free storage would be violated")
    if free_memory and free_memory < policy.minimum_free_memory_bytes:
        reasons.append("available memory is below the edge safety floor")
    if (
        battery is not None
        and on_ac_power is False
        and int(battery) < policy.minimum_battery_percent
    ):
        reasons.append("battery level is below the recovery safety floor")
    if estimated_rollback_ms > deadline_ms:
        reasons.append("predicted rollback time exceeds the operation deadline")
    if projected_flash_bytes > flash_remaining:
        reasons.append("flash write-endurance budget would be exceeded")

    return {
        "enabled": policy.enabled,
        "device_class": policy.device_class,
        "decision": "admit" if not reasons else "deny",
        "reasons": reasons,
        "required_recovery_bytes": required_recovery_bytes,
        "recovery_reserve_bytes": policy.recovery_reserve_bytes,
        "reserve_remaining_before_bytes": reserve_remaining,
        "reserve_remaining_after_bytes": max(0, reserve_remaining - required_recovery_bytes),
        "estimated_rollback_ms": estimated_rollback_ms,
        "rollback_deadline_ms": deadline_ms,
        "projected_flash_bytes": projected_flash_bytes,
        "flash_budget_remaining_bytes": flash_remaining,
        "flash_budget_remaining_after_bytes": max(
            0, flash_remaining - projected_flash_bytes
        ),
        "free_storage_bytes": free_storage,
        "minimum_free_storage_bytes": policy.minimum_free_storage_bytes,
        "free_memory_bytes": free_memory,
        "minimum_free_memory_bytes": policy.minimum_free_memory_bytes,
        "battery_percent": battery,
        "on_ac_power": on_ac_power,
        "evaluated_at": utc_now(),
    }


def _existing_parent(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _free_memory_bytes() -> int:
    if os.name == "nt":
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys)
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        except (ValueError, OSError):
            pass
    return 0
