const state = {
  tab: "new",
  selectedProfile: "",
  selectedProfiles: [],
  running: false,
  fullCrawlDone: false,
  nextUpdateAt: "",
  updateSeconds: 30 * 60,
  editingEta: false,
  editingBulk: false,
  dueUpdateFor: "",
  poller: null,
  etaTimer: null,
  calMonth: "",
  profiles: [],
  filtersOpen: false,
  hostItems: [],
  hostQuery: "",
  linkHostQuery: "",
  jobs: [],
  pageSize: 20,
  shownCount: 20,
  jobTotal: 0,
  hasMoreJobs: false,
  loadingMore: false,
  listKey: "",
  emptyNote: "",
  editingPageSize: false,
  tabCounts: { new: 0, opened: 0, applied: 0 },
  pageArmed: true,
  jobQuery: "",
  searchTimer: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || data.message || res.statusText);
  }
  return data;
}

function setNote(text, isError = false) {
  const note = $("note");
  note.textContent = text || "";
  note.className = isError ? "note error" : "note";
}

function setJoinNote(text, isError = false) {
  const note = $("join-note");
  if (!note) return;
  note.textContent = text || "";
  note.className = isError ? "note error" : "note";
}

function openJoinModal() {
  const modal = $("join-modal");
  modal.hidden = false;
  setJoinNote("");
  const connected = !$("session").hidden;
  $("logout").hidden = !connected;
  const email = $("email");
  if (email) email.focus();
}

function closeJoinModal() {
  $("join-modal").hidden = true;
  setJoinNote("");
}

