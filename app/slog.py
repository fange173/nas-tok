"""结构化日志：一行 JSON，便于 NAS 上 grep。"""
from __future__ import annotations

import json
import sys

from app.database import utcnow


def slog(event: str, **fields) -> None:
    rec = {"ts": utcnow().isoformat(sep=" ", timespec="seconds"), "event": event}
    rec.update(fields)
    print(json.dumps(rec, ensure_ascii=False), file=sys.stderr, flush=True)
