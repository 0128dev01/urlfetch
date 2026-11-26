from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import jobright_client
import store

PAGE_SIZE = 20
RETRY_SECONDS = 30
UPDATE_SECONDS = 30 * 60
EMPTY_STOP = 2
NEW_EMPTY_STOP = 3
MAX_POSITION = 4000

_stop = threading.Event()
_wake = threading.Event()
_lock = threading.Lock()
_thread: threading.Thread | None = None
_busy = False
_crawler_email: str = ""


def _now() -> datetime:
    return store.local_now()


def running() -> bool:
    return (
        _thread is not None
        and _thread.is_alive()
        and _crawler_email == store.current_email()
    )


def busy() -> bool:
    return _busy and _crawler_email == store.current_email()


def _set_status(
    message: str,
    error: str = "",
    next_update: str = "",
    busy: bool | None = None,
) -> None:
    global _busy
    if busy is not None:
        _busy = busy

    def mutate(state: dict) -> None:
        state["crawler_running"] = _busy
        state["crawler_status"] = message
        state["crawler_error"] = error
        if next_update:
            state["next_update_at"] = next_update

    store.update_state(mutate)


def _wait(seconds: int) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _stop.is_set():
            return "stop"
        if _wake.is_set():
            _wake.clear()
            return "wake"
        _stop.wait(min(1.0, max(0.05, deadline - time.monotonic())))
    if _stop.is_set():
        return "stop"
    if _wake.is_set():
        _wake.clear()
        return "wake"
    return "timeout"


def _wait_until_update() -> str:
    while not _stop.is_set():
        if _wake.is_set():
            _wake.clear()
            return "wake"
        stamp = store.load_state().get("next_update_at") or ""
        if not stamp:
            return "timeout"
        try:
            deadline = datetime.fromisoformat(stamp)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except ValueError:
            return "timeout"
        remain = (deadline - _now()).total_seconds()
        if remain <= 0:
            return "timeout"
        _stop.wait(min(1.0, remain))
    return "stop"


def _fetch(session_id: str, position: int, refresh: bool) -> tuple[list[dict], list[dict], int, list[dict]]:
    while not _stop.is_set():
        try:
            return jobright_client.fetch_recommendations(
                session_id,
                position,
                PAGE_SIZE,
                refresh,
                store.feed_sort_condition(),
            )
        except jobright_client.JobrightError as exc:
            message = str(exc)
            if "expired" in message.lower():
                raise
            _set_status(f"Request failed at {position}. Retrying in {RETRY_SECONDS}s. {message}", message)
            _wait(RETRY_SECONDS)
    return [], [], 0, []


def _job_stamp(job: dict) -> str:
    parsed = store._parse_job_time(str(job.get("publish_time") or ""))
    if parsed is not None:
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return str(job.get("posted_date") or "")


def is_most_recent_order(jobs: list[dict]) -> bool:
    stamps = [stamp for stamp in (_job_stamp(job) for job in jobs) if stamp]
    if len(stamps) < 8:
        return False
    inversions = sum(1 for first, second in zip(stamps, stamps[1:]) if first < second)
    return inversions <= 1


def _page_ids(jobs: list[dict]) -> list[str]:
    return [str(job.get("job_id") or "") for job in jobs if job.get("job_id")]


def _previous_first_ids() -> set[str]:
    state = store.load_state()
    if store.feed_sort() != "recent" or not state.get("feed_recent_order"):
        return set()
    return set(store.feed_first_ids())


def _send_linkedin_applied(session_id: str, linkedin_jobs: list[dict]) -> int:
    if not linkedin_jobs:
        return 0
    existing = store.load_state().get("jobs") or {}
    pending = []
    for job in linkedin_jobs:
        current = existing.get(job["job_id"]) or {}
        if current.get("status") == "applied" and current.get("jobright_synced"):
            continue
        pending.append((job, current))
    if not pending:
        return 0

    prepared = []
    for job, current in pending:
        if _stop.is_set():
            break
        synced = False
        try:
            jobright_client.mark_applied(session_id, job["job_id"])
            synced = True
        except jobright_client.JobrightError:
            synced = False
        prepared.append(
            {
                **job,
                "status": "applied",
                "applied_at": current.get("applied_at") or store.now(),
                "jobright_synced": synced,
                "applied_source": jobright_client.auto_apply_source(job) or "linkedin",
            }
        )
    if prepared:
        store.upsert_applied_jobs(prepared)
        store.update_state(
            lambda state: state.update(
                hidden_linkedin=state.get("hidden_linkedin", 0) + len(prepared)
            )
        )
    return len(prepared)


