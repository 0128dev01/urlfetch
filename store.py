from __future__ import annotations

import contextvars
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, urlparse

import db as sql_db

ROOT = sql_db.ROOT
DATA_DIR = sql_db.DATA_DIR
STATE_PATH = sql_db.STATE_JSON_PATH
DB_PATH = sql_db.DB_PATH
LOCK = Lock()
_FLUSH = Event()

_request_email: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_email", default=""
)


class _AccountState:
    __slots__ = ("conn", "cache", "dirty")

    def __init__(self) -> None:
        self.conn = None
        self.cache: dict | None = None
        self.dirty = False


_ACCOUNTS: dict[str, _AccountState] = {}
_LEGACY_DONE = False


def set_request_email(email: str) -> None:
    _request_email.set((email or "").strip().lower())


def current_email() -> str:
    return _request_email.get()


def _acct() -> _AccountState:
    email = current_email()
    if email not in _ACCOUNTS:
        _ACCOUNTS[email] = _AccountState()
    return _ACCOUNTS[email]


def local_now() -> datetime:
    return datetime.now().astimezone()


def local_day(stamp: datetime | None = None) -> str:
    return (stamp or local_now()).astimezone().strftime("%Y-%m-%d")


def now() -> str:
    return local_now().isoformat()


def _parse_job_time(text: str) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    value = raw.replace("Z", "+00:00")
    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
        for size, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M"), (10, "%Y-%m-%d")):
            try:
                parsed = datetime.strptime(raw[:size], fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        if "T" not in raw and " " not in raw and len(raw) >= 10:
            return parsed
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def posted_date_from(publish_time: str = "", posted: str = "") -> str:
    parsed = _parse_job_time(publish_time)
    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.strftime("%Y-%m-%d")
        return local_day(parsed)
    extra = str(posted or "").strip()
    if len(extra) >= 10 and extra[4:5] == "-":
        return extra[:10]
    return extra


def update_interval() -> int:
    try:
        seconds = int(load_state().get("update_seconds") or 30 * 60)
    except (TypeError, ValueError):
        seconds = 30 * 60
    return max(10, min(seconds, 24 * 60 * 60))


def ensure_next_update_at(seconds: int | None = None) -> dict:
    wait = seconds if seconds is not None else update_interval()

    def mutate(state: dict) -> None:
        if not state.get("full_crawl_done") or state.get("crawler_running"):
            return
        if state.get("next_update_at"):
            return
        state["next_update_at"] = (local_now() + timedelta(seconds=wait)).isoformat()

    return update_state(mutate)


def set_update_wait(seconds: int) -> dict:
    wait = max(10, min(int(seconds), 24 * 60 * 60))

    def mutate(state: dict) -> None:
        state["update_seconds"] = wait
        state["next_update_at"] = (local_now() + timedelta(seconds=wait)).isoformat()

    return update_state(mutate)


def _empty_state() -> dict:
    return {
        "session_id": "",
        "email": "",
        "selected_profile": "",
        "selected_profiles": [],
        "next_position": 0,
        "jobs": {},
        "hidden_linkedin": 0,
        "filter_chips": [],
        "has_more": False,
        "sort": "recommended",
        "crawler_running": False,
        "crawler_status": "",
        "crawler_error": "",
        "next_update_at": "",
        "update_seconds": 30 * 60,
        "applied_synced": False,
        "applied_hidden_linkedin": 0,
        "full_crawl_done": False,
        "search_days": 0,
        "posted_dates_backfilled": False,
        "date_from": "",
        "date_to": "",
        "link_host": "",
        "link_host_query": "",
        "bulk_open_count": 10,
        "display_count": 20,
        "selected_profile_name": "",
        "history": [],
        "history_backfilled": False,
        "host_visits": {},
        "feed_first_ids": [],
        "feed_recent_order": False,
        "feed_sort": "recent",
        "pending_tab_groups": [],
    }


def _maybe_migrate_legacy() -> None:
    global _LEGACY_DONE
    if _LEGACY_DONE:
        return
    _LEGACY_DONE = True
    if sql_db.ACTIVE_FILE.exists():
        return

    email = ""
    legacy = sql_db.DB_PATH

    if legacy.exists():
        conn = sql_db.connect(legacy)
        sql_db.init_schema(conn)
        sql_db.migrate_from_json(conn, _empty_state)
        state = sql_db.load_state_dict(conn, _empty_state)
        email = (state.get("email") or "").strip().lower()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        target = sql_db.db_path_for(email)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            import shutil
            shutil.move(str(legacy), str(target))
        else:
            legacy.unlink(missing_ok=True)
        for extra in legacy.parent.glob(legacy.name + "-*"):
            try:
                extra.unlink()
            except OSError:
                pass
    elif sql_db.STATE_JSON_PATH.exists():
        try:
            import json as _json
            data = _json.loads(sql_db.STATE_JSON_PATH.read_text(encoding="utf-8"))
            email = (data.get("email") or "").strip().lower()
        except Exception:
            pass

    sql_db.write_active_email(email)


def _conn():
    acct = _acct()
    if acct.conn is None:
        _maybe_migrate_legacy()
        path = sql_db.db_path_for(current_email())
        acct.conn = sql_db.connect(path)
        sql_db.init_schema(acct.conn)
        sql_db.migrate_from_json(acct.conn, _empty_state)
    return acct.conn


def _read_state() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    return sql_db.load_state_dict(_conn(), _empty_state)


def load_state() -> dict:
    acct = _acct()
    if acct.cache is not None:
        return acct.cache
    with LOCK:
        if acct.cache is None:
            acct.cache = _read_state()
        return acct.cache


def save_state(state: dict) -> None:
    acct = _acct()
    with LOCK:
        acct.cache = state
        acct.dirty = True
        _FLUSH.set()


def flush_state() -> None:
    with LOCK:
        for acct in _ACCOUNTS.values():
            if acct.cache is not None and acct.dirty and acct.conn is not None:
                sql_db.save_state_dict(acct.conn, acct.cache)
                acct.dirty = False


def _flush_loop() -> None:
    while True:
        _FLUSH.wait()
        time.sleep(0.2)
        _FLUSH.clear()
        try:
            flush_state()
        except Exception as exc:
            print(f"state flush failed: {exc}")


def update_state(mutator) -> dict:
    acct = _acct()
    with LOCK:
        if acct.cache is None:
            acct.cache = _read_state()
        mutator(acct.cache)
        acct.dirty = True
        _FLUSH.set()
        return acct.cache


Thread(target=_flush_loop, name="job-link-state-flush", daemon=True).start()


def posted_date_of(job: dict) -> str:
    derived = posted_date_from(job.get("publish_time") or "", job.get("posted") or "")
    if derived:
        return derived
    return str(job.get("posted_date") or "").strip()


def posted_hour_of(job: dict) -> str:
    parsed = _parse_job_time(job.get("publish_time") or "")
    if parsed is None:
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%H:%M")


def posted_display_of(job: dict) -> str:
    day = posted_date_of(job)
    hour = posted_hour_of(job)
    if day and hour:
        return f"{day} {hour}"
    return day or hour


def ensure_posted_dates() -> dict:
    state = load_state()
    if state.get("posted_dates_backfilled"):
        return state

    def mutate(current: dict) -> None:
        for job in current["jobs"].values():
            date = posted_date_of(job)
            if date:
                job["posted_date"] = date
        current["posted_dates_backfilled"] = True

    return update_state(mutate)


def _posted_freshness(job: dict) -> tuple[str, str]:
    """Sort key for posted time: prefer richer publish_time, then posted_date."""
    publish_time = str(job.get("publish_time") or "").strip()
    day = posted_date_of(job)
    return (publish_time, day)


def _keep_posted_date(incoming: dict, current: dict) -> dict:
    """Merge date fields, keeping the latest posted date when the same link is refreshed."""
    merged = dict(incoming)
    if not current:
        merged["posted_date"] = posted_date_of(merged)
        return merged

    incoming_fresh = _posted_freshness(incoming)
    current_fresh = _posted_freshness(current)
    newer = incoming if incoming_fresh >= current_fresh else current
    older = current if newer is incoming else incoming

    publish_time = str(newer.get("publish_time") or "").strip() or str(older.get("publish_time") or "").strip()
    posted = str(newer.get("posted") or "").strip() or str(older.get("posted") or "").strip()
    if publish_time:
        merged["publish_time"] = publish_time
    if posted:
        merged["posted"] = posted
    merged["posted_date"] = (
        posted_date_of({"publish_time": publish_time, "posted": posted})
        or posted_date_of(newer)
        or posted_date_of(older)
    )
    return merged


def _sync_same_link_posted_dates(existing: dict, job: dict) -> None:
    """If another saved job shares this apply link, move its posted date to the latest."""
    job_id = str(job.get("job_id") or "")
    url = str(job.get("apply_url") or "").strip()
    if not url:
        return
    for other_id, other in existing.items():
        if other_id == job_id:
            continue
        other_url = str(other.get("apply_url") or "").strip()
        if not other_url or not urls_match(url, other_url):
            continue
        updated = _keep_posted_date(
            {
                "publish_time": job.get("publish_time") or "",
                "posted": job.get("posted") or "",
                "posted_date": job.get("posted_date") or "",
            },
            other,
        )
        if updated.get("publish_time"):
            other["publish_time"] = updated["publish_time"]
        if updated.get("posted"):
            other["posted"] = updated["posted"]
        if updated.get("posted_date"):
            other["posted_date"] = updated["posted_date"]


def _merge_status(job: dict, current: dict) -> dict:
    merged = {**job, "status": current.get("status") or "new"}
    if current.get("opened_at"):
        merged["opened_at"] = current["opened_at"]
        merged["opened_profile"] = current.get("opened_profile", "")
    if current.get("opened_url"):
        merged["opened_url"] = current["opened_url"]
    if current.get("opened_urls"):
        merged["opened_urls"] = current["opened_urls"]
    if current.get("applied_at") or current.get("status") == "applied":
        merged["applied_at"] = current.get("applied_at") or merged.get("applied_at")
        merged["status"] = "applied"
    elif current.get("status") == "opened":
        merged["status"] = "opened"
    return merged


def upsert_jobs(
    jobs: list[dict],
    next_position: int,
    hidden_linkedin: int,
    filter_chips: list[str],
    reset: bool,
    has_more: bool,
    prepend_new: bool = False,
) -> dict:
    def mutate(state: dict) -> None:
        existing = state["jobs"]
        if reset:
            kept = {
                job_id: job
                for job_id, job in existing.items()
                if job.get("status") in ("opened", "applied")
            }
        else:
            kept = dict(existing)

        start_rank = 0 if reset else (
            max((job.get("rank") or 0) for job in kept.values()) + 1 if kept else 0
        )
        offset = 0
        for job in jobs:
            job_id = job["job_id"]
            current = existing.get(job_id, {})
            if job_id in kept and not reset:
                incoming = _keep_posted_date(
                    {key: job[key] for key in job if key not in ("rank", "status", "appeared")},
                    kept[job_id],
                )
                kept[job_id] = {
                    **kept[job_id],
                    **incoming,
                    "status": kept[job_id].get("status") or "new",
                }
                _sync_same_link_posted_dates(kept, kept[job_id])
                continue
            kept[job_id] = {
                **_merge_status(_keep_posted_date(job, current), current),
                "rank": start_rank + offset,
                "appeared": False,
            }
            _sync_same_link_posted_dates(kept, kept[job_id])
            offset += 1
        state["jobs"] = kept
        state["next_position"] = next_position
        state["hidden_linkedin"] = hidden_linkedin if reset else state.get("hidden_linkedin", 0) + hidden_linkedin
        state["filter_chips"] = filter_chips
        state["has_more"] = has_more
        state["last_new_count"] = 0

    return update_state(mutate)


def begin_feed_reorder() -> dict:
    def mutate(state: dict) -> None:
        for job in state["jobs"].values():
            rank = job.get("rank")
            if rank is None:
                job["rank"] = 20000
            else:
                job["rank"] = int(rank) + 10000

    return update_state(mutate)


def apply_feed_order(jobs: list[dict], start_rank: int, filter_chips: list[str] | None = None) -> int:
    added = 0

    def mutate(state: dict) -> None:
        nonlocal added
        existing = state["jobs"]
        for offset, job in enumerate(jobs):
            job_id = job["job_id"]
            current = existing.get(job_id, {})
            is_new = job_id not in existing
            merged = _merge_status(_keep_posted_date(job, current), current)
            existing[job_id] = {
                **(current if current else {}),
                **merged,
                "rank": start_rank + offset,
                "appeared": True if is_new else current.get("appeared", False),
            }
            _sync_same_link_posted_dates(existing, existing[job_id])
            if is_new:
                added += 1
        state["jobs"] = existing
        if filter_chips:
            state["filter_chips"] = filter_chips
        state["last_new_count"] = added

    update_state(mutate)
    return added


def upsert_applied_jobs(jobs: list[dict], hidden_linkedin: int | None = None) -> dict:
    def mutate(state: dict) -> None:
        existing = state["jobs"]
        added = 0
        marked = 0
        for job in jobs:
            job_id = job["job_id"]
            current = existing.get(job_id, {})
            if current:
                incoming = _keep_posted_date(
                    {key: job[key] for key in job if key not in ("rank", "status", "appeared")},
                    current,
                )
                current.update(incoming)
                current["status"] = "applied"
                current["applied_at"] = current.get("applied_at") or job.get("applied_at") or now()
                current["jobright_synced"] = True
                existing[job_id] = current
                marked += 1
                continue
            existing[job_id] = {
                **_keep_posted_date(job, current),
                "status": "applied",
                "applied_at": job.get("applied_at") or now(),
                "jobright_synced": True,
                "rank": None,
                "appeared": False,
            }
            added += 1
        state["jobs"] = existing
        state["applied_synced"] = True
        if hidden_linkedin is not None:
            state["applied_hidden_linkedin"] = hidden_linkedin
        state["last_applied_added"] = added
        state["last_applied_marked"] = marked

    return update_state(mutate)


def _host_of(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _url_tokens(url: str) -> set[str]:
    parsed = urlparse(url or "")
    host = _host_of(url)
    tokens: set[str] = set()
    path = parsed.path.rstrip("/").lower()
    if path:
        specific = [
            part
            for part in path.split("/")
            if part and len(part) >= 6 and part not in _GENERIC_URL_PARTS
        ]
        if specific:
            tokens.add(path)
            tokens.update(specific)
    for values in parse_qs(parsed.query).values():
        for value in values:
            if len(value) >= 6:
                tokens.add(value.lower())
    if host:
        tokens.add(host)
    return tokens


_COMPANY_STOP = {"inc", "llc", "ltd", "corp", "corporation", "company", "co", "the", "and"}


def _fold_company(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " and "))
    return " ".join(part for part in text.split() if part not in _COMPANY_STOP)


def feed_first_ids() -> list[str]:
    ids = load_state().get("feed_first_ids") or []
    return [str(job_id) for job_id in ids if job_id]


def feed_sort() -> str:
    value = str(load_state().get("feed_sort") or "recent").strip().lower()
    return "recent" if value == "recent" else "recommended"


def feed_sort_condition() -> int:
    return 1 if feed_sort() == "recent" else 0


def set_feed_sort(value: str) -> dict:
    sort = "recent" if str(value or "").strip().lower() == "recent" else "recommended"
    return update_state(
        lambda state: state.update(
            feed_sort=sort,
            feed_first_ids=[],
            feed_recent_order=False,
        )
    )


def set_feed_first_page(ids: list[str], recent_order: bool) -> dict:
    cleaned = [str(job_id) for job_id in ids if job_id]
    return update_state(
        lambda state: state.update(
            feed_first_ids=cleaned,
            feed_recent_order=bool(recent_order),
        )
    )


def companies_match(left: str, right: str) -> bool:
    first = _fold_company(left)
    second = _fold_company(right)
    if not first or not second:
        return False
    if first == second or first in second or second in first:
        return True
    compact_first = first.replace(" ", "")
    compact_second = second.replace(" ", "")
    return compact_first == compact_second or compact_first in compact_second or compact_second in compact_first


def same_company_day_jobs(job: dict, limit: int = 15) -> list[dict]:
    company = job.get("company") or ""
    day = posted_date_of(job)
    if not company or not day:
        return [job] if job.get("apply_url") else []
    group = []
    for item in load_state()["jobs"].values():
        if item.get("status") == "applied":
            continue
        if not item.get("apply_url"):
            continue
        if posted_date_of(item) != day:
            continue
        if not companies_match(company, item.get("company") or ""):
            continue
        group.append(item)
    group.sort(key=lambda item: (item.get("job_id") != job.get("job_id"), item.get("title") or ""))
    return group[:limit]


_TAB_GROUP_COLORS = [
    ("blue", "#2563eb"),
    ("green", "#16a34a"),
    ("purple", "#7c3aed"),
    ("cyan", "#0891b2"),
    ("pink", "#db2777"),
    ("yellow", "#ca8a04"),
    ("red", "#dc2626"),
]


def company_group_key(job: dict) -> str:
    folded = _fold_company(str(job.get("company") or ""))
    day = posted_date_of(job)
    if not folded or not day:
        return ""
    return f"{folded}|{day}"


def _group_color(key: str) -> tuple[str, str]:
    if not key:
        return "", ""
    return _TAB_GROUP_COLORS[sum(ord(char) for char in key) % len(_TAB_GROUP_COLORS)]


def company_group_buckets(jobs_iter=None) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {}
    source = jobs_iter if jobs_iter is not None else load_state()["jobs"].values()
    for item in source:
        if item.get("status") == "applied" or not item.get("apply_url"):
            continue
        key = company_group_key(item)
        if not key:
            continue
        buckets.setdefault(key, []).append(item)
    return buckets


def annotate_company_groups(jobs: list[dict], buckets: dict[str, list[dict]] | None = None) -> list[dict]:
    group_map = buckets if buckets is not None else company_group_buckets()
    for job in jobs:
        key = company_group_key(job)
        group = group_map.get(key) or []
        size = len(group)
        tab_color, hex_color = _group_color(key) if size > 1 else ("", "")
        job["company_group_key"] = key if size > 1 else ""
        job["company_group_size"] = size
        job["company_group_color"] = hex_color
        job["company_group_tab_color"] = tab_color
        job["company_group_index"] = next(
            (index + 1 for index, item in enumerate(group) if item.get("job_id") == job.get("job_id")),
            1,
        )
    return jobs


def remember_tab_group(jobs: list[dict]) -> None:
    if len(jobs) < 2:
        return
    key = company_group_key(jobs[0])
    tab_color, hex_color = _group_color(key)
    entry = {
        "id": f"{key}:{now()}",
        "company": str(jobs[0].get("company") or "Same company"),
        "color": tab_color or "blue",
        "hex": hex_color,
        "urls": [str(job.get("apply_url") or "") for job in jobs if job.get("apply_url")],
        "at": now(),
    }

    def mutate(state: dict) -> None:
        items = [
            item
            for item in (state.get("pending_tab_groups") or [])
            if _tab_group_fresh(item)
        ]
        items.append(entry)
        state["pending_tab_groups"] = items[-30:]

    update_state(mutate)


def pending_tab_groups() -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for job in load_state()["jobs"].values():
        if job.get("status") != "opened":
            continue
        key = company_group_key(job)
        if not key:
            continue
        buckets.setdefault(key, []).append(job)
    groups: list[dict] = []
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        tab_color, hex_color = _group_color(key)
        urls: list[str] = []
        seen: set[str] = set()
        for job in items:
            for url in (
                str(job.get("apply_url") or ""),
                str(job.get("opened_url") or ""),
                *(job.get("opened_urls") or []),
            ):
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
        groups.append(
            {
                "id": key,
                "company": str(items[0].get("company") or "Same company"),
                "color": tab_color or "blue",
                "hex": hex_color,
                "urls": urls,
            }
        )
    return groups


def _tab_group_fresh(item: dict) -> bool:
    parsed = _parse_job_time(str(item.get("at") or ""))
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_now().tzinfo)
    return (local_now() - parsed).total_seconds() <= 180


