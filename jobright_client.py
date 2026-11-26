from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import httpx

import store

JOB_TYPE_LABELS = {1: "Full-time", 2: "Part-time", 3: "Contract", 4: "Internship"}
SENIORITY_LABELS = {1: "Internship", 2: "Entry Level", 3: "Mid Level", 4: "Senior Level"}
WORK_MODEL_LABELS = {1: "On-site", 2: "Remote", 3: "Hybrid"}

API_BASE = "https://swan-api.jobright.ai"
SITE_BASE = "https://jobright.ai"
LINKEDIN_HOSTS = ("linkedin.com", "lnkd.in")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Origin": SITE_BASE,
    "Referer": f"{SITE_BASE}/jobs/recommend",
    "Accept": "application/json, text/plain, */*",
}


class JobrightError(RuntimeError):
    pass


def is_linkedin_url(url: str | None) -> bool:
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    return any(host == item or host.endswith("." + item) for item in LINKEDIN_HOSTS)


def should_auto_apply(job: dict | None) -> bool:
    if not job:
        return False
    return is_linkedin_url(job.get("apply_url")) or store.is_skip_title(job.get("title"))


def auto_apply_source(job: dict | None) -> str:
    if not job:
        return ""
    if is_linkedin_url(job.get("apply_url")):
        return "linkedin"
    if store.is_skip_title(job.get("title")):
        return "title-filter"
    return ""


def _client(session_id: str | None = None) -> httpx.Client:
    cookies = {"SESSION_ID": session_id} if session_id else {}
    return httpx.Client(
        base_url=API_BASE,
        headers=HEADERS,
        cookies=cookies,
        timeout=30,
        follow_redirects=True,
    )


