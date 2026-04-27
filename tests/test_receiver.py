import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hcwebhook_receiver.server import insert_payload


@pytest.fixture
def sample_payload():
    return {
        "sleep": [
            {
                "session_end_time": "2026-01-02T23:00:00Z",
                "duration_seconds": 28800,
                "stages": [
                    {"stage": "4", "start_time": "2026-01-02T15:00:00Z", "end_time": "2026-01-02T18:00:00Z"},
                    {"stage": "5", "start_time": "2026-01-02T18:00:00Z", "end_time": "2026-01-02T23:00:00Z"},
                ],
            }
        ],
        "heart_rate": [{"time": "2026-01-02T15:05:00Z", "bpm": 62}],
        "heart_rate_variability": [{"time": "2026-01-02T15:05:00Z", "rmssd_millis": 42.5}],
        "oxygen_saturation": [{"time": "2026-01-02T20:00:00Z", "percentage": 97.0}],
        "resting_heart_rate": [{"time": "2026-01-02T00:00:00Z", "bpm": 58}],
        "respiratory_rate": [{"time": "2026-01-02T20:00:00Z", "rate": 14.2}],
    }


def test_insert_payload_normalizes_sleep_and_vitals(tmp_path, sample_payload):
    db = tmp_path / "health.sqlite"
    result = insert_payload(db, "alice", sample_payload, "127.0.0.1", "pytest")

    assert result["status"] == "inserted"
    assert result["sleep_sessions_seen"] == 1
    assert result["sleep_sessions_inserted"] == 1
    assert result["vitals_seen"] == 5
    assert result["vitals_inserted"] == 5

    con = sqlite3.connect(db)
    sleep = con.execute("select session_start, session_end, duration_seconds, stage_count from sleep_sessions").fetchone()
    assert sleep == ("2026-01-02T15:00:00Z", "2026-01-02T23:00:00Z", 28800.0, 2)

    metrics = dict(con.execute("select metric, count(*) from vitals group by metric").fetchall())
    assert metrics == {
        "heart_rate": 1,
        "heart_rate_variability": 1,
        "oxygen_saturation": 1,
        "resting_heart_rate": 1,
        "respiratory_rate": 1,
    }


def test_duplicate_raw_event_is_detected(tmp_path, sample_payload):
    db = tmp_path / "health.sqlite"
    first = insert_payload(db, "alice", sample_payload, None, None)
    second = insert_payload(db, "alice", sample_payload, None, None)

    assert first["status"] == "inserted"
    assert second["status"] == "duplicate_raw"
    assert second["sleep_sessions_seen"] == 0
    assert second["vitals_seen"] == 0

    con = sqlite3.connect(db)
    assert con.execute("select count(*) from raw_events").fetchone()[0] == 1
    assert con.execute("select count(*) from sync_log").fetchone()[0] == 2


def test_sleep_start_falls_back_to_duration(tmp_path):
    db = tmp_path / "health.sqlite"
    payload = {
        "sleep": [{"session_end_time": "2026-01-02T23:00:00Z", "duration_seconds": 3600, "stages": []}]
    }
    insert_payload(db, "alice", payload, None, None)
    con = sqlite3.connect(db)
    start = con.execute("select session_start from sleep_sessions").fetchone()[0]
    assert start == "2026-01-02T22:00:00Z"
