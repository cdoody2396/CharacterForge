"""Crash-safe file primitives. A leaf module: config and model both import
downward from here — never the reverse."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: Path, data: dict) -> None:
    """Crash-safe JSON write (unique temp file in the same dir + os.replace):
    a crash mid-write can never leave a half-written file, and concurrent
    writers cannot race a shared temp path. Lock-free — callers own their
    concurrency."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