_GENERIC_URL_PARTS = {
    "apply",
    "application",
    "jobs",
    "job",
    "job-app",
    "job_app",
    "career",
    "careers",
    "boards",
    "embed",
    "portal",
    "view",
    "detail",
    "job-detail",
    "job-board",
    "posting",
    "listings",
    "search",
    "home",
    "index",
    "en-us",
    "en",
    "www",
    "info",
}


_STRONG_QUERY = {"token", "gh_jid", "jobid", "job_id", "postingid", "posting_id"}
_SKIP_QUERY = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "src", "source", "gh_src"}


def _normalize_url(url: str) -> str:
    parsed = urlparse((url or "").split("#")[0].strip())
    query = [
        (key.lower(), value)
        for key, values in parse_qs(parsed.query, keep_blank_values=False).items()
        if key.lower() not in _SKIP_QUERY
        for value in values
    ]
    query.sort()
    host = _host_of(url)
    path = parsed.path.rstrip("/").lower()
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _job_keys(url: str) -> set[str]:
    text = (url or "").lower()
    keys: set[str] = set()
    for uuid in re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        text,
    ):
        keys.add(f"id:{uuid}")
    parsed = urlparse(url or "")
    query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
    for name in _STRONG_QUERY:
        for value in query.get(name, []):
            if value:
                keys.add(f"qid:{value.lower()}")
                if value.isdigit() and len(value) >= 5:
                    keys.add(f"num:{value}")
    for number in re.findall(r"\d{5,}", text):
        keys.add(f"num:{number}")
    for part in parsed.path.split("/"):
        part = part.lower()
        if len(part) >= 8 and part not in _GENERIC_URL_PARTS and not part.isdigit():
            keys.add(f"path:{part}")
    return keys


