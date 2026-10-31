"""Shared fixtures for the notification-sound tests."""

import json
import plistlib
import sqlite3
from pathlib import Path

import grokbot_notify_sounds as gns

BUNDLE = "com.example.grokbot"

SCHEMA = """
CREATE TABLE app (
    app_id INTEGER PRIMARY KEY,
    identifier TEXT
);
CREATE TABLE record (
    rec_id INTEGER PRIMARY KEY,
    app_id INTEGER,
    data BLOB,
    delivered_date REAL
);
"""


def config_with(rules, **overrides):
    data = {
        "enabled": True,
        "poll_interval_seconds": 2,
        "bundle_id": BUNDLE,
        "master_volume": 100,
        "default": {"sound": "Tink", "volume": 40},
        "rules": rules,
    }
    data.update(overrides)
    return gns.normalize_config(data)


def notification_blob(title="", subtitle="", body=""):
    return plistlib.dumps(
        {"req": {"titl": title, "subt": subtitle, "body": body}},
        fmt=plistlib.FMT_BINARY,
    )


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def insert_app(path: Path, app_id: int, identifier: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO app (app_id, identifier) VALUES (?, ?)",
            (app_id, identifier),
        )
        conn.commit()
    finally:
        conn.close()


def insert_record(path: Path, rec_id: int, app_id: int, title="", subtitle="", body="", delivered=1.0) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO record (rec_id, app_id, data, delivered_date) VALUES (?, ?, ?, ?)",
            (rec_id, app_id, notification_blob(title, subtitle, body), delivered),
        )
        conn.commit()
    finally:
        conn.close()


def insert_raw(path: Path, rec_id: int, app_id: int, data: bytes) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO record (rec_id, app_id, data, delivered_date) VALUES (?, ?, ?, ?)",
            (rec_id, app_id, data, 1.0),
        )
        conn.commit()
    finally:
        conn.close()


def read_state(path: Path) -> int:
    return json.loads(path.read_text(encoding="utf-8"))["last_seen"]