function formatRemain(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function parseRemain(text) {
  const raw = String(text || "").trim();
  const clock = raw.match(/^(\d+)\s*:\s*(\d{1,2})$/);
  if (clock) {
    return Number(clock[1]) * 60 + Number(clock[2]);
  }
  if (/^\d+$/.test(raw)) {
    return Number(raw) * 60;
  }
  return null;
}

function paintEta() {
  const el = $("update-eta");
  const wrap = el?.closest(".start-wrap");
  if (!el) {
    return;
  }
  const show = state.fullCrawlDone;
  el.hidden = !show;
  wrap?.classList.toggle("is-update", show);
  if (!show || state.editingEta || document.activeElement === el) {
    return;
  }
  if (state.running) {
    el.value = "now";
    el.disabled = true;
    el.style.width = `${el.value.length}ch`;
    return;
  }
  el.disabled = false;
  if (!state.nextUpdateAt) {
    el.value = "—";
    el.style.width = `${el.value.length}ch`;
    return;
  }
  const remain = new Date(state.nextUpdateAt).getTime() - Date.now();
  if (Number.isNaN(remain)) {
    el.value = "—";
    el.style.width = `${el.value.length}ch`;
    return;
  }
  el.value = formatRemain(remain);
  el.style.width = `${el.value.length}ch`;
  if (remain <= 0 && state.dueUpdateFor !== state.nextUpdateAt) {
    state.dueUpdateFor = state.nextUpdateAt;
    startDueUpdate();
  }
}

async function startDueUpdate() {
  if (state.running || !state.fullCrawlDone) {
    return;
  }
  const seconds = state.updateSeconds || 30 * 60;
  try {
    const result = await api("/api/update-wait", {
      method: "POST",
      body: JSON.stringify({ seconds }),
    });
    state.nextUpdateAt = result.next_update_at || "";
    state.updateSeconds = result.update_seconds || seconds;
    paintEta();
  } catch {
    // Keep going so the list still updates.
  }
  await startCrawler();
}

function startEtaTimer() {
  if (state.etaTimer) {
    return;
  }
  state.etaTimer = setInterval(paintEta, 1000);
}

function setControls(crawler) {
  const running = Boolean(crawler.running);
  const done = Boolean(crawler.full_crawl_done);
  state.running = running;
  state.fullCrawlDone = done;
  state.nextUpdateAt = crawler.next_update_at || "";
  if (crawler.update_seconds) {
    state.updateSeconds = Number(crawler.update_seconds) || state.updateSeconds;
  }
  $("start-label").textContent = done ? "Update" : "Start";
  $("start").disabled = running;
  $("stop").disabled = !running;
  paintEta();
  startEtaTimer();
  if (running && !state.poller) {
    state.poller = setInterval(() => {
      refreshCrawler().catch(() => {});
    }, 3000);
  }
  if (!running && state.poller) {
    clearInterval(state.poller);
    state.poller = null;
  }
}

async function refreshCrawler() {
  const payload = await api("/api/state/light");
  const crawler = payload.crawler || {};
  setControls(crawler);
  if (crawler.error) {
    setNote(crawler.error, true);
  } else if (crawler.status) {
    setNote(crawler.status);
  }
  if (!crawler.running) {
    await refreshState();
  }
}

function stateUrl({ offset = 0, limit = null, tab = state.tab } = {}) {
  const params = new URLSearchParams({ tab: tab || "new" });
  params.set("offset", String(Math.max(0, offset || 0)));
  const pageLimit = limit == null ? (state.pageSize === 0 ? 0 : state.pageSize) : limit;
  params.set("limit", String(pageLimit));
  const query = String(state.jobQuery || "").trim();
  if (query) {
    params.set("q", query);
  }
  return `/api/state?${params.toString()}`;
}

function profileLabel(profile) {
  const jobright =
    profile.jobright_cookie === true
      ? " · Jobright cookie"
      : profile.jobright_cookie === null
        ? " · Chrome open"
        : "";
  return `${profile.name}${profile.email ? " · " + profile.email : ""}${jobright}`;
}

function normalizeSelectedProfiles(selected) {
  const available = new Set((state.profiles || []).map((item) => item.directory));
  const items = [];
  const seen = new Set();
  for (const directory of selected || []) {
    if (!directory || seen.has(directory) || (available.size && !available.has(directory))) {
      continue;
    }
    seen.add(directory);
    items.push(directory);
  }
  return items;
}

function paintSelectedProfiles() {
  const select = $("profile");
  const list = $("profile-list");
  const chips = $("profile-chips");
  const placeholder = $("profile-placeholder");
  const selected = normalizeSelectedProfiles(state.selectedProfiles);
  state.selectedProfiles = selected;
  state.selectedProfile = selected[0] || "";
  if (select) {
    select.value = state.selectedProfile;
  }
  if (chips) {
    chips.innerHTML = selected.map((directory) => {
      const profile = (state.profiles || []).find((item) => item.directory === directory);
      const name = profile ? profile.name || profile.directory : directory;
      return `
      <button type="button" class="profile-chip" data-remove-profile="${escapeHtml(directory)}" title="Remove ${escapeHtml(name)}">
        <img class="profile-chip-pic" alt="" src="/api/profiles/picture?dir=${encodeURIComponent(directory)}" />
        <span class="profile-chip-name">${escapeHtml(name)}</span>
        <span class="profile-chip-remove" aria-hidden="true">×</span>
      </button>`;
    }).join("");
  }
  if (placeholder) {
    placeholder.hidden = selected.length > 0;
  }
  if (list) {
    list.innerHTML = (state.profiles || []).map((profile) => {
      const active = selected.includes(profile.directory);
      return `
      <button type="button" class="profile-option${active ? " is-active" : ""}" role="option" data-profile="${escapeHtml(profile.directory)}" aria-selected="${active}">
        <span class="profile-check" aria-hidden="true">${active ? "✓" : ""}</span>
        <img class="profile-pic" alt="" src="/api/profiles/picture?dir=${encodeURIComponent(profile.directory)}" />
        <span class="profile-option-text">${escapeHtml(profileLabel(profile))}</span>
      </button>`;
    }).join("");
  }
}

function renderProfiles(profiles, selected) {
  const select = $("profile");
  state.profiles = profiles || [];
  if (select) {
    select.innerHTML = "";
    for (const profile of state.profiles) {
      const option = document.createElement("option");
      option.value = profile.directory;
      option.textContent = profileLabel(profile);
      select.appendChild(option);
    }
  }
  const chosen = Array.isArray(selected) ? selected : selected ? [selected] : [];
  state.selectedProfiles = normalizeSelectedProfiles(chosen);
  paintSelectedProfiles();
}

async function saveSelectedProfiles(directories) {
  const selected = normalizeSelectedProfiles(directories);
  state.selectedProfiles = selected;
  state.selectedProfile = selected[0] || "";
  paintSelectedProfiles();
  const names = {};
  for (const directory of selected) {
    const profile = (state.profiles || []).find((item) => item.directory === directory);
    names[directory] = profile ? profileLabel(profile) : directory;
  }
  await api("/api/profile", {
    method: "POST",
    body: JSON.stringify({ directories: selected, names }),
  });
}

function renderFilters(chips) {
  const box = $("filters");
  const chipsBox = $("filter-chips");
  const toggle = $("filters-toggle");
  toggle.hidden = false;
  toggle.setAttribute("aria-expanded", String(state.filtersOpen));
  box.hidden = !state.filtersOpen;
  if (!chipsBox) {
    return;
  }
  chipsBox.innerHTML = (chips || []).map((chip) => `<span class="chip">${escapeHtml(chip)}</span>`).join("");
}

function fillFilterForm(payload) {
  if (!payload) {
    return;
  }
  const days = String(payload.daysAgo || 3);
  if (![...$("filter-days").options].some((item) => item.value === days)) {
    const extra = document.createElement("option");
    extra.value = days;
    extra.textContent = `Past ${days} days`;
    $("filter-days").appendChild(extra);
  }
  $("filter-days").value = days;
  renderFilters(payload.chips || []);
}

async function loadFilters() {
  try {
    fillFilterForm(await api("/api/filters"));
  } catch (error) {
    setNote(error.message, true);
  }
}

function hostCountsHtml(visited, total, compact = false) {
  if (compact) {
    return `<span class="host-counts"><span class="host-visited">${visited}</span>/<span class="host-total">${total}</span></span>`;
  }
  return `<span class="host-counts"><span class="host-visited">${visited}</span> visited / <span class="host-total">${total}</span></span>`;
}

function hostMatchesQuery(item, query) {
  if (!query) {
    return true;
  }
  return `${item.name || ""} ${item.host || ""}`.toLowerCase().includes(query);
}

function paintLinkHostOptions() {
  const box = $("link-host-options");
  if (!box) return;
  const selected = $("link-host").value || "";
  const activeQuery = String(state.linkHostQuery || "").trim().toLowerCase();
  const query = state.hostQuery.trim().toLowerCase();
  const items = (state.hostItems || []).filter((item) => {
    if (query && !item.host) return false;
    return (item.host === "" || (item.count || 0) > 0) && hostMatchesQuery(item, query);
  });
  const matched = query ? matchingHostItems(query) : [];
  if (!items.length && !matched.length) {
    box.innerHTML = `<div class="link-host-empty">No posted links match.</div>`;
    return;
  }
  const parts = [];
  if (query && matched.length) {
    const matchCount = matched.reduce((sum, item) => sum + (item.count || 0), 0);
    const matchVisited = matched.reduce((sum, item) => sum + (item.visited || 0), 0);
    const allActive = !selected && activeQuery === query;
    parts.push(`
      <button type="button" class="link-host-option link-host-option-all${allActive ? " is-active" : ""}" role="option" data-host-query="${escapeHtml(query)}" aria-selected="${allActive}">
        <span class="link-host-name">Show all matching links</span>
        ${hostCountsHtml(matchVisited, matchCount)}
        <span class="link-host-hint">${matched.length} group${matched.length === 1 ? "" : "s"} · Enter</span>
      </button>`);
  }
  parts.push(
    ...items.map((item) => `
      <button type="button" class="link-host-option${item.host === selected ? " is-active" : ""}" role="option" data-host="${escapeHtml(item.host)}" aria-selected="${item.host === selected}">
        <span class="link-host-name">${escapeHtml(item.name)}</span>
        ${hostCountsHtml(item.visited, item.count)}
      </button>`)
  );
  box.innerHTML = parts.join("");
}

function matchingHostItems(query) {
  const text = String(query || "").trim().toLowerCase();
  return (state.hostItems || []).filter(
    (item) => item.host && hostMatchesQuery(item, text)
  );
}

function renderLinkHosts(hosts, selected, hostQuery = "") {
  const select = $("link-host");
  const list = $("link-host-list");
  const button = $("link-host-btn");
  const wasOpen = list && !list.hidden;
  const visible = (hosts || []).filter((item) => (item.count || 0) > 0);
  const total = visible.reduce((sum, item) => sum + item.count, 0);
  const visitedTotal = visible.reduce((sum, item) => sum + (item.visited || 0), 0);
  const items = [{ host: "", name: "All posted links", count: total, visited: visitedTotal }, ...visible.map((item) => ({
    host: item.host,
    name: item.host,
    count: item.count,
    visited: item.visited || 0,
  }))];
  if (selected && !items.some((item) => item.host === selected)) {
    selected = "";
  }
  const activeQuery = String(hostQuery || "").trim();
  state.linkHostQuery = activeQuery;
  if (activeQuery) {
    state.hostQuery = activeQuery;
  }
  const current = items.find((item) => item.host === selected) || items[0];
  state.hostItems = items;
  select.innerHTML = items.map((item) => `<option value="${escapeHtml(item.host)}">${escapeHtml(item.name)}</option>`).join("");
  select.value = current.host;
  if (activeQuery && !selected) {
    const matched = matchingHostItems(activeQuery);
    const matchCount = matched.reduce((sum, item) => sum + (item.count || 0), 0);
    const matchVisited = matched.reduce((sum, item) => sum + (item.visited || 0), 0);
    const label = matched.length
      ? `All matching · ${activeQuery}`
      : `No match · ${activeQuery}`;
    button.innerHTML = `<span class="link-host-name">${escapeHtml(label)}</span>${hostCountsHtml(matchVisited, matchCount, true)}`;
  } else {
    button.innerHTML = `<span class="link-host-name">${escapeHtml(current.name)}</span>${hostCountsHtml(current.visited, current.count, true)}`;
  }
  const clear = $("link-host-clear");
  if (clear) {
    clear.hidden = !(current.host || activeQuery);
  }
  const search = $("link-host-search");
  if (search && document.activeElement !== search) {
    search.value = state.hostQuery;
  }
  paintLinkHostOptions();
  if (wasOpen) {
    list.hidden = false;
    button.setAttribute("aria-expanded", "true");
  }
}

function renderJobs(payload) {
  const shown = payload.shown || {};
  state.tabCounts = {
    new: shown.new ?? payload.counts.new ?? 0,
    opened: shown.opened ?? payload.counts.opened ?? 0,
    applied: shown.applied ?? payload.counts.applied ?? 0,
  };
  const hiddenBits = [];
  const jobTotal = payload.job_total ?? (payload.jobs || []).length;
  if (payload.date_from || payload.date_to) {
    const range =
      payload.date_from && payload.date_to && payload.date_from === payload.date_to
        ? payload.date_from
        : `${payload.date_from || "start"} to ${payload.date_to || "now"}`;
    hiddenBits.push(`Showing ${jobTotal} links posted ${range}.`);
  }
  if (payload.link_host) {
    hiddenBits.push(`Showing ${jobTotal} links from ${payload.link_host}.`);
  } else if (payload.link_host_query) {
    hiddenBits.push(`Showing ${jobTotal} links matching posted-link “${payload.link_host_query}”.`);
  }
  if (payload.counts.real_applied || payload.counts.skipped_applied) {
    const real = payload.counts.real_applied || 0;
    const skipped = payload.counts.skipped_applied || 0;
    const parts = [];
    if (real) parts.push(`${real} applied`);
    if (skipped) {
      const detail = [];
      if (payload.counts.linkedin_applied) detail.push(`${payload.counts.linkedin_applied} LinkedIn`);
      if (payload.counts.title_skipped) detail.push(`${payload.counts.title_skipped} title`);
      parts.push(`${skipped} skipped${detail.length ? ` (${detail.join(", ")})` : ""}`);
    }
    hiddenBits.push(`Applied list: ${parts.join(" · ")}.`);
  }
  $("hidden").textContent = hiddenBits.join(" ");
  $("session").textContent = payload.connected
    ? `Connected${payload.email ? " · " + payload.email : ""}`
    : "Not connected";
  $("session").hidden = !payload.connected;
  $("join-jobright").hidden = Boolean(payload.connected);
  $("join-jobright").textContent = "Join in Jobright";
  $("logout").hidden = !payload.connected;
  if (!state.editingBulk && document.activeElement !== $("bulk-count")) {
    $("bulk-count").value = String(Math.min(20, Math.max(1, payload.bulk_open_count || 10)));
  }
  $("sort").value = payload.sort || "recommended";
  $("feed-sort").value = payload.feed_sort === "recommended" ? "recommended" : "recent";
  if (!state.editingPageSize && document.activeElement !== $("page-size")) {
    state.pageSize = pageSizeValue(payload.display_count);
    $("page-size").value = pageSizeLabel(state.pageSize);
    paintPageSizePresets();
  }
  $("date-from").value = payload.date_from || "";
  $("date-to").value = payload.date_to || "";
  $("date-from").min = payload.date_min || "";
  $("date-from").max = payload.date_max || "";
  $("date-to").min = payload.date_min || "";
  $("date-to").max = payload.date_max || "";
  const crawler = payload.crawler || {};
  setControls(crawler);
  if (crawler.error) {
    setNote(crawler.error, true);
  } else {
    setNote(crawler.status || "");
  }
  renderFilters(payload.filter_chips || []);
  renderLinkHosts(payload.link_hosts || [], payload.link_host || "", payload.link_host_query || "");
  renderCalendar(payload);
  renderHistory(payload.history || []);
  fitHistoryPane();

  const nextKey = [
    state.tab,
    payload.date_from || "",
    payload.date_to || "",
    payload.link_host || payload.link_host_query || "",
    payload.sort || "",
    String(state.jobQuery || "").trim().toLowerCase(),
  ].join("|");
  state.jobs = payload.jobs || [];
  state.jobTotal = jobTotal;
  state.hasMoreJobs = Boolean(payload.has_more_jobs);
  const ranged = payload.date_from || payload.date_to;
  state.emptyNote = state.jobQuery.trim()
    ? "No jobs match this title or company search."
    : payload.link_host
      ? `No jobs from ${payload.link_host}.`
      : payload.link_host_query
        ? `No jobs match posted-link “${payload.link_host_query}”.`
      : ranged
        ? "No jobs posted in this date range."
        : "No jobs in this list yet.";
  state.listKey = nextKey;
  state.shownCount = state.jobs.length;
  paintJobTable();
}

function visibleJobs() {
  return state.jobs;
}

function appliedKind(job) {
  if (job.status !== "applied") return "";
  if (job.applied_kind === "skipped" || job.applied_kind === "applied") {
    return job.applied_kind;
  }
  const source = String(job.applied_source || "").toLowerCase();
  if (source === "linkedin" || source === "title-filter") return "skipped";
  let host = String(job.link_host || "");
  if (!host && job.apply_url) {
    try {
      host = new URL(job.apply_url).hostname.replace(/^www\./, "").toLowerCase();
    } catch {
      host = "";
    }
  }
  if (host.includes("linkedin.") || host === "lnkd.in") return "skipped";
  return "applied";
}

function appliedLabel(job) {
  if (job.status !== "applied") return "";
  if (job.applied_label) return job.applied_label;
  const source = String(job.applied_source || "").toLowerCase();
  if (source === "title-filter") return "Skipped · Title";
  if (source === "linkedin") return "Skipped · LinkedIn";
  if (appliedKind(job) === "skipped") return "Skipped · LinkedIn";
  return "Applied";
}

function jobRowHtml(job, index) {
  const grouped = Number(job.company_group_size || 0) > 1;
  let host = String(job.link_host || "");
  if (!host && job.apply_url) {
    try {
      host = new URL(job.apply_url).hostname.replace(/^www\./, "");
    } catch {
      host = "";
    }
  }
  const favicon = host
    ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=32`
    : "";
  const badge = grouped
    ? `<span class="co-fav"${job.company_group_color ? ` style="--group-color:${escapeHtml(job.company_group_color)}"` : ""}>${favicon ? `<img src="${escapeHtml(favicon)}" alt="" />` : `<span class="co-fav-empty"></span>`}<i class="co-fav-dot" title="Same company"></i></span>`
    : "";
  const kind = appliedKind(job);
  const label = appliedLabel(job);
  const rowClass = [
    `is-${job.status}`,
    kind === "skipped" ? "is-skipped" : "",
    kind === "applied" ? "is-real-applied" : "",
    job.appeared ? "is-appeared" : "",
  ].filter(Boolean).join(" ");
  const kindBadge = label
    ? `<span class="applied-kind applied-kind-${kind || "applied"}">${escapeHtml(label)}</span>`
    : "";
  return `
    <tr class="${rowClass}">
      <td class="num">${index + 1}</td>
      <td class="profile">${job.opened_profile ? `<img class="profile-pic" alt="" title="${escapeHtml(job.opened_profile)}" src="/api/profiles/picture?dir=${encodeURIComponent(job.opened_profile)}" />` : ""}</td>
      <td class="title" title="${escapeHtml(job.title)}">${escapeHtml(job.title)}${kindBadge}</td>
      <td class="company" title="${escapeHtml(job.company)}"><span class="company-cell">${badge}<span class="company-name">${escapeHtml(job.company)}</span></span></td>
      <td class="link" data-open="${job.job_id}" title="${escapeHtml(job.apply_url)}"><a href="${escapeHtml(job.apply_url)}" data-open="${job.job_id}" title="${escapeHtml(job.apply_url)}">${escapeHtml(shortLink(job.apply_url))}</a></td>
      <td class="match">${job.match != null ? job.match + "%" : ""}</td>
      <td class="location" title="${escapeHtml(job.location)}">${escapeHtml(job.location)}</td>
      <td class="salary" title="${escapeHtml(job.salary)}">${escapeHtml(job.salary)}</td>
      <td class="posted" data-date="${escapeHtml(job.posted_date || "")}" title="${escapeHtml(job.posted_at || job.posted_date || job.posted || "")}"><span class="posted-day">${escapeHtml(job.posted_date || job.posted || "")}</span>${job.posted_hour ? `<span class="posted-hour">${escapeHtml(job.posted_hour)}</span>` : ""}</td>
      <td class="actions">
        <select class="action-select" data-job="${job.job_id}">
          <option value="" ${job.status === "new" ? "selected" : ""}>Select</option>
          <option value="open" ${job.status === "opened" ? "selected" : ""}>Open</option>
          <option value="applied" ${job.status === "applied" ? "selected" : ""}>Applied</option>
        </select>
      </td>
    </tr>
  `;
}

function paintDisplayedCounts() {
  const totals = state.tabCounts || {};
  for (const tab of ["new", "opened", "applied"]) {
    const total = Number(totals[tab] || 0);
    const box = $(`count-${tab}`);
    if (!box) continue;
    if (tab === state.tab) {
      const loaded = state.jobs.length;
      const all = state.jobTotal || loaded;
      box.textContent = `${loaded}/${all}`;
    } else {
      box.textContent = String(total);
    }
  }
}

function paintJobTable() {
  const list = $("list");
  if (!list) return;
  const jobs = visibleJobs();
  paintDisplayedCounts();
  if (!jobs.length) {
    list.innerHTML = `<div class="note">${escapeHtml(state.emptyNote || "No jobs in this list yet.")}</div>`;
    return;
  }
  const shown = jobs.length;
  const total = state.jobTotal || shown;
  const rows = jobs.map((job, index) => jobRowHtml(job, index)).join("");
  const more = state.hasMoreJobs
    ? `<button type="button" class="list-more" data-load-more>Showing ${shown} of ${total}. Scroll for more.</button>`
    : `<div class="list-more">Showing ${shown} of ${total}.</div>`;
  list.innerHTML = `
    <table>
      <thead>
        <tr>
          <th class="num">#</th>
          <th class="profile"></th>
          <th class="title">Title</th>
          <th class="company">Company</th>
          <th class="link">Link</th>
          <th class="match">Match</th>
          <th class="location">Location</th>
          <th class="salary">Salary</th>
          <th class="posted">Posted</th>
          <th class="actions">Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    ${more}
  `;
  state.pageArmed = true;
  fitJobList();
}

function updateListMore() {
  const list = $("list");
  if (!list) return;
  const shown = state.jobs.length;
  const total = state.jobTotal || shown;
  const next = state.hasMoreJobs
    ? `<button type="button" class="list-more" data-load-more>Showing ${shown} of ${total}. Scroll for more.</button>`
    : `<div class="list-more">Showing ${shown} of ${total}.</div>`;
  const current = list.querySelector(".list-more");
  if (current) {
    current.outerHTML = next;
  } else {
    list.insertAdjacentHTML("beforeend", next);
  }
}

async function loadNextPage() {
  if (!state.hasMoreJobs || state.loadingMore) return;
  state.loadingMore = true;
  state.pageArmed = false;
  const list = $("list");
  const tbody = list?.querySelector("tbody");
  const start = state.jobs.length;
  try {
    const limit = state.pageSize === 0 ? 100 : state.pageSize;
    const payload = await api(stateUrl({ offset: start, limit }));
    const rows = payload.jobs || [];
    state.jobs = state.jobs.concat(rows);
    state.jobTotal = payload.job_total ?? state.jobTotal;
    state.hasMoreJobs = Boolean(payload.has_more_jobs);
    state.shownCount = state.jobs.length;
    if (tbody && rows.length) {
      tbody.insertAdjacentHTML(
        "beforeend",
        rows.map((job, index) => jobRowHtml(job, start + index)).join("")
      );
      updateListMore();
      paintDisplayedCounts();
    } else {
      paintJobTable();
    }
  } catch (error) {
    setNote(error.message, true);
  } finally {
    state.loadingMore = false;
  }
}

function listNearBottom() {
  if (!state.hasMoreJobs || state.loadingMore) {
    return false;
  }
  const list = $("list");
  const target = list?.querySelector(".list-more") || list;
  if (!target) return false;
  return target.getBoundingClientRect().top <= window.innerHeight + 80;
}

function pageSizeValue(value) {
  const text = String(value ?? "").trim().toLowerCase();
  if (text === "all" || text === "0") {
    return 0;
  }
  const raw = Number(text);
  if (!Number.isFinite(raw)) {
    return 20;
  }
  return Math.max(1, Math.min(200, Math.round(raw)));
}

function pageSizeLabel(count) {
  return count === 0 ? "All" : String(count);
}

function pageChunk(jobs) {
  if (state.pageSize === 0) {
    return Math.max((jobs || visibleJobs()).length, 1);
  }
  return state.pageSize;
}

function paintPageSizePresets() {
  document.querySelectorAll("[data-page-size]").forEach((button) => {
    button.classList.toggle("is-active", Number(button.dataset.pageSize) === state.pageSize);
  });
}

async function savePageSize(value) {
  const count = pageSizeValue(value);
  state.pageSize = count;
  state.shownCount = count === 0 ? state.jobTotal || 0 : count;
  $("page-size").value = pageSizeLabel(count);
  paintPageSizePresets();
  try {
    await api("/api/display-count", {
      method: "POST",
      body: JSON.stringify({ count }),
    });
    await refreshState();
  } catch (error) {
    setNote(error.message, true);
  }
}

function shortLink(value) {
  const text = String(value || "");
  return text.slice(0, 30);
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderHistory(items) {
  const box = $("history");
  if (!box) return;
  const profiles = [];
  const links = [];
  const seenProfiles = new Set();
  const seenHosts = new Set();
  for (const item of items || []) {
    if (item.profile && !seenProfiles.has(item.profile)) {
      seenProfiles.add(item.profile);
      profiles.push(item);
    }
    const host = item.host || "";
    const url = item.url || "";
    if (item.kind === "visit" && (host || url) && !seenHosts.has(host || url)) {
      seenHosts.add(host || url);
      links.push(item);
    }
  }
  if (!profiles.length && !links.length) {
    box.innerHTML = `<div class="note">No profile or visit history yet.</div>`;
    return;
  }
  const profileHtml = profiles.map((item) => {
    const profile = (item.name || item.profile || "Chrome profile").replace(/\s*[·•]\s*Chrome open$/i, "");
    const pic = `<img class="history-pic" alt="" src="/api/profiles/picture?dir=${encodeURIComponent(item.profile)}" />`;
    return `<div class="history-item">
      ${pic}
      <span class="history-main">${escapeHtml(profile)}</span>
    </div>`;
  }).join("");
  const linkHtml = links.map((item) => {
    const link = item.host || item.url || "";
    const pic = item.profile
      ? `<img class="history-pic" alt="" src="/api/profiles/picture?dir=${encodeURIComponent(item.profile)}" />`
      : `<span class="history-pic"></span>`;
    return `<div class="history-item">
      ${pic}
      <span class="history-link" title="${escapeHtml(item.url || link)}">${escapeHtml(link)}</span>
    </div>`;
  }).join("");
  box.innerHTML = `
    ${profiles.length ? `<div class="history-group"><h3>Profiles</h3>${profileHtml}</div>` : ""}
    ${links.length ? `<div class="history-group"><h3>Posted links</h3>${linkHtml}</div>` : ""}
  `;
}

async function refreshState() {
  const payload = await api(stateUrl({ offset: 0 }));
  const selected = Array.isArray(payload.selected_profiles) && payload.selected_profiles.length
    ? payload.selected_profiles
    : payload.selected_profile
      ? [payload.selected_profile]
      : [];
  state.selectedProfiles = normalizeSelectedProfiles(selected);
  state.selectedProfile = state.selectedProfiles[0] || "";
  paintSelectedProfiles();
  renderJobs(payload);
}

async function loadProfiles() {
  const payload = await api("/api/profiles");
  const current = await api(stateUrl({ offset: 0, limit: 1 }));
  const selected = Array.isArray(current.selected_profiles) && current.selected_profiles.length
    ? current.selected_profiles
    : current.selected_profile
      ? [current.selected_profile]
      : [];
  renderProfiles(payload.profiles, selected);
  if (state.selectedProfiles.length) {
    await saveSelectedProfiles(state.selectedProfiles);
  }
}

async function startCrawler() {
  try {
    if (state.fullCrawlDone) {
      setNote("Checking for new links...");
      await api("/api/crawler/update", { method: "POST" });
    } else {
      setNote("Starting full recommend crawl...");
      await api("/api/crawler/start", { method: "POST" });
    }
    setControls({ running: true, full_crawl_done: state.fullCrawlDone });
    await refreshState();
  } catch (error) {
    setNote(error.message, true);
  }
}

async function stopCrawler() {
  try {
    await api("/api/crawler/stop", { method: "POST" });
    setNote("Stopping...");
    await refreshState();
  } catch (error) {
    setNote(error.message, true);
  }
}

function closeProfileList() {
  const list = $("profile-list");
  const field = $("profile-field");
  if (!list) return;
  list.hidden = true;
  if (field) field.setAttribute("aria-expanded", "false");
}

function openProfileList() {
  const list = $("profile-list");
  const field = $("profile-field");
  if (!list) return;
  list.hidden = false;
  if (field) field.setAttribute("aria-expanded", "true");
}

function toggleProfileList() {
  const list = $("profile-list");
  if (!list) return;
  if (list.hidden) openProfileList();
  else closeProfileList();
}

async function toggleProfile(directory) {
  if (!directory) return;
  const selected = normalizeSelectedProfiles(state.selectedProfiles);
  const index = selected.indexOf(directory);
  if (index >= 0) {
    selected.splice(index, 1);
  } else {
    selected.push(directory);
  }
  try {
    await saveSelectedProfiles(selected);
  } catch (error) {
    setNote(error.message, true);
  }
}

async function removeProfile(directory) {
  if (!directory) return;
  const selected = normalizeSelectedProfiles(state.selectedProfiles).filter(
    (item) => item !== directory
  );
  try {
    await saveSelectedProfiles(selected);
  } catch (error) {
    setNote(error.message, true);
  }
}

$("profile-field").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-remove-profile]");
  if (chip) {
    event.preventDefault();
    event.stopPropagation();
    removeProfile(chip.dataset.removeProfile || "");
    return;
  }
  event.preventDefault();
  toggleProfileList();
});

$("profile-btn").addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  toggleProfileList();
});

$("profile-list").addEventListener("click", (event) => {
  const option = event.target.closest("[data-profile]");
  if (!option) return;
  event.preventDefault();
  event.stopPropagation();
  toggleProfile(option.dataset.profile || "");
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".profile-menu")) closeProfileList();
});

$("sort").addEventListener("change", async (event) => {
  await api("/api/sort", {
    method: "POST",
    body: JSON.stringify({ sort: event.target.value }),
  });
  await refreshState();
});

$("feed-sort").addEventListener("change", async (event) => {
  const sort = event.target.value === "recommended" ? "recommended" : "recent";
  await api("/api/feed-sort", {
    method: "POST",
    body: JSON.stringify({ sort }),
  });
  await refreshState();
  setNote(
    sort === "recent"
      ? "Jobright order set to Most recent. Update will stop at the last first list when the feed is newest-first."
      : "Jobright order set to Recommended. Update will use the original method."
  );
});

function localDay(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftMonth(stamp, delta) {
  const [year, month] = stamp.split("-").map(Number);
  const date = new Date(year, month - 1 + delta, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function renderCalendar(payload) {
  const cal = $("day-cal");
  if (!cal) return;
  const days = payload.posted_days || {};
  const keys = Object.keys(days).sort();
  const fallback =
    payload.today || payload.date_to || payload.date_from || payload.date_max || keys[keys.length - 1] ||
    localDay();
  if (!state.calMonth) state.calMonth = fallback.slice(0, 7);
  const [year, month] = state.calMonth.split("-").map(Number);
  const first = new Date(year, month - 1, 1);
  const startPad = first.getDay();
  const lastDate = new Date(year, month, 0).getDate();
  const selected = payload.date_from && payload.date_from === payload.date_to ? payload.date_from : "";
  const label = first.toLocaleString("en-US", { month: "short", year: "numeric" });
  const cells = [];
  for (let i = 0; i < startPad; i += 1) cells.push("<span class=\"day-cal-empty\"></span>");
  for (let day = 1; day <= lastDate; day += 1) {
    const stamp = `${state.calMonth}-${String(day).padStart(2, "0")}`;
    const raw = days[stamp];
    const count = raw && typeof raw === "object" ? Number(raw.count || 0) : Number(raw || 0);
    const active = selected === stamp ? " is-selected" : "";
    const saved = count ? " has-jobs" : "";
    const badge = count ? `<span class="day-cal-badge">${count}</span>` : "";
    cells.push(
      `<button type="button" class="day-cal-day${active}${saved}" data-cal-day="${stamp}" title="${count ? `${count} saved links` : ""}" ${count ? "" : "disabled"}><span>${day}</span>${badge}</button>`
    );
  }
  cal.innerHTML = `
    <div class="day-cal-head">
      <button type="button" class="day-cal-nav" data-cal-nav="-1" aria-label="Previous month">‹</button>
      <span class="day-cal-label">${escapeHtml(label)}</span>
      <button type="button" class="day-cal-nav" data-cal-nav="1" aria-label="Next month">›</button>
    </div>
    <div class="day-cal-dow">${["S", "M", "T", "W", "T", "F", "S"].map((name) => `<span>${name}</span>`).join("")}</div>
    <div class="day-cal-grid">${cells.join("")}</div>
  `;
}

$("day-cal").addEventListener("click", async (event) => {
  const nav = event.target.closest("[data-cal-nav]");
  if (nav) {
    state.calMonth = shiftMonth(state.calMonth || localDay().slice(0, 7), Number(nav.dataset.calNav));
    const payload = await api(`/api/state?tab=${state.tab}`);
    renderCalendar(payload);
    return;
  }
  const day = event.target.closest("[data-cal-day]")?.dataset.calDay;
  if (!day) return;
  await applyDates(day, day);
});

async function applyDates(dateFrom, dateTo) {
  await api("/api/dates", {
    method: "POST",
    body: JSON.stringify({ date_from: dateFrom || "", date_to: dateTo || "" }),
  });
  await refreshState();
}

$("date-from").addEventListener("change", () => applyDates($("date-from").value, $("date-to").value));
$("date-to").addEventListener("change", () => applyDates($("date-from").value, $("date-to").value));
$("date-clear").addEventListener("click", () => applyDates("", ""));

const SHORT_UPDATE_SECONDS = 2 * 60;

async function applyUpdateWait() {
  const seconds = parseRemain($("update-eta").value);
  if (seconds == null) {
    paintEta();
    return;
  }
  if (seconds < SHORT_UPDATE_SECONDS) {
    const ok = window.confirm(
      `This wait is very short (${formatRemain(seconds * 1000)}). The list will update often. Use it anyway?`
    );
    if (!ok) {
      paintEta();
      return;
    }
  }
  const result = await api("/api/update-wait", {
    method: "POST",
    body: JSON.stringify({ seconds }),
  });
  state.nextUpdateAt = result.next_update_at || "";
  state.updateSeconds = result.update_seconds || seconds;
  paintEta();
  if (seconds < SHORT_UPDATE_SECONDS) {
    setNote(`Warning: next update is in ${formatRemain(seconds * 1000)}. That is a very short wait.`);
  }
}

$("update-eta").addEventListener("click", (event) => event.stopPropagation());
$("update-eta").addEventListener("mousedown", (event) => event.stopPropagation());
$("update-eta").addEventListener("focus", () => {
  state.editingEta = true;
});
$("update-eta").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    event.target.blur();
  }
});
$("update-eta").addEventListener("blur", async () => {
  try {
    await applyUpdateWait();
  } catch (error) {
    setNote(error.message, true);
  } finally {
    state.editingEta = false;
    paintEta();
  }
});

async function applyLinkHost(host, query = "") {
  const body = host
    ? { host: host || "", query: "" }
    : { host: "", query: query || "" };
  await api("/api/link-host", {
    method: "POST",
    body: JSON.stringify(body),
  });
  await refreshState();
}

function closeLinkHostList() {
  $("link-host-list").hidden = true;
  $("link-host-btn").setAttribute("aria-expanded", "false");
}

function openLinkHostList() {
  const list = $("link-host-list");
  list.hidden = false;
  $("link-host-btn").setAttribute("aria-expanded", "true");
  const search = $("link-host-search");
  if (search) {
    search.value = state.hostQuery;
    requestAnimationFrame(() => search.focus());
  }
}

$("link-host-btn").addEventListener("click", (event) => {
  event.preventDefault();
  if ($("link-host-list").hidden) {
    openLinkHostList();
  } else {
    closeLinkHostList();
  }
});

$("link-host-list").addEventListener("click", (event) => {
  const allMatching = event.target.closest("[data-host-query]");
  if (allMatching) {
    const query = String(allMatching.dataset.hostQuery || "").trim();
    closeLinkHostList();
    applyLinkHost("", query);
    return;
  }
  const option = event.target.closest("[data-host]");
  if (!option) return;
  state.hostQuery = "";
  state.linkHostQuery = "";
  closeLinkHostList();
  applyLinkHost(option.dataset.host || "");
});

$("link-host-search").addEventListener("input", (event) => {
  state.hostQuery = event.target.value;
  paintLinkHostOptions();
});
$("link-host-search").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    const query = String(event.target.value || state.hostQuery || "").trim();
    closeLinkHostList();
    if (!query) {
      state.linkHostQuery = "";
      applyLinkHost("");
      return;
    }
    // Show every job link from every matching host group in the table.
    applyLinkHost("", query);
  }
});

$("link-host").addEventListener("change", (event) => applyLinkHost(event.target.value));
$("link-host-clear").addEventListener("click", () => {
  state.hostQuery = "";
  state.linkHostQuery = "";
  closeLinkHostList();
  applyLinkHost("");
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".link-host-menu")) closeLinkHostList();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeLinkHostList();
    closeProfileList();
  }
});

$("join-jobright").addEventListener("click", openJoinModal);
$("session").addEventListener("click", openJoinModal);
$("logout").addEventListener("click", async () => {
  try {
    await api("/api/jobright/logout", { method: "POST" });
    closeJoinModal();
    setNote("Logged out.");
    await refreshState();
  } catch (error) {
    setNote(error.message, true);
  }
});
$("join-close").addEventListener("click", closeJoinModal);
$("join-modal").addEventListener("click", (event) => {
  if (event.target.closest("[data-close-modal]")) closeJoinModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("join-modal").hidden) closeJoinModal();
});

$("connect").addEventListener("click", async () => {
  try {
    setJoinNote("Connecting to Jobright...");
    await api("/api/jobright/login", {
      method: "POST",
      body: JSON.stringify({
        email: $("email").value.trim(),
        password: $("password").value,
      }),
    });
    $("password").value = "";
    closeJoinModal();
    setNote("Jobright connected.");
    await loadFilters();
    await loadProfiles();
    await refreshState();
  } catch (error) {
    setJoinNote(error.message, true);
  }
});

$("session-btn").addEventListener("click", async () => {
  try {
    setJoinNote("Saving Jobright session...");
    await api("/api/jobright/session", {
      method: "POST",
      body: JSON.stringify({ session_id: $("session-id").value.trim() }),
    });
    $("session-id").value = "";
    closeJoinModal();
    setNote("Jobright session saved.");
    await loadFilters();
    await refreshState();
  } catch (error) {
    setJoinNote(error.message, true);
  }
});

$("filter-save").addEventListener("click", async () => {
  try {
    setNote("Saving posted date to Jobright...");
    const result = await api("/api/filters", {
      method: "POST",
      body: JSON.stringify({
        daysAgo: Number($("filter-days").value),
      }),
    });
    fillFilterForm(result);
    setNote("Posted date saved on Jobright. Click Start or Update to load matching links.");
  } catch (error) {
    setNote(error.message, true);
  }
});

function bulkCount() {
  const raw = Number($("bulk-count").value);
  if (!Number.isFinite(raw)) {
    return 10;
  }
  return Math.max(1, Math.min(20, Math.round(raw)));
}

async function bulkOpen() {
  try {
    if (!normalizeSelectedProfiles(state.selectedProfiles).length) {
      setNote("Select at least one Chrome profile first.", true);
      return;
    }
    const count = bulkCount();
    $("bulk-count").value = String(count);
    setNote(`Opening the same ${count} links in every selected Chrome profile...`);
    const result = await api("/api/jobs/bulk-open", {
      method: "POST",
      body: JSON.stringify({ count, tab: state.tab }),
    });
    setNote(result.message || `Opened ${result.opened} links.`);
    await refreshState();
  } catch (error) {
    setNote(error.message, true);
  }
}

$("bulk-open").addEventListener("click", () => {
  bulkOpen();
});
$("bulk-count").addEventListener("focus", () => {
  state.editingBulk = true;
});
$("bulk-count").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    event.target.blur();
    bulkOpen();
  }
});
$("bulk-count").addEventListener("blur", () => {
  $("bulk-count").value = String(bulkCount());
  state.editingBulk = false;
});

$("start").addEventListener("click", startCrawler);
$("stop").addEventListener("click", stopCrawler);

$("filters-toggle").addEventListener("click", () => {
  state.filtersOpen = !state.filtersOpen;
  $("filters").hidden = !state.filtersOpen;
  $("filters-toggle").setAttribute("aria-expanded", String(state.filtersOpen));
});

function onListPageScroll() {
  const list = $("list");
  const near = listNearBottom();
  const scrolled = window.scrollY > 8 || (list && list.scrollTop > 4);
  if (near && state.pageArmed && scrolled) {
    loadNextPage();
    return;
  }
  if (!near) {
    state.pageArmed = true;
  }
}

$("list").addEventListener("scroll", onListPageScroll, { passive: true });
window.addEventListener("scroll", onListPageScroll, { passive: true });

$("job-search").addEventListener("input", () => {
  state.jobQuery = $("job-search").value || "";
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => {
    refreshState().catch((error) => setNote(error.message, true));
  }, 200);
});

document.querySelectorAll("[data-page-size]").forEach((button) => {
  button.addEventListener("click", () => {
    savePageSize(button.dataset.pageSize);
  });
});

$("page-size").addEventListener("focus", () => {
  state.editingPageSize = true;
});
$("page-size").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    event.target.blur();
  }
});
$("page-size").addEventListener("blur", () => {
  state.editingPageSize = false;
  savePageSize($("page-size").value);
});

document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.tab = button.dataset.tab;
    document.querySelectorAll("[data-tab]").forEach((item) => {
      item.setAttribute("aria-current", item === button ? "true" : "false");
    });
    await refreshState();
  });
});

$("list").addEventListener("click", async (event) => {
  if (event.target.closest("[data-load-more]")) {
    loadNextPage();
    return;
  }
  const posted = event.target.closest("[data-date]");
  if (posted?.dataset.date) {
    await applyDates(posted.dataset.date, posted.dataset.date);
    return;
  }
  const openId = event.target.closest("[data-open]")?.dataset.open;
  if (!openId) {
    return;
  }
  event.preventDefault();
  try {
    const result = await api(`/api/jobs/${openId}/open`, { method: "POST" });
    if (result.message) setNote(result.message);
    await refreshState();
  } catch (error) {
    setNote(error.message, true);
  }
});

$("list").addEventListener("change", async (event) => {
  const select = event.target.closest("select[data-job]");
  if (!select) {
    return;
  }
  const jobId = select.dataset.job;
  const action = select.value;
  if (!action) {
    return;
  }
  try {
    if (action === "open") {
      const result = await api(`/api/jobs/${jobId}/open`, { method: "POST" });
      if (result.message) setNote(result.message);
    }
    if (action === "applied") {
      const result = await api(`/api/jobs/${jobId}/applied`, { method: "POST" });
      setNote(result.message, !result.synced);
    }
    await refreshState();
  } catch (error) {
    setNote(error.message, true);
    await refreshState();
  }
});

function currentTheme() {
  return document.documentElement.dataset.theme === "night" ? "night" : "day";
}

function applyTheme(theme) {
  const next = theme === "night" ? "night" : "day";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("joblink-theme", next);
  const button = $("theme-toggle");
  if (!button) return;
  button.setAttribute("aria-label", next === "night" ? "Switch to day style" : "Switch to night style");
  button.title = next === "night" ? "Day" : "Night";
}

function fitJobList() {
  const list = $("list");
  if (list) {
    list.style.maxHeight = "";
  }
}

function fitHistoryPane() {
  const side = document.querySelector(".side-col");
  if (!side) return;
  const top = side.getBoundingClientRect().top;
  side.style.maxHeight = `${Math.max(240, Math.round(window.innerHeight - top - 16))}px`;
}

window.addEventListener("resize", () => {
  fitHistoryPane();
  fitJobList();
});
window.addEventListener("scroll", fitHistoryPane, { passive: true });

$("theme-toggle").addEventListener("click", () => {
  applyTheme(currentTheme() === "night" ? "day" : "night");
});
applyTheme(currentTheme());

loadProfiles()
  .then(refreshState)
  .then(loadFilters)
  .catch((error) => setNote(error.message, true));
