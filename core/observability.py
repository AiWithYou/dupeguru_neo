# Copyright 2026 AiWithYou contributors
#
# This software is licensed under the "GPLv3" License as described in the
# "LICENSE" file.

"""Privacy-preserving structured diagnostics for scans and actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
import uuid
import zipfile

_SENSITIVE_KEY = re.compile(r"(?:path|filename|directory|root|target|reference|keeper)", re.IGNORECASE)


class ObservabilityError(RuntimeError):
    """Raised when a diagnostic artifact cannot be produced safely."""


@dataclass(frozen=True)
class BuildIdentity:
    app_version: str
    commit: str

    def __post_init__(self) -> None:
        if not self.app_version.strip():
            raise ValueError("app_version is required")
        if not self.commit.strip():
            raise ValueError("build commit is required")


class PathRedactor:
    """Produces stable, non-reversible identifiers within one diagnostic set."""

    def __init__(self, key: bytes):
        if len(key) < 16:
            raise ValueError("redaction key must contain at least 16 bytes")
        self._key = key

    def redact(self, value: str | Path) -> str:
        normalized = str(value).replace("\\", "/").encode("utf-8", "surrogatepass")
        token = hmac.new(self._key, normalized, hashlib.sha256).hexdigest()[:20]
        return f"path:{token}"

    def sanitize(self, value: Any, *, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {str(k): self.sanitize(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.sanitize(item, key=key) for item in value]
        if isinstance(value, Path):
            return self.redact(value)
        if isinstance(value, str) and key is not None and _SENSITIVE_KEY.search(key):
            return self.redact(value)
        return value


class StructuredEventLogger:
    """Writes bounded JSON Lines without exposing raw paths by default."""

    def __init__(
        self,
        path: Path,
        *,
        identity: BuildIdentity,
        redactor: PathRedactor,
        session_id: str | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count < 0:
            raise ValueError("backup_count cannot be negative")
        self.path = path
        self.identity = identity
        self.redactor = redactor
        self.session_id = session_id or str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        logger_name = f"dupeguru_neo.events.{self.session_id}"
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    def event(
        self,
        event_type: str,
        *,
        scan_id: str | None = None,
        action_id: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not event_type or not event_type.strip():
            raise ValueError("event_type is required")
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "app_version": self.identity.app_version,
            "build_commit": self.identity.commit,
            "session_id": self.session_id,
        }
        if scan_id is not None:
            record["scan_id"] = scan_id
        if action_id is not None:
            record["action_id"] = action_id
        if fields:
            sanitized = self.redactor.sanitize(fields)
            if not isinstance(sanitized, dict):
                raise TypeError("sanitized fields must remain a mapping")
            record.update(sanitized)
        self._logger.info(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        return record

    def close(self) -> None:
        handlers = tuple(self._logger.handlers)
        self._logger.handlers.clear()
        for handler in handlers:
            handler.flush()
            handler.close()

    def __enter__(self) -> "StructuredEventLogger":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def create_crash_bundle(
    output_path: Path,
    *,
    identity: BuildIdentity,
    crash_id: str,
    traceback_text: str,
    metadata: Mapping[str, Any],
    redactor: PathRedactor,
    log_paths: Iterable[Path] = (),
) -> Path:
    """Create a local diagnostic bundle from explicitly supplied, sanitized data."""

    if not crash_id.strip():
        raise ValueError("crash_id is required")
    if output_path.suffix.lower() != ".zip":
        raise ObservabilityError("crash bundle path must end in .zip")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(output_path)
    manifest = {
        "schema_version": 1,
        "crash_id": crash_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": identity.app_version,
        "build_commit": identity.commit,
        "metadata": redactor.sanitize(metadata),
        "included_logs": [],
    }
    created = False
    try:
        with zipfile.ZipFile(output_path, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            created = True
            bundle.writestr("traceback.txt", traceback_text)
            for index, log_path in enumerate(log_paths):
                resolved = log_path.resolve(strict=True)
                if resolved.is_dir():
                    raise ObservabilityError(f"log source is a directory: {resolved}")
                archive_name = f"logs/event-{index:02d}.jsonl"
                bundle.write(resolved, archive_name)
                manifest["included_logs"].append(archive_name)
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2),
            )
    except Exception:
        if created and output_path.exists():
            output_path.unlink()
        raise
    return output_path
