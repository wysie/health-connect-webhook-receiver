#!/usr/bin/env python3
"""
Self-hosted Health Connect webhook receiver.

- Listens on configurable host/port.
- Accepts POST /health-connect/<person>.
- Auth via X-Webhook-Token header or ?token= query parameter.
- Stores raw JSON payloads and lightly-normalized sleep/vitals into SQLite.
- No cloud calls, no LLM calls.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_DATA_DIR = Path.home() / ".hermes" / "health_connect"
DEFAULT_DB = DEFAULT_DATA_DIR / "health_connect.sqlite"
DEFAULT_TOKEN_FILE = DEFAULT_DATA_DIR / "webhook_token"
DEFAULT_LOG = Path.home() / ".hermes" / "logs" / "health_connect_receiver.log"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(msg: str) -> None:
    DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{utc_now()} {msg}\n"
    with DEFAULT_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def load_token() -> str:
    env = os.environ.get("HEALTH_CONNECT_WEBHOOK_TOKEN")
    if env:
        return env.strip()
    if DEFAULT_TOKEN_FILE.exists():
        return DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    raise RuntimeError(f"Missing token: set HEALTH_CONNECT_WEBHOOK_TOKEN or create {DEFAULT_TOKEN_FILE}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                person TEXT NOT NULL,
                source_ip TEXT,
                user_agent TEXT,
                payload_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(person, payload_sha256)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sleep_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_event_id INTEGER NOT NULL,
                person TEXT NOT NULL,
                session_start TEXT,
                session_end TEXT,
                duration_seconds REAL,
                stage_count INTEGER,
                stages_json TEXT,
                UNIQUE(person, session_start, session_end),
                FOREIGN KEY(raw_event_id) REFERENCES raw_events(id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vitals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_event_id INTEGER NOT NULL,
                person TEXT NOT NULL,
                metric TEXT NOT NULL,
                time TEXT,
                value REAL,
                unit TEXT,
                raw_json TEXT,
                UNIQUE(person, metric, time, value),
                FOREIGN KEY(raw_event_id) REFERENCES raw_events(id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                person TEXT NOT NULL,
                raw_event_id INTEGER,
                status TEXT NOT NULL,
                message TEXT
            )
            """
        )