def _strong_keys(url: str) -> set[str]:
    return {key for key in _job_keys(url) if key.startswith(("id:", "num:", "qid:"))}


def urls_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if _normalize_url(left) == _normalize_url(right):
        return True
    shared_strong = _strong_keys(left) & _strong_keys(right)
    if shared_strong:
        return True
    if _strong_keys(left) and _strong_keys(right):
        return False
    shared_keys = _job_keys(left) & _job_keys(right)
    if any(key.startswith("path:") and len(key) >= 16 for key in shared_keys):
        return True
    if _host_of(left) != _host_of(right):
        return False
    left_tokens = _url_tokens(left)
    right_tokens = _url_tokens(right)
    shared = left_tokens & right_tokens
    return any(token != _host_of(left) for token in shared)


def _job_urls(job: dict) -> list[str]:
    urls = [job.get("apply_url") or "", job.get("jobright_url") or "", job.get("opened_url") or ""]
    urls.extend(job.get("opened_urls") or [])
    return [url for url in urls if url]


def job_matches_page(job: dict, url: str) -> bool:
    return any(urls_match(known, url) for known in _job_urls(job))


HISTORY_LIMIT = 250


def _history_profile_name(directory: str, name: str = "") -> str:
    if name.strip():
        return name.strip()
    state = load_state()
    if directory and directory == state.get("selected_profile"):
        return str(state.get("selected_profile_name") or directory)
    return directory or ""


