from __future__ import annotations

import time
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chrome_profiles
import crawler
import db as sql_db
import jobright_client
import store

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

app = FastAPI(title="Job Link")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def account_middleware(request, call_next):
    email = request.cookies.get("joblink_account", "")
    needs_cookie = False
    if not email:
        email = (sql_db.read_active_email() or "").strip().lower()
        needs_cookie = bool(email)
    store.set_request_email(email)
    response = await call_next(request)
    if needs_cookie:
        response.set_cookie(
            "joblink_account", email,
            max_age=365 * 24 * 3600, httponly=False, samesite="lax",
        )
    return response


_APPLY_QUEUE: Queue[tuple[str, str]] = Queue()
_APPLY_SEEN: set[str] = set()
_APPLY_LOCK = Lock()

# test change in dev
def _jobright_apply_loop() -> None:
    
    while True:
        email, job_id = _APPLY_QUEUE.get()
        try:
            store.set_request_email(email)
            session_id = store.load_state().get("session_id") or ""
            if session_id:
                jobright_client.mark_applied(session_id, job_id)
                store.set_job_status(job_id, "applied", {"jobright_synced": True})
        except Exception as exc:
            print(f"jobright apply failed {job_id}: {exc}")
        finally:
            with _APPLY_LOCK:
                _APPLY_SEEN.discard(job_id)
            _APPLY_QUEUE.task_done()


def _queue_jobright_apply(job_id: str) -> None:
    email = store.current_email()
    with _APPLY_LOCK:
        if job_id in _APPLY_SEEN:
            return
        _APPLY_SEEN.add(job_id)
    _APPLY_QUEUE.put((email, job_id))


Thread(target=_jobright_apply_loop, name="job-link-apply", daemon=True).start()


@app.on_event("startup")
def on_startup() -> None:
    email = (sql_db.read_active_email() or "").strip().lower()
    Thread(
        target=crawler.sync_on_start, args=(email,), name="job-link-sync", daemon=True
    ).start()


class LoginBody(BaseModel):
    email: str
    password: str


class SessionBody(BaseModel):
    session_id: str = Field(min_length=8)


class ProfileBody(BaseModel):
    directory: str = ""
    name: str = ""
    directories: list[str] | None = None
    names: dict[str, str] | None = None


PAGE_SIZE = 20
MAX_PAGES = 10


class RefreshBody(BaseModel):
    pages: int = 10
    reset: bool = True
    scan_new: bool = False


class SortBody(BaseModel):
    sort: str = "recommended"


class FeedSortBody(BaseModel):
    sort: str = "recent"


class DateBody(BaseModel):
    date_from: str = ""
    date_to: str = ""


class LinkHostBody(BaseModel):
    host: str = ""


class AppliedUrlBody(BaseModel):
    url: str = ""
    company: str = ""
    source: str = ""
    previous: str = ""


class OpenedUrlBody(BaseModel):
    url: str = ""
    previous: str = ""


class OpenTabItem(BaseModel):
    id: int
    url: str = ""
    previous: str = ""


class OpenTabsBody(BaseModel):
    tabs: list[OpenTabItem] = []


class UpdateWaitBody(BaseModel):
    seconds: int = 1800


class BulkOpenBody(BaseModel):
    count: int = 10
    tab: str = "new"


class DisplayCountBody(BaseModel):
    count: int = 20


class CompanyApplyBody(BaseModel):
    days: int = 60


class FilterBody(BaseModel):
    daysAgo: int | None = None
    country: str | None = None
    seniority: list[int] | None = None
    jobTypes: list[int] | None = None
    workModel: list[int] | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/profiles")
def profiles() -> dict:
    try:
        items = chrome_profiles.list_profiles()
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"profiles": items}


@app.get("/api/profiles/picture", response_model=None)
def profile_picture(dir: str = ""):
    path = chrome_profiles.profile_picture_path(dir)
    if path:
        media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(path, media_type=media)
    directory = Path(dir or "").name
    label = chrome_profiles.profile_display_name(directory)
    name = (label[:1] or "?").upper()
    if name in {"<", ">", "&", '"'}:
        name = "?"
    color = chrome_profiles.profile_picture_color(directory)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
        f'<circle cx="20" cy="20" r="20" fill="{color}"/>'
        f'<text x="20" y="26" text-anchor="middle" font-size="18" '
        f'font-family="Segoe UI, sans-serif" fill="#fff">{name}</text>'
        f"</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/state")