def as_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def insert_payload(db_path: Path, person: str, payload: dict, source_ip: str | None, user_agent: str | None) -> dict:
    received_at = utc_now()
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()

    init_db(db_path)
    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        cur = con.execute(
            """
            INSERT OR IGNORE INTO raw_events
              (received_at, person, source_ip, user_agent, payload_sha256, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (received_at, person, source_ip, user_agent, digest, payload_text),
        )
        inserted = cur.rowcount == 1
        if inserted:
            raw_event_id = cur.lastrowid
        else:
            raw_event_id = con.execute(
                "SELECT id FROM raw_events WHERE person=? AND payload_sha256=?",
                (person, digest),
            ).fetchone()[0]

        sleep_seen = 0
        sleep_inserted = 0
        vitals_seen = 0
        vitals_inserted = 0

        if inserted:
            for sleep in payload.get("sleep") or []:
                if not isinstance(sleep, dict):
                    continue
                stages = sleep.get("stages") or []
                # HC Webhook currently provides session_end_time + duration_seconds,
                # but no explicit session_start_time. Infer start from the earliest
                # stage start_time when available; fallback to end-duration.
                session_start = sleep.get("session_start_time") or sleep.get("start_time")
                session_end = sleep.get("session_end_time") or sleep.get("end_time")
                if not session_start and isinstance(stages, list) and stages:
                    starts = [s.get("start_time") for s in stages if isinstance(s, dict) and s.get("start_time")]
                    if starts:
                        session_start = min(starts)
                if not session_start and session_end and sleep.get("duration_seconds") is not None:
                    try:
                        end_dt = datetime.fromisoformat(session_end.replace("Z", "+00:00"))
                        session_start = (end_dt.timestamp() - float(sleep.get("duration_seconds")))
                        session_start = datetime.fromtimestamp(session_start, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                    except Exception:
                        pass
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO sleep_sessions
                      (raw_event_id, person, session_start, session_end, duration_seconds, stage_count, stages_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        raw_event_id,
                        person,
                        session_start,
                        session_end,
                        as_float(sleep.get("duration_seconds")),
                        len(stages) if isinstance(stages, list) else None,
                        json.dumps(stages, ensure_ascii=False),
                    ),
                )
                sleep_seen += 1
                sleep_inserted += cur.rowcount

            vital_specs = {
                "heart_rate": ("bpm", ["bpm", "beats_per_minute", "value"]),
                "resting_heart_rate": ("bpm", ["bpm", "beats_per_minute", "value"]),
                "heart_rate_variability": ("ms", ["heart_rate_variability_millis", "rmssd_millis", "value"]),
                "oxygen_saturation": ("percent", ["percentage", "value"]),
                "respiratory_rate": ("rpm", ["rate", "value"]),
            }
            for metric, (unit, keys) in vital_specs.items():
                for rec in payload.get(metric) or []:
                    if not isinstance(rec, dict):
                        continue
                    value = None
                    for k in keys:
                        if k in rec:
                            value = as_float(rec.get(k))
                            break
                    t = rec.get("time") or rec.get("start_time") or rec.get("end_time")
                    cur = con.execute(
                        """
                        INSERT OR IGNORE INTO vitals
                          (raw_event_id, person, metric, time, value, unit, raw_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (raw_event_id, person, metric, t, value, unit, json.dumps(rec, ensure_ascii=False)),
                    )
                    vitals_seen += 1
                    vitals_inserted += cur.rowcount

        status = "inserted" if inserted else "duplicate_raw"
        msg = (
            f"{status}: raw_event_id={raw_event_id} "
            f"sleep_seen={sleep_seen} sleep_inserted={sleep_inserted} "
            f"vitals_seen={vitals_seen} vitals_inserted={vitals_inserted} "
            f"sha={digest[:12]}"
        )
        con.execute(
            "INSERT INTO sync_log(received_at, person, raw_event_id, status, message) VALUES (?, ?, ?, ?, ?)",
            (received_at, person, raw_event_id, status, msg),
        )
        return {
            "ok": True,
            "status": status,
            "raw_event_id": raw_event_id,
            "person": person,
            "sleep_sessions_seen": sleep_seen,
            "sleep_sessions_inserted": sleep_inserted,
            "vitals_seen": vitals_seen,
            "vitals_inserted": vitals_inserted,
            "sha256": digest,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "HealthConnectReceiver/1.0"

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log(f"{self.client_address[0]} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/healthz", "/"):
            self._send_json(200, {"ok": True, "service": "health-connect-receiver", "time": utc_now()})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) != 2 or parts[0] != "health-connect":
                self._send_json(404, {"ok": False, "error": "expected /health-connect/<person>"})
                return
            person = parts[1].strip().lower()
            if not person or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in person):
                self._send_json(400, {"ok": False, "error": "invalid person"})
                return

            token = self.headers.get("X-Webhook-Token") or (parse_qs(parsed.query).get("token") or [None])[0]
            expected = self.server.auth_token  # type: ignore[attr-defined]
            if not token or not hmac.compare_digest(token, expected):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                self._send_json(400, {"ok": False, "error": "empty body"})
                return
            max_body_mb = float(os.environ.get("HCWEBHOOK_MAX_BODY_MB", "25"))
            if length > max_body_mb * 1024 * 1024:
                self._send_json(413, {"ok": False, "error": "payload too large"})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"ok": False, "error": f"invalid json: {e}"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"ok": False, "error": "json must be object"})
                return

            result = insert_payload(
                self.server.db_path,  # type: ignore[attr-defined]
                person,
                payload,
                self.client_address[0] if self.client_address else None,
                self.headers.get("User-Agent"),
            )
            log(
                f"POST person={person} status={result['status']} "
                f"sleep_seen={result['sleep_sessions_seen']} sleep_inserted={result['sleep_sessions_inserted']} "
                f"vitals_seen={result['vitals_seen']} vitals_inserted={result['vitals_inserted']} "
                f"id={result['raw_event_id']}"
            )
            self._send_json(200, result)
        except Exception as e:
            log("ERROR " + repr(e) + "\n" + traceback.format_exc())
            self._send_json(500, {"ok": False, "error": "internal_error", "message": str(e)})


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("HCWEBHOOK_HOST", os.environ.get("HEALTH_CONNECT_RECEIVER_HOST", "0.0.0.0")))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HCWEBHOOK_PORT", os.environ.get("HEALTH_CONNECT_RECEIVER_PORT", "8787"))))
    parser.add_argument("--db", default=os.environ.get("HCWEBHOOK_DB", os.environ.get("HEALTH_CONNECT_DB", str(DEFAULT_DB))))
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser()
    init_db(db_path)
    token = load_token()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.auth_token = token  # type: ignore[attr-defined]
    httpd.db_path = db_path  # type: ignore[attr-defined]
    log(f"START host={args.host} port={args.port} db={db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("STOP")


if __name__ == "__main__":
    main()