def _queue_jobright_applies(jobs: list[dict]) -> None:
    if not jobs:
        return
    try:
        from app import _queue_jobright_apply
    except Exception:
        return
    for job in jobs:
        job_id = job.get("job_id") or ""
        if not job_id or job.get("jobright_synced"):
            continue
        _queue_jobright_apply(job_id)


def _apply_title_filters(_session_id: str = "") -> int:
    """Re-check saved jobs so title skip keys also catch older New/Opened rows."""
    marked = store.apply_title_skip_filters()
    _queue_jobright_applies(marked)
    return len(marked)


def _session() -> str:
    session_id = store.load_state().get("session_id") or ""
    if not session_id:
        raise jobright_client.JobrightError("Connect your Jobright account first.")
    return session_id


def _sync_applied(session_id: str) -> tuple[int, int]:
    _set_status("Loading applied jobs from Jobright once...")
    while not _stop.is_set():
        try:
            jobs, hidden = jobright_client.fetch_applied_jobs(session_id)
            break
        except jobright_client.JobrightError as exc:
            message = str(exc)
            if "expired" in message.lower():
                raise
            _set_status(f"Applied-list request failed. Retrying in {RETRY_SECONDS}s. {message}", message)
            _wait(RETRY_SECONDS)
    else:
        return 0, 0
    store.upsert_applied_jobs(jobs, 0)
    linkedin = sum(1 for job in jobs if job.get("applied_source") == "linkedin")
    _set_status(
        f"Imported {len(jobs)} applied jobs from Jobright, including {linkedin} LinkedIn."
    )
    return len(jobs), hidden


def _full_crawl(session_id: str) -> None:
    filters = jobright_client.get_filters(session_id)
    chips = jobright_client.summarize_filters(filters)
    store.update_state(lambda state: state.update(search_days=int(filters.get("daysAgo") or 0)))
    position = 0
    refresh = True
    empty_pages = 0
    first = True
    linkedin_applied = 0
    while not _stop.is_set() and position <= MAX_POSITION:
        _set_status(f"Loading recommended jobs at {position}...")
        jobs, linkedin, raw, ordered = _fetch(session_id, position, refresh)
        refresh = False
        if raw == 0:
            empty_pages += 1
            if empty_pages >= EMPTY_STOP:
                break
        else:
            empty_pages = 0
            if first and ordered:
                store.set_feed_first_page(_page_ids(ordered), is_most_recent_order(ordered))
            linkedin_applied += _send_linkedin_applied(session_id, linkedin)
            store.upsert_jobs(jobs, position + PAGE_SIZE, 0, chips, first, True)
            first = False
        position += PAGE_SIZE
        counts = store.counts()
        extra = (
            f" {linkedin_applied} LinkedIn/title-filtered applications sent to Applied."
            if linkedin_applied
            else ""
        )
        _set_status(
            f"Loaded {counts['all']} company links in your selected period.{extra}"
        )
    title_applied = _apply_title_filters(session_id)
    if title_applied:
        linkedin_applied += title_applied
    store.update_state(
        lambda state: state.update(
            has_more=False,
            next_position=position,
            full_crawl_done=True,
            hidden_linkedin=linkedin_applied,
        )
    )