def state(
    tab: str = "new",
    offset: int = 0,
    limit: int | None = None,
    q: str = "",
) -> dict:
    current = store.ensure_posted_dates()
    current = store.ensure_next_update_at()
    sort = current.get("sort") or "recommended"
    display_count = int(current.get("display_count") or 20)
    display_count = 0 if display_count == 0 else max(1, min(display_count, 200))
    page_limit = limit
    if page_limit is None:
        page_limit = None if display_count == 0 else display_count
    elif page_limit < 0:
        page_limit = display_count if display_count else 20
    view = store.build_state_view(
        tab=tab,
        sort=sort,
        date_from=current.get("date_from") or "",
        date_to=current.get("date_to") or "",
        link_host=current.get("link_host") or "",
        query=q,
        offset=max(0, int(offset or 0)),
        limit=page_limit,
    )
    return {
        "connected": bool(current.get("session_id")),
        "email": current.get("email") or "",
        "selected_profile": current.get("selected_profile") or "",
        "selected_profiles": store.selected_profiles(current),
        "next_position": current.get("next_position") or 0,
        "has_more": bool(current.get("has_more")),
        "sort": sort,
        "filter_chips": current.get("filter_chips") or [],
        "date_from": current.get("date_from") or "",
        "date_to": current.get("date_to") or "",
        "date_min": view["date_min"],
        "date_max": view["date_max"],
        "today": store.local_day(),
        "timezone": store.local_now().tzname() or "",
        "posted_days": view["posted_days"],
        "link_host": current.get("link_host") or "",
        "bulk_open_count": int(current.get("bulk_open_count") or 10),
        "display_count": display_count,
        "company_apply_days": store.company_apply_days(),
        "feed_sort": store.feed_sort(),
        "feed_recent_order": bool(current.get("feed_recent_order")),
        "history": store.history(),
        "link_hosts": view["link_hosts"],
        "crawler": {
            "running": crawler.busy(),
            "full_crawl_done": bool(current.get("full_crawl_done"))
            or (int(current.get("next_position") or 0) > 0 and not current.get("has_more")),
            "status": current.get("crawler_status") or "",
            "error": current.get("crawler_error") or "",
            "next_update_at": current.get("next_update_at") or "",
            "update_seconds": store.update_interval(),
        },
        "counts": view["counts"],
        "shown": view["shown"],
        "jobs": view["jobs"],
        "job_total": view["job_total"],
        "job_offset": view["job_offset"],
        "has_more_jobs": view["has_more_jobs"],
        "query": q or "",
    }


@app.get("/api/state/light")
def state_light() -> dict:
    current = store.load_state()
    return {
        "crawler": {
            "running": crawler.busy(),
            "full_crawl_done": bool(current.get("full_crawl_done"))
            or (int(current.get("next_position") or 0) > 0 and not current.get("has_more")),
            "status": current.get("crawler_status") or "",
            "error": current.get("crawler_error") or "",
            "next_update_at": current.get("next_update_at") or "",
            "update_seconds": store.update_interval(),
        },
    }


def _filter_payload(filters: dict) -> dict:
    chips = jobright_client.summarize_filters(filters)
    titles = [item.get("title") for item in (filters.get("jobTaxonomyList") or []) if item.get("title")]
    return {
        "daysAgo": filters.get("daysAgo"),
        "country": filters.get("country") or "",
        "seniority": filters.get("seniority") or [],
        "jobTypes": filters.get("jobTypes") or [],
        "workModel": filters.get("workModel") or [],
        "titles": titles,
        "chips": chips,
        "options": {
            "daysAgo": [1, 3, 7, 14, 30],
            "seniority": jobright_client.SENIORITY_LABELS,
            "jobTypes": jobright_client.JOB_TYPE_LABELS,
            "workModel": jobright_client.WORK_MODEL_LABELS,
        },
    }


