from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import FileChange, RegistryChange

MAX_BACKUP_BYTES = 25 * 1024 * 1024
STARTUP_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"


@dataclass
class FileSnapshot:
    path: str
    exists: bool
    size: int | None = None
    digest: str | None = None
    mtime: float | None = None
    backup_path: str | None = None


class FileJournal:
    def __init__(self, capsule_dir: Path, watch_paths: list[str]):
        self.capsule_dir = capsule_dir
        self.watch_paths = [Path(p).resolve() for p in watch_paths]
        self.backup_dir = capsule_dir / "file_backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.baseline: dict[str, FileSnapshot] = {}
        self.changes: list[FileChange] = []

    def capture_baseline(self) -> None:
        for root in self.watch_paths:
            if root.is_file():
                self._snapshot_file(root)
            elif root.exists():
                for path in root.rglob("*"):
                    if path.is_file() and not self._is_inside_capsule(path):
                        self._snapshot_file(path)
        self._write_json("file_baseline.json", {k: asdict(v) for k, v in self.baseline.items()})

    def detect_changes(self) -> list[FileChange]:
        current_paths: set[str] = set()
        latest: dict[str, FileSnapshot] = {}
        for root in self.watch_paths:
            if root.is_file():
                candidates = [root]
            elif root.exists():
                candidates = [p for p in root.rglob("*") if p.is_file() and not self._is_inside_capsule(p)]
            else:
                candidates = []
            for path in candidates:
                snap = self._light_snapshot(path)
                latest[snap.path] = snap
                current_paths.add(snap.path)

        changes: list[FileChange] = []
        baseline_paths = set(self.baseline)
        for path in sorted(current_paths - baseline_paths):
            after = latest[path]
            changes.append(FileChange(path=path, change_type="created", size_after=after.size, hash_after=after.digest))

        for path in sorted(baseline_paths - current_paths):
            before = self.baseline[path]
            changes.append(
                FileChange(
                    path=path,
                    change_type="deleted",
                    size_before=before.size,
                    hash_before=before.digest,
                    backup_path=before.backup_path,
                )
            )

        for path in sorted(current_paths & baseline_paths):
            before = self.baseline[path]
            after = latest[path]
            if before.digest != after.digest or before.size != after.size:
                changes.append(
                    FileChange(
                        path=path,
                        change_type="modified",
                        size_before=before.size,
                        size_after=after.size,
                        hash_before=before.digest,
                        hash_after=after.digest,
                        backup_path=before.backup_path,
                    )
                )

        self.changes = changes
        self._write_json("file_changes.json", [c.to_dict() for c in changes])
        return changes

    def rollback(self, keep_paths: set[str] | None = None) -> dict[str, int]:
        self.detect_changes()
        normalized_keep = {self._normalize_path(path) for path in (keep_paths or set())}
        restored = 0
        deleted = 0
        kept = 0
        skipped = 0
        for change in self.changes:
            path = Path(change.path)
            if self._normalize_path(change.path) in normalized_keep:
                kept += 1
                continue
            try:
                if change.change_type == "created":
                    if path.exists() and path.is_file():
                        path.unlink()
                        deleted += 1
                elif change.change_type in {"modified", "deleted"} and change.backup_path:
                    backup = Path(change.backup_path)
                    if backup.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, path)
                        restored += 1
                    else:
                        skipped += 1
            except OSError:
                skipped += 1
        return {"restored": restored, "deleted": deleted, "kept": kept, "skipped": skipped}

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.normcase(str(Path(path).resolve()))

    def _snapshot_file(self, path: Path) -> None:
        snap = self._light_snapshot(path)
        if snap.exists and snap.size is not None and snap.size <= MAX_BACKUP_BYTES:
            backup = self.backup_dir / self._backup_name(path)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            snap.backup_path = str(backup)
        self.baseline[snap.path] = snap

    def _light_snapshot(self, path: Path) -> FileSnapshot:
        resolved = path.resolve()
        if not resolved.exists():
            return FileSnapshot(path=str(resolved), exists=False)
        stat = resolved.stat()
        return FileSnapshot(path=str(resolved), exists=True, size=stat.st_size, digest=self._hash(resolved), mtime=stat.st_mtime)

    def _hash(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _backup_name(self, path: Path) -> Path:
        safe = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
        return Path(safe[:2]) / f"{safe}_{path.name}"

    def _write_json(self, name: str, data: object) -> None:
        (self.capsule_dir / name).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _is_inside_capsule(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.capsule_dir.resolve())
            return True
        except ValueError:
            return False


class RegistryJournal:
    def __init__(self, capsule_dir: Path):
        self.capsule_dir = capsule_dir
        self.baseline = self._query_startup_values()
        self.changes: list[RegistryChange] = []
        (self.capsule_dir / "registry_baseline.json").write_text(json.dumps(self.baseline, indent=2), encoding="utf-8")

    def detect_changes(self) -> list[RegistryChange]:
        current = self._query_startup_values()
        changes: list[RegistryChange] = []
        before_keys = set(self.baseline)
        after_keys = set(current)
        for name in sorted(after_keys - before_keys):
            changes.append(RegistryChange(STARTUP_KEY, name, "created", None, current[name]))
        for name in sorted(before_keys - after_keys):
            changes.append(RegistryChange(STARTUP_KEY, name, "deleted", self.baseline[name], None))
        for name in sorted(before_keys & after_keys):
            if self.baseline[name] != current[name]:
                changes.append(RegistryChange(STARTUP_KEY, name, "modified", self.baseline[name], current[name]))
        self.changes = changes
        (self.capsule_dir / "registry_changes.json").write_text(
            json.dumps([c.to_dict() for c in changes], indent=2),
            encoding="utf-8",
        )
        return changes

    def rollback(self, keep_values: set[tuple[str, str]] | None = None) -> dict[str, int]:
        self.detect_changes()
        normalized_keep = {(hive.casefold(), name.casefold()) for hive, name in (keep_values or set())}
        reverted = 0
        kept = 0
        skipped = 0
        for change in self.changes:
            if (change.hive_path.casefold(), change.value_name.casefold()) in normalized_keep:
                kept += 1
                continue
            try:
                if change.change_type == "created":
                    subprocess.run(["reg", "delete", STARTUP_KEY, "/v", change.value_name, "/f"], check=False, capture_output=True, text=True)
                    reverted += 1
                elif change.change_type in {"modified", "deleted"} and change.before:
                    subprocess.run(["reg", "add", STARTUP_KEY, "/v", change.value_name, "/d", change.before, "/f"], check=False, capture_output=True, text=True)
                    reverted += 1
            except OSError:
                skipped += 1
        return {"reverted": reverted, "kept": kept, "skipped": skipped}

    def _query_startup_values(self) -> dict[str, str]:
        if os.name != "nt":
            return {}
        result = subprocess.run(["reg", "query", STARTUP_KEY], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return {}
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[1].startswith("REG_"):
                values[parts[0]] = parts[2]
        return values