def ensure_history() -> dict:
    state = load_state()
    if state.get("history_backfilled"):
        return state

    def mutate(current: dict) -> None:
        if current.get("history_backfilled"):
            return
        events = []
        for job in current.get("jobs", {}).values():
            stamp = job.get("opened_at") or ""
            if not stamp:
                continue
            events.append(
                {
                    "kind": "visit",
                    "at": stamp,
                    "profile": job.get("opened_profile") or current.get("selected_profile") or "",
                    "name": job.get("opened_profile") or current.get("selected_profile_name") or "",
                    "job_id": job.get("job_id") or "",
                    "title": job.get("title") or "",
                    "company": job.get("company") or "",
                    "url": job.get("apply_url") or job.get("opened_url") or "",
                    "host": link_host_of(job),
                }
            )
        events.sort(key=lambda item: item.get("at") or "", reverse=True)
        current["history"] = events[:HISTORY_LIMIT]
        current["history_backfilled"] = True

    return update_state(mutate)


def history(limit: int = 80) -> list[dict]:
    ensure_history()
    items = list(load_state().get("history") or [])
    return items[: max(1, min(int(limit or 80), HISTORY_LIMIT))]


def selected_profiles(state: dict | None = None) -> list[str]:
    current = state or load_state()
    items = [
        str(item).strip()
        for item in (current.get("selected_profiles") or [])
        if str(item).strip()
    ]
    primary = str(current.get("selected_profile") or "").strip()
    if primary and primary not in items:
        items.insert(0, primary)
    # de-dupe while keeping order
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def select_profiles(directories: list[str], names: dict[str, str] | None = None) -> dict:
    labels = names or {}
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in directories or []:
        directory = str(item or "").strip()
        if not directory or directory in seen:
            continue
        seen.add(directory)
        cleaned.append(directory)

    def mutate(state: dict) -> None:
        previous = selected_profiles(state)
        state["selected_profiles"] = cleaned
        primary = cleaned[0] if cleaned else ""
        state["selected_profile"] = primary
        if primary:
            state["selected_profile_name"] = labels.get(primary) or _history_profile_name(
                primary, labels.get(primary, "")
            )
        else:
            state["selected_profile_name"] = ""
        if not cleaned or cleaned == previous:
            return
        items = list(state.get("history") or [])
        for directory in cleaned:
            if items and items[0].get("kind") == "profile" and items[0].get("profile") == directory:
                continue
            items.insert(
                0,
                {
                    "kind": "profile",
                    "at": now(),
                    "profile": directory,
                    "name": labels.get(directory) or _history_profile_name(directory, ""),
                },
            )
        state["history"] = items[:HISTORY_LIMIT]

    return update_state(mutate)


