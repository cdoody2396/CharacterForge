"""Layer 4 scaffold — local, append-only JSONL audit log.

Not enforcement: visibility (DECISIONS.md §11, layer 4). Every generation,
conversation turn, and filter block can be logged locally so boundary-testing
is reviewable and the other layers get a tuning signal. Toggleable via
``safety.logging_enabled``; per-day files under ``data/logs/``.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, log_dir: Path | str, enabled: bool = True):
        self.log_dir = Path(log_dir)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    def path_for_today(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return self.log_dir / f"audit-{stamp}.jsonl"

    def log(self, kind: str, **payload: Any) -> None:
        """Append one event. Never raises into the caller — a broken log
        must not take down the app (it is visibility, not enforcement).
        Non-serializable values (Path/datetime/set, e.g. a generated-frame
        path from a later stage) are coerced via ``default=str`` rather than
        raising, and serialization runs inside the guard so a bad payload
        cannot escape."""
        if not self.enabled:
            return
        try:
            event = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "kind": kind,
                **payload,
            }
            line = json.dumps(event, ensure_ascii=False, default=str)
            with self._lock:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with self.path_for_today().open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except (OSError, TypeError, ValueError):
            pass
