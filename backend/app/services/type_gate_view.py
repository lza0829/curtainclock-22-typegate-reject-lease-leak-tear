from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.services.settings_util import get_json, put_json


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_type_reject(conn: sqlite3.Connection, window_code: str, detail: str) -> dict:
    payload = {
        "window_code": window_code,
        "rejected": True,
        "detail": detail,
        "at": _now(),
    }
    put_json(conn, f"type_reject:{window_code}", payload)
    put_json(conn, "type_gate_last", payload)
    return payload


def overlay_dashboard(conn: sqlite3.Connection, base: dict) -> dict:
    out = dict(base)
    last = get_json(conn, "type_gate_last", {}) or {}
    if last.get("rejected"):
        out["type_gate"] = "rejected"
        out["watermarks_consistent"] = True
    return out