def select_profile(directory: str, name: str = "") -> dict:
    directory = (directory or "").strip()
    label = name.strip() or _history_profile_name(directory, name)
    current = selected_profiles()
    if directory:
        if directory in current:
            ordered = [directory] + [item for item in current if item != directory]
        else:
            ordered = current + [directory]
    else:
        ordered = []
    names = {directory: label} if directory else {}
    return select_profiles(ordered, names)


def date_visit_key(state: dict | None = None) -> str:
    current = state or load_state()
    start = str(current.get("date_from") or "").strip()
    end = str(current.get("date_to") or "").strip()
    if start and end:
        return start if start == end else f"{start}:{end}"
    return start or end or ""


def host_visits_for_date(state: dict | None = None) -> dict[str, int]:
    current = state or load_state()
    bucket = ((current.get("host_visits") or {}).get(date_visit_key(current)) or {})
    return {str(host): int(count or 0) for host, count in bucket.items() if host}


def record_visits(jobs: list[dict], profile: str, name: str = "") -> None:
    label = name.strip() or _history_profile_name(profile, name)
    rows = [job for job in jobs if job]
    if not rows:
        return

    def mutate(state: dict) -> None:
        items = list(state.get("history") or [])
        stamp = now()
        visits = state.setdefault("host_visits", {})
        day_key = date_visit_key(state)
        bucket = visits.setdefault(day_key, {})
        for job in rows:
            host = link_host_of(job)
            if host:
                bucket[host] = int(bucket.get(host) or 0) + 1
            items.insert(
                0,
                {
                    "kind": "visit",
                    "at": stamp,
                    "profile": profile,
                    "name": label or profile,
                    "job_id": job.get("job_id") or "",
                    "title": job.get("title") or "",
                    "company": job.get("company") or "",
                    "url": job.get("apply_url") or job.get("opened_url") or "",
                    "host": host,
                },
            )
        state["history"] = items[:HISTORY_LIMIT]

    update_state(mutate)