@app.get("/api/filters")
def get_filters() -> dict:
    session_id = store.load_state().get("session_id")
    if not session_id:
        raise HTTPException(401, "Connect your Jobright account first.")
    try:
        filters = jobright_client.get_filters(session_id)
    except jobright_client.JobrightError as exc:
        raise HTTPException(400, str(exc)) from exc
    chips = jobright_client.summarize_filters(filters)
    store.update_state(lambda current: current.update(filter_chips=chips, search_days=int(filters.get("daysAgo") or 0)))
    return _filter_payload(filters)


@app.post("/api/filters")
def set_filters(body: FilterBody) -> dict:
    session_id = store.load_state().get("session_id")
    if not session_id:
        raise HTTPException(401, "Connect your Jobright account first.")
    updates: dict = {}
    if body.daysAgo is not None:
        updates["daysAgo"] = max(1, min(int(body.daysAgo), 90))
    if body.country is not None:
        updates["country"] = body.country.strip()
    if body.seniority is not None:
        updates["seniority"] = body.seniority
    if body.jobTypes is not None:
        updates["jobTypes"] = body.jobTypes
    if body.workModel is not None:
        updates["workModel"] = body.workModel
    try:
        filters = jobright_client.save_filters(session_id, updates)
    except jobright_client.JobrightError as exc:
        raise HTTPException(400, str(exc)) from exc
    chips = jobright_client.summarize_filters(filters)
    store.update_state(lambda current: current.update(filter_chips=chips, search_days=int(filters.get("daysAgo") or 0)))
    return {"ok": True, **_filter_payload(filters)}


@app.post("/api/crawler/start")
def crawler_start() -> dict:
    if not store.load_state().get("session_id"):
        raise HTTPException(401, "Connect your Jobright account first.")
    crawler.start()
    return {"ok": True, "running": True}


@app.post("/api/crawler/stop")
def crawler_stop() -> dict:
    crawler.stop()
    return {"ok": True, "running": False}


@app.post("/api/crawler/update")
def crawler_update() -> dict:
    if not store.load_state().get("session_id"):
        raise HTTPException(401, "Connect your Jobright account first.")
    crawler.update()
    return {"ok": True, "running": True}


@app.post("/api/jobs/applied/sync")
def sync_applied() -> dict:
    current = store.load_state()
    session_id = current.get("session_id")
    if not session_id:
        raise HTTPException(401, "Connect your Jobright account first.")
    try:
        jobs, hidden = jobright_client.fetch_applied_jobs(session_id)
    except jobright_client.JobrightError as exc:
        raise HTTPException(400, str(exc)) from exc
    store.upsert_applied_jobs(jobs, 0)
    return {
        "ok": True,
        "added": store.load_state().get("last_applied_added", 0),
        "marked": store.load_state().get("last_applied_marked", 0),
        "imported": len(jobs),
        "linkedin": hidden,
        "counts": store.counts(),
    }


@app.post("/api/update-wait")
def set_update_wait(body: UpdateWaitBody) -> dict:
    current = store.set_update_wait(body.seconds)
    return {
        "update_seconds": store.update_interval(),
        "next_update_at": current.get("next_update_at") or "",
    }


@app.post("/api/company-apply-days")
def set_company_apply_days(body: CompanyApplyBody) -> dict:
    result = store.set_company_apply_days(body.days)
    for job in result.get("marked_jobs") or []:
        job_id = job.get("job_id") or ""
        if job_id and not job.get("jobright_synced"):
            _queue_jobright_apply(job_id)
    return {
        "company_apply_days": int(result.get("company_apply_days") or 0),
        "marked": int(result.get("marked") or 0),
        "reverted": int(result.get("reverted") or 0),
        "counts": store.counts(),
    }


@app.post("/api/sort")
def set_sort(body: SortBody) -> dict:
    allowed = {"recommended", "match", "newest", "company", "title"}
    sort = body.sort if body.sort in allowed else "recommended"
    store.update_state(lambda current: current.update(sort=sort))
    return {"sort": sort}


