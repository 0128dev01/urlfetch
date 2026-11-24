from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "job_link.db"
STATE_JSON_PATH = DATA_DIR / "state.json"
STATE_JSON_BAK = DATA_DIR / "state.json.bak"
ACTIVE_FILE = DATA_DIR / "active_account"


def _sanitize_email(email: str) -> str:
    text = (email or "").strip().lower()
    if not text:
        return "_default"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text)


def account_dir(email: str) -> Path:
    return DATA_DIR / _sanitize_email(email)


def db_path_for(email: str) -> Path:
    return account_dir(email) / "job_link.db"


def read_active_email() -> str:
    try:
        return ACTIVE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_active_email(email: str) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ACTIVE_FILE.write_text((email or "").strip().lower(), encoding="utf-8")


SETTING_KEYS = (
    "session_id",
    "email",
    "selected_profile",
    "selected_profiles",
    "next_position",
    "hidden_linkedin",
    "filter_chips",
    "has_more",
    "sort",
    "crawler_running",
    "crawler_status",
    "crawler_error",
    "next_update_at",
    "update_seconds",
    "applied_synced",
    "applied_hidden_linkedin",
    "full_crawl_done",
    "search_days",
    "posted_dates_backfilled",
    "date_from",
    "date_to",
    "link_host",
    "bulk_open_count",
    "display_count",
    "company_apply_days",
    "selected_profile_name",
    "history_backfilled",
    "feed_first_ids",
    "feed_recent_order",
    "feed_sort",
    "pending_tab_groups",
    "last_new_count",
    "last_applied_added",
    "last_applied_marked",
    "company_apply_from",
    "company_apply_to",
)


def connect(db_file: Path | None = None) -> sqlite3.Connection:
    path = db_file or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'new',
            posted_date TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            link_host TEXT NOT NULL DEFAULT '',
            publish_time TEXT NOT NULL DEFAULT '',
            opened_at TEXT NOT NULL DEFAULT '',
            applied_at TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_date);
        CREATE INDEX IF NOT EXISTS idx_jobs_host ON jobs(link_host);
        CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS host_visits (
            day_key TEXT NOT NULL,
            host TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day_key, host)
        );
        """
    )
    conn.commit()


def _encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(text: str, default=None):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return default


def _job_row(job: dict) -> tuple:
    job_id = str(job.get("job_id") or "")
    return (
        job_id,
        str(job.get("status") or "new"),
        str(job.get("posted_date") or ""),
        str(job.get("company") or ""),
        str(job.get("title") or ""),
        str(job.get("link_host") or ""),
        str(job.get("publish_time") or ""),
        str(job.get("opened_at") or ""),
        str(job.get("applied_at") or ""),
        _encode(job),
    )


def meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def load_state_dict(conn: sqlite3.Connection, empty_state) -> dict:
    state = empty_state()
    for row in conn.execute("SELECT key, value FROM settings"):
        state[row["key"]] = _decode(row["value"], state.get(row["key"]))

    jobs: dict[str, dict] = {}
    for row in conn.execute("SELECT job_id, data FROM jobs"):
        job = _decode(row["data"], None)
        if isinstance(job, dict) and row["job_id"]:
            jobs[str(row["job_id"])] = job
    state["jobs"] = jobs

    history = []
    for row in conn.execute("SELECT data FROM history ORDER BY id ASC"):
        item = _decode(row["data"], None)
        if isinstance(item, dict):
            history.append(item)
    state["history"] = history

    visits: dict[str, dict] = {}
    for row in conn.execute("SELECT day_key, host, count FROM host_visits"):
        bucket = visits.setdefault(str(row["day_key"]), {})
        bucket[str(row["host"])] = int(row["count"] or 0)
    state["host_visits"] = visits
    return state


def save_state_dict(conn: sqlite3.Connection, state: dict) -> None:
    jobs = state.get("jobs") or {}
    history = list(state.get("history") or [])
    visits = state.get("host_visits") or {}

    with conn:
        for key in SETTING_KEYS:
            if key not in state:
                continue
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _encode(state.get(key))),
            )

        conn.execute("DELETE FROM jobs")
        rows = [_job_row(job) for job_id, job in jobs.items() if job and job_id]
        if rows:
            conn.executemany(
                "INSERT INTO jobs("
                "job_id, status, posted_date, company, title, link_host, "
                "publish_time, opened_at, applied_at, data"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        conn.execute("DELETE FROM history")
        if history:
            conn.executemany(
                "INSERT INTO history(at, kind, data) VALUES (?, ?, ?)",
                [
                    (
                        str(item.get("at") or ""),
                        str(item.get("kind") or ""),
                        _encode(item),
                    )
                    for item in history
                    if isinstance(item, dict)
                ],
            )

        conn.execute("DELETE FROM host_visits")
        visit_rows = []
        for day_key, bucket in visits.items():
            if not isinstance(bucket, dict):
                continue
            for host, count in bucket.items():
                if not host:
                    continue
                visit_rows.append((str(day_key), str(host), int(count or 0)))
        if visit_rows:
            conn.executemany(
                "INSERT INTO host_visits(day_key, host, count) VALUES (?, ?, ?)",
                visit_rows,
            )

        meta_set(conn, "schema_version", "1")
        meta_set(conn, "migrated", "1")


def job_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    return int(row["n"] if row else 0)


def migrate_from_json(conn: sqlite3.Connection, empty_state) -> bool:
    if meta_get(conn, "migrated") == "1" and job_count(conn) > 0:
        return False
    if not STATE_JSON_PATH.exists():
        if meta_get(conn, "migrated") != "1":
            save_state_dict(conn, empty_state())
            meta_set(conn, "migrated", "1")
            conn.commit()
        return False

    try:
        data = json.loads(STATE_JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    state = empty_state()
    state.update(data)
    state["jobs"] = data.get("jobs") or {}
    state["history"] = data.get("history") or []
    state["host_visits"] = data.get("host_visits") or {}
    save_state_dict(conn, state)
    conn.commit()

    try:
        if STATE_JSON_BAK.exists():
            STATE_JSON_BAK.unlink()
        STATE_JSON_PATH.replace(STATE_JSON_BAK)
    except OSError:
        pass
    return True