def _scan_new(session_id: str) -> int:
    filters = jobright_client.get_filters(session_id)
    chips = jobright_client.summarize_filters(filters)
    store.update_state(lambda state: state.update(search_days=int(filters.get("daysAgo") or 0)))
    position = 0
    feed_rank = 0
    empty_new = 0
    empty_pages = 0
    added = 0
    first_ids: list[str] = []
    recent_mode = False
    stop_reason = "original"
    last_first = _previous_first_ids()
    use_first_list = bool(last_first)
    store.begin_feed_reorder()
    while not _stop.is_set() and position <= MAX_POSITION:
        _set_status(f"Checking for new links at {position}...")
        jobs, linkedin, raw, ordered = _fetch(session_id, position, position == 0)
        if raw == 0:
            empty_pages += 1
            if empty_pages >= EMPTY_STOP:
                stop_reason = "empty"
                break
            position += PAGE_SIZE
            continue
        empty_pages = 0
        if position == 0:
            first_ids = _page_ids(ordered)
            recent_mode = is_most_recent_order(ordered)
            use_first_list = recent_mode and bool(last_first)
        _send_linkedin_applied(session_id, linkedin)
        page_added = store.apply_feed_order(jobs, feed_rank, chips)
        added += page_added
        feed_rank += len(jobs)
        page_ids = set(_page_ids(ordered))
        if use_first_list and (page_ids & last_first):
            stop_reason = "first-list"
            break
        if page_added:
            empty_new = 0
        else:
            empty_new += 1
            if empty_new >= NEW_EMPTY_STOP and position >= PAGE_SIZE * 10:
                stop_reason = "original"
                break
        position += PAGE_SIZE
    if first_ids:
        store.set_feed_first_page(first_ids, recent_mode)
    title_applied = _apply_title_filters(session_id)
    extra = ""
    if title_applied:
        extra += f" {title_applied} title-filtered links sent to Applied."
    if stop_reason == "first-list":
        extra += " Stopped at last first list."
    elif not recent_mode:
        extra += " Feed was not most-recent order, used original Update."
    _set_status(f"New-link update finished. Added {added} links.{extra}")
    return added


def _loop(email: str = "") -> None:
    if email:
        store.set_request_email(email)
    try:
        session_id = _session()
        _set_status("Starting full recommend crawl...", busy=True)
        _sync_applied(session_id)
        _full_crawl(session_id)
        counts = store.counts()
        _set_status(
            f"All links loaded ({counts['all']} company links). Click Update for new links.",
            busy=False,
        )
        while not _stop.is_set():
            seconds = store.update_interval()
            nxt = (_now() + timedelta(seconds=seconds)).isoformat()
            _set_status(
                f"All links loaded ({counts['all']} company links). "
                "Click Update, or wait for the timer.",
                next_update=nxt,
                busy=False,
            )
            result = _wait_until_update()
            if result == "stop":
                break
            session_id = _session()
            _set_status("Checking for new links...", busy=True)
            added = _scan_new(session_id)
            counts = store.counts()
            seconds = store.update_interval()
            nxt = (_now() + timedelta(seconds=seconds)).isoformat()
            _set_status(f"Update finished. Added {added} new links.", next_update=nxt, busy=False)
    except jobright_client.JobrightError as exc:
        _set_status("Crawler stopped.", str(exc), busy=False)
    except Exception as exc:
        _set_status("Crawler stopped.", str(exc), busy=False)
    finally:
        def mutate(state: dict) -> None:
            state["crawler_running"] = False
            if not state.get("crawler_error"):
                state["crawler_status"] = state.get("crawler_status") or "Stopped."

        store.update_state(mutate)


def start() -> None:
    global _thread, _crawler_email
    email = store.current_email()
    with _lock:
        if _busy and _crawler_email == email:
            return
        if _thread is not None and _thread.is_alive() and _crawler_email == email:
            return
        if _thread is not None and _thread.is_alive():
            _stop.set()
            _wake.set()
            _thread.join(timeout=2)
        _stop.clear()
        _wake.clear()
        _crawler_email = email
        store.update_state(
            lambda state: state.update(
                crawler_running=True,
                crawler_status="Starting full recommend crawl...",
                crawler_error="",
                next_update_at="",
                full_crawl_done=False,
            )
        )
        _thread = threading.Thread(
            target=_loop, args=(email,), name="jobright-crawler", daemon=True
        )
        _thread.start()