def remember_opened_url(job_id: str, url: str) -> None:
    page = (url or "").strip()
    if not page:
        return

    def mutate(state: dict) -> None:
        job = state["jobs"].get(job_id)
        if not job:
            return
        job["opened_url"] = page
        seen = [item for item in (job.get("opened_urls") or []) if item]
        if page not in seen:
            seen.append(page)
        job["opened_urls"] = seen[-8:]

    update_state(mutate)


def attach_opened_urls(url: str, previous: str = "") -> dict | None:
    previous = (previous or "").strip()
    url = (url or "").strip()
    previous_job = find_opened_job_by_url(previous, "") if previous else None
    url_job = find_opened_job_by_url(url, "") if url else None
    if url_job and previous_job and url_job.get("job_id") != previous_job.get("job_id"):
        remember_opened_url(url_job["job_id"], url)
        return url_job
    job = previous_job or url_job
    if not job:
        return None
    if previous:
        remember_opened_url(job["job_id"], previous)
    if url:
        remember_opened_url(job["job_id"], url)
    return job


def find_opened_job_by_url(url: str, company: str = "") -> dict | None:
    jobs = list(load_state()["jobs"].values())
    matches = [job for job in jobs if job_matches_page(job, url)]
    if company.strip():
        named = [job for job in matches if companies_match(company, job.get("company") or "")]
        if named:
            matches = named
        else:
            host = _host_of(url)
            host_named = [
                job
                for job in jobs
                if companies_match(company, job.get("company") or "")
                and (
                    not host
                    or host in {_host_of(item) for item in _job_urls(job)}
                )
            ]
            if host_named:
                matches = host_named
    if not matches:
        matches = [job for job in _fallback_opened_jobs(url, company) if job]
    if not matches:
        return None
    live = [job for job in matches if job.get("status") != "applied"]
    pool = live or matches
    opened = [job for job in pool if job.get("status") == "opened" or job.get("opened_at")]
    pool = opened or pool
    pool.sort(key=lambda job: job.get("opened_at") or "", reverse=True)
    return pool[0]