def _result(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise JobrightError(f"Jobright returned a non-JSON response ({resp.status_code}).") from exc
    if resp.status_code == 401:
        raise JobrightError("Jobright session expired. Connect again.")
    if resp.status_code >= 400:
        raise JobrightError(f"Jobright request failed ({resp.status_code}).")
    if data.get("success") is False:
        message = data.get("message") or data.get("errorMsg") or "Jobright request failed."
        if any(word in str(message).lower() for word in ("login", "auth", "session")):
            raise JobrightError("Jobright session expired. Connect again.")
        raise JobrightError(str(message))
    return data.get("result") or {}


def login(email: str, password: str) -> str:
    with _client() as client:
        resp = client.post("/swan/auth/login/pwd", json={"email": email, "password": password})
        _result(resp)
        session_id = resp.cookies.get("SESSION_ID")
        if not session_id:
            for cookie in resp.headers.get_list("set-cookie"):
                if "SESSION_ID=" in cookie:
                    session_id = cookie.split("SESSION_ID=", 1)[1].split(";", 1)[0]
                    break
        if not session_id:
            raise JobrightError("Login succeeded but Jobright did not return a session.")
        return session_id


def validate_session(session_id: str) -> bool:
    with _client(session_id) as client:
        resp = client.get("/swan/recommend/list/jobs", params={"position": 0, "count": 1})
        if resp.status_code in (401, 403):
            return False
        data = resp.json()
        return bool(data.get("success"))


def _first_url(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def _company_site(job_result: dict, company_result: dict) -> str:
    return _first_url(
        job_result.get("applyLink"),
        job_result.get("applyUrl"),
        job_result.get("jobApplyUrl"),
        company_result.get("companyWebsite"),
        company_result.get("companyUrl"),
        company_result.get("website"),
        company_result.get("homepage"),
        company_result.get("companyHomePage"),
        company_result.get("c_homepage_url"),
    )


def parse_job(raw: dict) -> dict | None:
    job_result = raw.get("jobResult") or {}
    company_result = raw.get("companyResult") or {}
    job_id = str(job_result.get("jobId") or raw.get("jobId") or "")
    if not job_id:
        return None

    apply_url = _company_site(job_result, company_result)
    if not apply_url:
        return None

    score = raw.get("displayScore")
    try:
        match = int(round(float(score))) if score is not None else None
    except (TypeError, ValueError):
        match = None

    publish_time = str(job_result.get("publishTime") or "").strip()
    posted = str(job_result.get("publishTimeDesc") or "").strip()
    return {
        "job_id": job_id,
        "title": job_result.get("jobTitle") or "",
        "company": company_result.get("companyName") or "",
        "apply_url": apply_url,
        "jobright_url": f"{SITE_BASE}/jobs/info/{job_id}",
        "salary": job_result.get("salaryDesc") or "",
        "location": job_result.get("jobLocation") or "",
        "remote": bool(job_result.get("isRemote")),
        "seniority": job_result.get("jobSeniority") or "",
        "work_model": job_result.get("workModel") or "",
        "posted": posted,
        "publish_time": publish_time,
        "posted_date": store.posted_date_from(publish_time, posted),
        "match": match,
        "status": "new",
    }


def get_filters(session_id: str) -> dict:
    with _client(session_id) as client:
        resp = client.post("/swan/filter/get/filter")
        return _result(resp) or {}


def save_filters(session_id: str, updates: dict) -> dict:
    current = get_filters(session_id)
    allowed = ("daysAgo", "country", "seniority", "jobTypes", "workModel")
    for key in allowed:
        if key in updates and updates[key] is not None:
            current[key] = updates[key]
    try:
        with _client(session_id) as client:
            resp = client.post("/swan/filter/update/filter", json=current)
            _result(resp)
    except httpx.HTTPError as exc:
        raise JobrightError(f"Jobright request failed: {exc}") from exc
    return get_filters(session_id)


def summarize_filters(filters: dict) -> list[str]:
    chips = []
    country = (filters.get("country") or "").upper()
    if country == "US":
        chips.append("United States")
    elif country:
        chips.append(country)

    titles = [item.get("title") for item in (filters.get("jobTaxonomyList") or []) if item.get("title")]
    if titles:
        extra = f" (+{len(titles) - 1})" if len(titles) > 1 else ""
        chips.append(f"{titles[0]}{extra}")

    seniorities = [SENIORITY_LABELS.get(item, str(item)) for item in (filters.get("seniority") or [])]
    if seniorities:
        extra = f" (+{len(seniorities) - 1})" if len(seniorities) > 1 else ""
        chips.append(f"{seniorities[0]}{extra}")

    for item in filters.get("jobTypes") or []:
        chips.append(JOB_TYPE_LABELS.get(item, str(item)))
    for item in filters.get("workModel") or []:
        chips.append(WORK_MODEL_LABELS.get(item, str(item)))

    days = filters.get("daysAgo")
    if days:
        chips.append(f"Past {days} days")
    return [chip for chip in chips if chip]


def fetch_applied_jobs(session_id: str, page_size: int = 20) -> tuple[list[dict], int]:
    jobs: list[dict] = []
    hidden = 0
    page = 1
    cursor = None
    seen: set[str] = set()
    try:
        with _client(session_id) as client:
            while page <= 200:
                body: dict[str, object] = {"page": page, "pageSize": page_size}
                if cursor:
                    body["cursor"] = cursor
                resp = client.post("/swan/job/applied/jobs-v3", json=body)
                result = _result(resp)
                raw_list = result.get("list") or []
                if not raw_list:
                    break
                for raw in raw_list:
                    parsed = parse_job(raw)
                    if not parsed:
                        continue
                    job_id = parsed["job_id"]
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    parsed["status"] = "applied"
                    parsed["applied_at"] = raw.get("applyTime") or parsed.get("applied_at") or ""
                    if is_linkedin_url(parsed.get("apply_url")):
                        hidden += 1
                        parsed["applied_source"] = "linkedin"
                    jobs.append(parsed)
                if not result.get("hasMore"):
                    break
                cursor = result.get("cursor")
                page += 1
    except httpx.HTTPError as exc:
        raise JobrightError(f"Jobright request failed: {exc}") from exc
    return jobs, hidden


SORT_RECENT = 1


def fetch_recommendations(
    session_id: str,
    position: int = 0,
    count: int = 20,
    refresh: bool = False,
    sort_condition: int = SORT_RECENT,
) -> tuple[list[dict], list[dict], int, list[dict]]:
    params: dict[str, object] = {
        "position": position,
        "count": count,
        "sortCondition": sort_condition,
        "syncRerank": "false",
    }
    if refresh:
        params["refresh"] = "true"
    try:
        with _client(session_id) as client:
            resp = client.get("/swan/recommend/list/jobs", params=params)
            raw_list = _result(resp).get("jobList") or []
    except httpx.HTTPError as exc:
        raise JobrightError(f"Jobright request failed: {exc}") from exc

    ordered: list[dict] = []
    jobs = []
    linkedin = []
    for raw in raw_list:
        parsed = parse_job(raw)
        if not parsed:
            continue
        ordered.append(parsed)
        if should_auto_apply(parsed):
            linkedin.append(parsed)
        else:
            jobs.append(parsed)
    return jobs, linkedin, len(raw_list), ordered


def fetch_pages(
    session_id: str,
    start_position: int,
    pages: int,
    page_size: int = 20,
    refresh_first: bool = False,
    workers: int = 6,
) -> tuple[list[dict], int, int]:
    """Fetch several recommend pages. The first page can refresh; the rest run in threads."""

    def one(position: int, refresh: bool) -> tuple[int, list[dict], int]:
        jobs, linkedin, _raw, _ordered = fetch_recommendations(
            session_id,
            position,
            page_size,
            refresh,
            store.feed_sort_condition(),
        )
        return position, jobs, len(linkedin)

    by_position: dict[int, list[dict]] = {}
    hidden = 0

    if refresh_first and pages:
        position, jobs, skipped = one(start_position, True)
        by_position[position] = jobs
        hidden += skipped
        rest = [
            (start_position + page_size * index, False)
            for index in range(1, pages)
        ]
    else:
        rest = [
            (start_position + page_size * index, False)
            for index in range(pages)
        ]

    if rest:
        with ThreadPoolExecutor(max_workers=min(workers, len(rest))) as pool:
            futures = [pool.submit(one, position, refresh) for position, refresh in rest]
            for future in as_completed(futures):
                position, jobs, skipped = future.result()
                by_position[position] = jobs
                hidden += skipped

    ordered: list[dict] = []
    seen: set[str] = set()
    for index in range(pages):
        for job in by_position.get(start_position + page_size * index, []):
            job_id = job.get("job_id")
            if job_id and job_id in seen:
                continue
            if job_id:
                seen.add(job_id)
            ordered.append(job)
    return ordered, hidden, start_position + page_size * pages


def mark_applied(session_id: str, job_id: str) -> dict:
    payload = {"jobId": job_id, "source": 0}
    with _client(session_id) as client:
        resp = client.post("/swan/job/apply", json=payload)
        try:
            return _result(resp)
        except JobrightError:
            resp = client.post(
                "/swan/job/apply-status/save",
                json={"jobId": job_id, "status": 0},
            )
            return _result(resp)