def update() -> None:
    global _thread, _crawler_email
    email = store.current_email()
    with _lock:
        if _busy and _crawler_email == email:
            return
        if _thread is not None and _thread.is_alive() and _crawler_email == email:
            _set_status("Checking for new links...", busy=True)
            _wake.set()
            return
        if _thread is not None and _thread.is_alive():
            _stop.set()
            _wake.set()
            _thread.join(timeout=2)
        _stop.clear()
        _wake.clear()
        _crawler_email = email
        store.update_state(
            lambda state: state.update(
                crawler_running=True,
                crawler_status="Checking for new links...",
                crawler_error="",
            )
        )
        _thread = threading.Thread(
            target=_update_once, args=(email,), name="jobright-update", daemon=True
        )
        _thread.start()


def _update_once(email: str = "") -> None:
    if email:
        store.set_request_email(email)
    try:
        session_id = _session()
        _set_status("Checking for new links...", busy=True)
        added = _scan_new(session_id)
        nxt = (_now() + timedelta(seconds=store.update_interval())).isoformat()
        _set_status(f"Update finished. Added {added} new links.", next_update=nxt, busy=False)
    except jobright_client.JobrightError as exc:
        _set_status("Update stopped.", str(exc), busy=False)
    except Exception as exc:
        _set_status("Update stopped.", str(exc), busy=False)
    finally:
        store.update_state(lambda state: state.update(crawler_running=False))


def _queue_unsynced_applied() -> int:
    pending = [
        job
        for job in (store.load_state().get("jobs") or {}).values()
        if job.get("status") == "applied" and not job.get("jobright_synced") and job.get("job_id")
    ]
    _queue_jobright_applies(pending)
    return len(pending)


def _read_order_state(session_id: str) -> bool:
    jobs, linkedin, _raw, ordered = _fetch(session_id, 0, True)
    if not ordered:
        store.set_feed_first_page([], False)
        return False
    recent = is_most_recent_order(ordered)
    store.set_feed_first_page(_page_ids(ordered), recent)
    _send_linkedin_applied(session_id, linkedin)
    return recent


def sync_on_start(email: str = "") -> None:
    global _busy, _crawler_email
    if email:
        store.set_request_email(email)
        _crawler_email = email
    with _lock:
        if _busy:
            return
        if not store.load_state().get("session_id"):
            return
        _stop.clear()
        _busy = True
    try:
        session_id = _session()
        _set_status("Syncing saved data with Jobright...", busy=True)
        if not store.load_state().get("feed_sort"):
            store.set_feed_sort("recent")
        filters = jobright_client.get_filters(session_id)
        chips = jobright_client.summarize_filters(filters)
        store.update_state(
            lambda state: state.update(
                filter_chips=chips,
                search_days=int(filters.get("daysAgo") or 0),
            )
        )
        applied, _hidden = _sync_applied(session_id)
        recent = _read_order_state(session_id)
        title_applied = _apply_title_filters(session_id)
        queued = _queue_unsynced_applied()
        counts = store.counts()
        wanted = "most recent" if store.feed_sort() == "recent" else "recommended"
        seen = "most-recent order" if recent else "not most-recent order"
        extra = ""
        if title_applied:
            extra += f" {title_applied} title-filtered links sent to Applied."
        if queued:
            extra += f" {queued} saved Applied links queued to Jobright."
        _set_status(
            f"Synced with Jobright. Order set to {wanted} ({seen}). "
            f"{applied} applied jobs imported, {counts['all']} saved links.{extra} "
            "Click Start or Update.",
            busy=False,
        )
    except jobright_client.JobrightError as exc:
        _set_status("Startup sync stopped.", str(exc), busy=False)
    except Exception as exc:
        _set_status("Startup sync stopped.", str(exc), busy=False)
    finally:
        store.update_state(lambda state: state.update(crawler_running=_busy))


def stop() -> None:
    global _busy
    _stop.set()
    _wake.set()
    _busy = False