def _fallback_opened_jobs(url: str, company: str = "") -> list[dict]:
    opened = [
        job
        for job in load_state()["jobs"].values()
        if job.get("status") == "opened"
    ]
    if company.strip():
        named = [job for job in opened if companies_match(company, job.get("company") or "")]
        if len(named) == 1:
            return named
    host = _host_of(url)
    if host:
        same_host = [
            job
            for job in opened
            if host in {_host_of(item) for item in _job_urls(job)}
        ]
        if len(same_host) == 1:
            return same_host
    if len(opened) == 1:
        return opened
    return []


def set_job_status(job_id: str, status: str, extra: dict | None = None) -> dict:
    extra = extra or {}

    def mutate(state: dict) -> None:
        job = state["jobs"].get(job_id)
        if not job:
            raise KeyError(job_id)
        job["status"] = status
        job.update(extra)

    return update_state(mutate)


def link_host_of(job: dict) -> str:
    cached = str(job.get("link_host") or "").strip().lower()
    if cached:
        return cached[4:] if cached.startswith("www.") else cached
    host = urlparse(job.get("apply_url") or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _normalize_host(link_host: str = "") -> str:
    host = (link_host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _normalize_host_query(query: str = "") -> str:
    return str(query or "").strip().lower()


def host_matches_filter(host: str, link_host: str = "", link_host_query: str = "") -> bool:
    host_filter = _normalize_host(link_host)
    host_query = _normalize_host_query(link_host_query)
    value = _normalize_host(host)
    if host_filter:
        return value == host_filter
    if host_query:
        return host_query in value
    return True


def _in_posted_range(job: dict, date_from: str = "", date_to: str = "") -> bool:
    start = (date_from or "").strip()
    end = (date_to or "").strip()
    if not start and not end:
        return True
    posted = posted_date_of(job)
    if start and (not posted or posted < start):
        return False
    if end and (not posted or posted > end):
        return False
    return True


def _job_tab(job: dict) -> str:
    status = str(job.get("status") or "new")
    return status if status in ("opened", "applied") else "new"


SKIPPED_APPLIED_SOURCES = frozenset({"linkedin", "title-filter"})


def is_skipped_applied(job: dict) -> bool:
    if _job_tab(job) != "applied":
        return False
    source = str(job.get("applied_source") or "").strip().lower()
    if source in SKIPPED_APPLIED_SOURCES:
        return True
    host = _normalize_host(str(job.get("link_host") or ""))
    if not host:
        host = urlparse(job.get("apply_url") or "").netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    return "linkedin." in host or host == "lnkd.in"


def applied_kind_of(job: dict) -> str:
    if _job_tab(job) != "applied":
        return ""
    return "skipped" if is_skipped_applied(job) else "applied"


def applied_label_of(job: dict) -> str:
    if _job_tab(job) != "applied":
        return ""
    source = str(job.get("applied_source") or "").strip().lower()
    host = _normalize_host(str(job.get("link_host") or ""))
    if not host:
        host = urlparse(job.get("apply_url") or "").netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    if source == "title-filter":
        return "Skipped · Title"
    if source == "linkedin" or "linkedin." in host or host == "lnkd.in":
        return "Skipped · LinkedIn"
    return "Applied"


def _sort_jobs(jobs: list[dict], sort: str) -> None:
    if sort == "match":
        jobs.sort(key=lambda job: (-(job.get("match") or 0), job.get("company", "")))
    elif sort == "company":
        jobs.sort(key=lambda job: (job.get("company") or "").lower())
    elif sort == "title":
        jobs.sort(key=lambda job: (job.get("title") or "").lower())
    elif sort == "newest":
        jobs.sort(key=lambda job: job.get("publish_time") or job.get("posted_date") or "", reverse=True)
    else:
        jobs.sort(key=lambda job: (job.get("rank") is None, job.get("rank") or 0))


def _prepare_job_fields(job: dict) -> tuple[str, str]:
    day = str(job.get("posted_date") or "").strip()
    if not day:
        day = posted_date_of(job)
        if day:
            job["posted_date"] = day
    host = _normalize_host(str(job.get("link_host") or ""))
    if not host:
        host = urlparse(job.get("apply_url") or "").netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            job["link_host"] = host
    return day, host


def _finish_job_fields(job: dict) -> None:
    day, host = _prepare_job_fields(job)
    job["posted_date"] = day
    job["link_host"] = host
    job["posted_hour"] = posted_hour_of(job)
    job["posted_at"] = posted_display_of(job)


def build_state_view(
    tab: str = "new",
    sort: str | None = None,
    date_from: str = "",
    date_to: str = "",
    link_host: str = "",
    link_host_query: str = "",
    query: str = "",
    offset: int = 0,
    limit: int | None = None,
    include_jobs: bool = True,
) -> dict:
    state = load_state()
    tab_key = tab if tab in ("opened", "applied") else "new"
    sort_key = sort or state.get("sort") or "recommended"
    host_filter = _normalize_host(link_host)
    host_query = _normalize_host_query(link_host_query)
    if host_filter:
        host_query = ""
    search = (query or "").strip().lower()
    start = max(0, int(offset or 0))
    # None = all jobs, 0 = all jobs (display All), >0 = page size
    page_size = None if limit is None else max(0, int(limit))

    counts = {"new": 0, "opened": 0, "applied": 0, "all": 0}
    shown = {"new": 0, "opened": 0, "applied": 0}
    day_totals: Counter[str] = Counter()
    day_visited: Counter[str] = Counter()
    host_counts: Counter[str] = Counter()
    filtered: list[dict] = []
    date_min = ""
    date_max = ""
    linkedin_applied = 0
    title_skipped = 0
    skipped_applied = 0
    real_applied = 0
    total = 0

    for job in state["jobs"].values():
        day, host = _prepare_job_fields(job)
        if day:
            if not date_min or day < date_min:
                date_min = day
            if not date_max or day > date_max:
                date_max = day
            day_totals[day] += 1
            if job.get("status") in ("opened", "applied"):
                day_visited[day] += 1

        status = _job_tab(job)

        in_range = True
        if date_from or date_to:
            in_range = _in_posted_range(job, date_from, date_to)
        if in_range:
            counts[status] += 1
            counts["all"] += 1
            if status == "applied":
                if is_skipped_applied(job):
                    skipped_applied += 1
                    source = str(job.get("applied_source") or "").strip().lower()
                    if source == "title-filter":
                        title_skipped += 1
                    else:
                        linkedin_applied += 1
                else:
                    real_applied += 1

        matches_host = host_matches_filter(host, host_filter, host_query)
        if in_range and matches_host:
            shown[status] += 1

        if status == tab_key and in_range and host:
            host_counts[host] += 1

        if status != tab_key or not in_range or not matches_host:
            continue
        if search:
            title = str(job.get("title") or "").lower()
            company = str(job.get("company") or "").lower()
            if search not in title and search not in company:
                continue
        total += 1
        if include_jobs:
            filtered.append(job)

    page: list[dict] = []
    if include_jobs:
        _sort_jobs(filtered, sort_key)
        if page_size == 0 or page_size is None:
            page = filtered[start:]
        else:
            page = filtered[start : start + page_size]
        page_keys = {
            company_group_key(job)
            for job in page
            if job.get("status") != "applied" and job.get("apply_url")
        }
        page_keys.discard("")
        group_buckets: dict[str, list[dict]] = {}
        if page_keys:
            for item in state["jobs"].values():
                if item.get("status") == "applied" or not item.get("apply_url"):
                    continue
                key = company_group_key(item)
                if key in page_keys:
                    group_buckets.setdefault(key, []).append(item)
        for job in page:
            _finish_job_fields(job)
            if tab_key == "applied":
                job["applied_kind"] = applied_kind_of(job)
                job["applied_label"] = applied_label_of(job)
        annotate_company_groups(page, group_buckets)

    visits = host_visits_for_date(state)
    hosts = [
        {"host": host, "count": count, "visited": int(visits.get(host) or 0)}
        for host, count in sorted(host_counts.items(), key=lambda item: (-item[1], item[0]))
        if count
    ]

    return {
        "counts": {
            **counts,
            "hidden_linkedin": state.get("hidden_linkedin", 0),
            "linkedin_applied": linkedin_applied,
            "title_skipped": title_skipped,
            "skipped_applied": skipped_applied,
            "real_applied": real_applied,
        },
        "shown": shown,
        "posted_days": {
            day: {"count": day_totals[day], "visited": day_visited[day]} for day in day_totals
        },
        "date_min": date_min,
        "date_max": date_max,
        "link_hosts": hosts,
        "jobs": page,
        "job_total": total,
        "job_offset": start,
        "job_limit": page_size,
        "has_more_jobs": (start + len(page)) < total if include_jobs else False,
    }


def posted_date_bounds() -> tuple[str, str]:
    date_min = ""
    date_max = ""
    for job in load_state()["jobs"].values():
        day = posted_date_of(job)
        if not day:
            continue
        if not date_min or day < date_min:
            date_min = day
        if not date_max or day > date_max:
            date_max = day
    return date_min, date_max


def jobs_by_tab(
    tab: str,
    sort: str | None = None,
    date_from: str = "",
    date_to: str = "",
    link_host: str = "",
    link_host_query: str = "",
) -> list[dict]:
    return build_state_view(
        tab=tab,
        sort=sort,
        date_from=date_from,
        date_to=date_to,
        link_host=link_host,
        link_host_query=link_host_query,
        include_jobs=True,
    )["jobs"]


def posted_day_counts() -> dict[str, dict[str, int]]:
    return build_state_view(include_jobs=False)["posted_days"]


def link_hosts(tab: str, date_from: str = "", date_to: str = "") -> list[dict]:
    return build_state_view(
        tab=tab,
        date_from=date_from,
        date_to=date_to,
        include_jobs=False,
    )["link_hosts"]


def counts(date_from: str = "", date_to: str = "") -> dict:
    return build_state_view(
        date_from=date_from,
        date_to=date_to,
        include_jobs=False,
    )["counts"]