@app.post("/api/feed-sort")
def set_feed_sort(body: FeedSortBody) -> dict:
    store.set_feed_sort(body.sort)
    return {
        "feed_sort": store.feed_sort(),
        "feed_recent_order": False,
    }


@app.post("/api/dates")
def set_dates(body: DateBody) -> dict:
    date_from = (body.date_from or "").strip()
    date_to = (body.date_to or "").strip()
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    store.update_state(
        lambda current: current.update(date_from=date_from, date_to=date_to, link_host="")
    )
    return {"date_from": date_from, "date_to": date_to, "link_host": ""}


@app.post("/api/display-count")
def set_display_count(body: DisplayCountBody) -> dict:
    raw = 0 if body.count == 0 else int(body.count or 20)
    count = 0 if raw == 0 else max(1, min(raw, 200))
    store.update_state(lambda current: current.update(display_count=count))
    return {"display_count": count}


@app.post("/api/link-host")
def set_link_host(body: LinkHostBody) -> dict:
    host = (body.host or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    store.update_state(lambda current: current.update(link_host=host))
    return {"link_host": host}


@app.post("/api/profile")
def select_profile(body: ProfileBody) -> dict:
    if body.directories is not None:
        state = store.select_profiles(body.directories, body.names or {})
    else:
        state = store.select_profile(body.directory, body.name)
    return {
        "selected_profile": state.get("selected_profile") or "",
        "selected_profiles": store.selected_profiles(state),
    }


@app.post("/api/jobright/login")
def jobright_login(body: LoginBody, response: Response) -> dict:
    try:
        session_id = jobright_client.login(body.email.strip(), body.password)
    except jobright_client.JobrightError as exc:
        raise HTTPException(400, str(exc)) from exc
    new_email = body.email.strip().lower()
    crawler.stop()
    store.set_request_email(new_email)
    response.set_cookie(
        "joblink_account", new_email,
        max_age=365 * 24 * 3600, httponly=False, samesite="lax",
    )
    store.update_state(
        lambda current: current.update(session_id=session_id, email=body.email.strip())
    )
    return {"ok": True, "email": body.email.strip()}


@app.post("/api/jobright/logout")
def jobright_logout() -> dict:
    crawler.stop()
    store.update_state(
        lambda current: current.update(
            session_id="",
            crawler_running=False,
            crawler_status="",
            crawler_error="",
            next_update_at="",
            full_crawl_done=False,
        )
    )
    return {"ok": True}


@app.post("/api/jobright/session")
def jobright_session(body: SessionBody) -> dict:
    session_id = body.session_id.strip()
    if not jobright_client.validate_session(session_id):
        raise HTTPException(400, "That Jobright session is not valid.")
    store.update_state(lambda current: current.update(session_id=session_id))
    return {"ok": True}


@app.post("/api/jobs/refresh")
def refresh_jobs(body: RefreshBody) -> dict:
    current = store.load_state()
    session_id = current.get("session_id")
    if not session_id:
        raise HTTPException(401, "Connect your Jobright account first.")

    pages = max(1, min(body.pages, MAX_PAGES))
    position = 0 if body.reset or body.scan_new else int(current.get("next_position") or 0)
    try:
        if body.reset or body.scan_new or not current.get("filter_chips"):
            filters = jobright_client.get_filters(session_id)
            chips = jobright_client.summarize_filters(filters)
        else:
            chips = current.get("filter_chips") or []
        all_jobs, hidden, next_position = jobright_client.fetch_pages(
            session_id,
            start_position=position,
            pages=pages,
            page_size=PAGE_SIZE,
            refresh_first=body.reset and not body.scan_new,
        )
    except jobright_client.JobrightError as exc:
        raise HTTPException(400, str(exc)) from exc

    if body.scan_new:
        known = set(current.get("jobs") or {})
        fresh = [job for job in all_jobs if job["job_id"] not in known]
        store.upsert_jobs(
            fresh,
            current.get("next_position") or 0,
            hidden,
            chips,
            False,
            current.get("has_more", False),
            prepend_new=True,
        )
        return {
            "added": len(fresh),
            "hidden_linkedin": hidden,
            "filter_chips": chips,
            "has_more": bool(current.get("has_more")),
            "new_count": len(fresh),
            "counts": store.counts(),
        }

    has_more = len(all_jobs) + hidden > 0
    store.upsert_jobs(all_jobs, next_position, hidden, chips, body.reset, has_more)
    return {
        "added": len(all_jobs),
        "hidden_linkedin": hidden,
        "filter_chips": chips,
        "has_more": has_more,
        "new_count": 0,
        "counts": store.counts(),
    }


@app.post("/api/jobs/{job_id}/open")
def open_job(job_id: str) -> dict:
    current = store.load_state()
    job = current["jobs"].get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    profile = current.get("selected_profile") or ""
    if not profile:
        raise HTTPException(400, "Select a Chrome profile first.")
    group = store.same_company_day_jobs(job)
    if not group:
        raise HTTPException(400, "This job has no company link.")
    try:
        chrome_profiles.open_many_in_profile(
            profile,
            [item.get("apply_url") or "" for item in group],
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc

    extra = {"opened_at": store.now(), "opened_profile": profile}
    for item in group:
        item_id = item["job_id"]
        if item.get("status") != "applied":
            store.set_job_status(item_id, "opened", extra)
        store.remember_opened_url(item_id, item.get("apply_url") or "")
    store.remember_tab_group(group)
    store.record_visits(group, profile)
    day = store.posted_date_of(job)
    company = job.get("company") or "this company"
    return {
        "ok": True,
        "status": "opened",
        "opened": len(group),
        "company": company,
        "posted_date": day,
        "message": (
            f"Opened {len(group)} {company} jobs posted {day}."
            if len(group) > 1
            else f"Opened {company}."
        ),
        "counts": store.counts(),
    }


@app.post("/api/jobs/bulk-open")
def bulk_open(body: BulkOpenBody) -> dict:
    count = max(1, min(int(body.count or 10), 20))
    store.update_state(lambda current: current.update(bulk_open_count=count))
    current = store.load_state()
    profiles = store.selected_profiles(current)
    if not profiles:
        raise HTTPException(400, "Select a Chrome profile first.")
    # Same `count` links opened in every selected profile
    candidates = [
        job
        for job in store.jobs_by_tab(
            body.tab or "new",
            current.get("sort") or "recommended",
            current.get("date_from") or "",
            current.get("date_to") or "",
            current.get("link_host") or "",
        )
        if job.get("status") != "applied" and job.get("apply_url")
    ]
    if not candidates:
        return {"ok": True, "opened": 0, "message": "No links to open in this list.", "counts": store.counts()}
    seen: set[str] = set()
    groups: list[list[dict]] = []
    opened: list[dict] = []
    for job in candidates:
        if len(opened) >= count:
            break
        group = [
            item
            for item in store.same_company_day_jobs(job)
            if item.get("job_id") not in seen and item.get("apply_url")
        ]
        if not group:
            continue
        leftover = max(0, count - len(opened))
        group = group[:leftover]
        for item in group:
            seen.add(item["job_id"])
        groups.append(group)
        opened.extend(group)
    if not opened:
        return {"ok": True, "opened": 0, "message": "No links to open in this list.", "counts": store.counts()}
    try:
        for profile in profiles:
            for group in groups:
                chrome_profiles.open_many_in_profile(
                    profile,
                    [item.get("apply_url") or "" for item in group],
                )
                store.record_visits(group, profile)
                time.sleep(0.2)
        primary = profiles[0]
        extra = {"opened_at": store.now(), "opened_profile": primary}
        for group in groups:
            for item in group:
                if item.get("status") != "applied":
                    store.set_job_status(item["job_id"], "opened", extra)
                store.remember_opened_url(item["job_id"], item.get("apply_url") or "")
            store.remember_tab_group(group)
    except (ValueError, FileNotFoundError) as extra_exc:
        raise HTTPException(400, str(extra_exc)) from extra_exc
    message = (
        f"Opened the same {len(opened)} link{'s' if len(opened) != 1 else ''} "
        f"in {len(profiles)} Chrome profile{'s' if len(profiles) != 1 else ''}."
    )
    return {
        "ok": True,
        "opened": len(opened),
        "profiles": len(profiles),
        "message": message,
        "counts": store.counts(),
    }


@app.post("/api/jobs/{job_id}/applied")
def mark_applied(job_id: str) -> dict:
    current = store.load_state()
    job = current["jobs"].get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    session_id = current.get("session_id")
    if not session_id:
        raise HTTPException(401, "Connect your Jobright account first.")

    try:
        jobright_client.mark_applied(session_id, job_id)
        synced = True
        message = "Marked applied on Jobright."
    except jobright_client.JobrightError as exc:
        synced = False
        message = str(exc)

    store.set_job_status(
        job_id,
        "applied",
        {"applied_at": store.now(), "jobright_synced": synced},
    )
    return {"ok": True, "synced": synced, "message": message, "counts": store.counts()}


def _mark_job_applied(job_id: str, source: str) -> dict:
    current = store.load_state()
    job = current.get("jobs", {}).get(job_id) or {}
    already = job.get("status") == "applied"
    store.set_job_status(
        job_id,
        "applied",
        {
            "applied_at": job.get("applied_at") or store.now(),
            "jobright_synced": bool(job.get("jobright_synced")),
            "applied_source": source or job.get("applied_source") or "",
        },
    )
    if current.get("session_id") and not job.get("jobright_synced"):
        _queue_jobright_apply(job_id)
        message = "Marked applied in Job Link. Jobright sync is queued."
    elif job.get("jobright_synced"):
        message = "Already marked applied on Jobright."
    else:
        message = "Marked applied in Job Link."
    return {
        "ok": True,
        "matched": True,
        "job_id": job_id,
        "synced": bool(job.get("jobright_synced")),
        "queued": bool(current.get("session_id") and not job.get("jobright_synced")),
        "already": already,
        "message": message,
        "counts": store.counts(),
    }


@app.get("/api/open-tab-groups")
def open_tab_groups() -> dict:
    return {"groups": store.pending_tab_groups()}


@app.post("/api/open-tab-groups/match")
def match_open_tab_groups(body: OpenTabsBody) -> dict:
    groups = store.pending_tab_groups()
    matches = []
    for tab in body.tabs:
        url = (tab.url or "").strip()
        previous = (tab.previous or "").strip()
        for group in groups:
            wanted = group.get("urls") or []
            if any(store.urls_match(url, item) or store.urls_match(previous, item) for item in wanted):
                matches.append(
                    {
                        "id": tab.id,
                        "group_id": group.get("id") or "",
                        "company": group.get("company") or "Same company",
                        "color": group.get("color") or "blue",
                        "hex": group.get("hex") or "#2563eb",
                    }
                )
                break
    return {"groups": groups, "matches": matches}


@app.post("/api/jobs/opened-url")
def remember_opened_tab_url(body: OpenedUrlBody) -> dict:
    url = (body.url or "").strip()
    previous = (body.previous or "").strip()
    if not url and not previous:
        return {"ok": True, "matched": False}
    job = store.attach_opened_urls(url, previous)
    return {"ok": True, "matched": bool(job), "job_id": (job or {}).get("job_id") or ""}


@app.post("/api/jobs/applied-by-url")
def mark_applied_by_url(body: AppliedUrlBody) -> dict:
    url = (body.url or "").strip()
    previous = (body.previous or "").strip()
    if not url and not previous:
        return {"ok": True, "matched": False, "message": "No page URL was sent."}
    if previous:
        store.attach_opened_urls(url, previous)
    job = store.find_opened_job_by_url(url, body.company)
    if not job and previous:
        job = store.find_opened_job_by_url(previous, body.company)
    if not job and body.company:
        job = store.find_opened_job_by_url(url, "")
    if not job and previous:
        job = store.find_opened_job_by_url(previous, "")
    if not job:
        print(f"closed-tab unmatched source={body.source} url={url} previous={previous}")
        return {"ok": True, "matched": False, "message": "This page was not opened from Job Link."}
    result = _mark_job_applied(job["job_id"], body.source.strip() or "resume-assistant")
    print(
        f"closed-tab matched source={body.source} job_id={job['job_id']} "
        + f"title={job.get('title') or ''} url={url} previous={previous}"
    )
    return result
