from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

from .models import utc_now


PROFILE_VERSION = 1
MIN_ACTIVE_SAMPLES = 5
MAX_SAMPLES = 30
METRIC_FLOORS = {
    "peak_memory_bytes": 8 * 1024 * 1024,
    "peak_process_count": 1,
    "peak_write_ops": 25,
    "peak_file_changes": 1,
    "peak_obligations": 1,
    "peak_journal_bytes": 4096,
    "runtime_ms": 250,
    "max_seal_latency_ms": 5,
}


class AdaptiveBaselineStore:
    """Explainable upper-bound profiles learned from verified capsule runs."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def profile_id(self, command: list[str], requested: str | None = None) -> str:
        identity = requested.strip() if requested else " ".join(command)
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:12]
        return f"{_slug(identity)[:36]}-{digest}"

    def get(self, profile_id: str) -> dict[str, Any]:
        path = self._path(profile_id)
        if not path.exists():
            return self._empty(profile_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        profiles = []
        for path in sorted(self.root.glob("*.json")):
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            profiles.append(
                {
                    "profile_id": profile["profile_id"],
                    "sample_count": len(profile.get("samples", [])),
                    "state": profile.get("state", "warming"),
                    "updated_at": profile.get("updated_at"),
                    "thresholds": profile.get("thresholds", {}),
                }
            )
        return profiles

    def observe(
        self,
        profile_id: str,
        metrics: dict[str, float],
        eligible: bool,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = self.get(profile_id)
        if eligible:
            sample = {
                "captured_at": utc_now(),
                "metrics": _normalized_metrics(metrics),
                "metadata": metadata or {},
            }
            profile["samples"] = (profile.get("samples", []) + [sample])[-MAX_SAMPLES:]
            profile["thresholds"] = _thresholds(profile["samples"])
            profile["state"] = (
                "active"
                if len(profile["samples"]) >= MIN_ACTIVE_SAMPLES
                else "warming"
            )
            profile["updated_at"] = utc_now()
            self._write(profile)
        return profile

    def assess(self, profile_id: str, metrics: dict[str, float]) -> dict[str, Any]:
        profile = self.get(profile_id)
        sample_count = len(profile.get("samples", []))
        current = _normalized_metrics(metrics)
        if sample_count < MIN_ACTIVE_SAMPLES:
            return {
                "profile_id": profile_id,
                "state": "warming",
                "sample_count": sample_count,
                "minimum_samples": MIN_ACTIVE_SAMPLES,
                "anomalous": False,
                "anomalies": [],
                "metrics": current,
                "thresholds": profile.get("thresholds", {}),
                "explanation": (
                    f"{MIN_ACTIVE_SAMPLES - sample_count} more verified clean runs "
                    "are required before adaptive decisions become active"
                ),
                "assessed_at": utc_now(),
            }

        anomalies = []
        for name, value in current.items():
            threshold = profile.get("thresholds", {}).get(name)
            if not threshold or value <= float(threshold["upper_bound"]):
                continue
            anomalies.append(
                {
                    "metric": name,
                    "observed": value,
                    "upper_bound": threshold["upper_bound"],
                    "median": threshold["median"],
                    "severity_ratio": round(
                        value / max(float(threshold["upper_bound"]), 1.0), 3
                    ),
                }
            )
        return {
            "profile_id": profile_id,
            "state": "anomalous" if anomalies else "normal",
            "sample_count": sample_count,
            "minimum_samples": MIN_ACTIVE_SAMPLES,
            "anomalous": bool(anomalies),
            "anomalies": anomalies,
            "metrics": current,
            "thresholds": profile.get("thresholds", {}),
            "explanation": (
                f"{len(anomalies)} metric(s) exceeded robust learned bounds"
                if anomalies
                else "all observed metrics remain within robust learned bounds"
            ),
            "assessed_at": utc_now(),
        }

    def reset(self, profile_id: str) -> None:
        self._path(profile_id).unlink(missing_ok=True)

    def _empty(self, profile_id: str) -> dict[str, Any]:
        return {
            "version": PROFILE_VERSION,
            "profile_id": profile_id,
            "state": "warming",
            "samples": [],
            "thresholds": {},
            "updated_at": None,
        }

    def _path(self, profile_id: str) -> Path:
        safe = _slug(profile_id)
        return self.root / f"{safe}.json"

    def _write(self, profile: dict[str, Any]) -> None:
        path = self._path(str(profile["profile_id"]))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        temporary.replace(path)


def _thresholds(samples: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    names = sorted(
        {
            name
            for sample in samples
            for name in sample.get("metrics", {})
            if name in METRIC_FLOORS
        }
    )
    result: dict[str, dict[str, float]] = {}
    for name in names:
        values = [
            float(sample["metrics"][name])
            for sample in samples
            if name in sample.get("metrics", {})
        ]
        median = statistics.median(values)
        deviations = [abs(value - median) for value in values]
        mad = statistics.median(deviations)
        robust_sigma = 1.4826 * mad
        p95 = _percentile(values, 0.95)
        upper = max(
            p95,
            median + 3 * robust_sigma,
            median + float(METRIC_FLOORS[name]),
            median * 1.10,
        )
        result[name] = {
            "median": round(median, 3),
            "mad": round(mad, 3),
            "p95": round(p95, 3),
            "upper_bound": round(upper, 3),
        }
    return result


def _normalized_metrics(metrics: dict[str, float]) -> dict[str, float]:
    return {
        name: round(max(0.0, float(value)), 3)
        for name, value in metrics.items()
        if name in METRIC_FLOORS and math.isfinite(float(value))
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil((len(ordered) - 1) * fraction))
    return ordered[index]


def _slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(part for part in cleaned.split("-") if part) or "profile"
