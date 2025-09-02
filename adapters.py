from typing import List, Dict, Any, Optional
import sys, json, datetime, re, warnings

import requests
from bs4 import BeautifulSoup
try:
    # bs4 >=4.12
    from bs4 import MarkupResemblesLocatorWarning
except Exception:  # pragma: no cover
    class MarkupResemblesLocatorWarning(UserWarning):
        pass

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils import job_matches_music, mk_row

# Silence noisy BS4 warning in CI logs
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

# --------- shared HTTP session ----------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 MusicJobs/2.1"
        ),
        "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        "Content-Type": "application/json",
    })
    retry = Retry(
        total=5,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

SESSION = make_session()
REQ_TIMEOUT = 35

def _warn(msg: str):
    print(msg, file=sys.stderr)

# ---------------- Greenhouse ----------------
def fetch_greenhouse(slug: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = SESSION.get(url, timeout=REQ_TIMEOUT)
    if r.status_code >= 400:
        _warn(f"[WARN] greenhouse:{slug} -> HTTP {r.status_code}")
        return out
    try:
        jobs = (r.json() or {}).get("jobs", []) or []
    except Exception:
        _warn(f"[WARN] greenhouse:{slug} invalid JSON")
        return out
    for j in jobs:
        title = j.get("title") or ""
        job_id = str(j.get("id") or "")
        abs_url = j.get("absolute_url") or ""
        offices = j.get("offices") or []
        location = ", ".join([o.get("name","") for o in offices if isinstance(o, dict)]) or ""
        desc = j.get("content") or ""
        if job_matches_music(f"{title}\n{location}\n{desc}"):
            posted_iso = (j.get("updated_at") or "").replace(" ", "T")
            if posted_iso and not posted_iso.endswith("Z"):
                posted_iso += "Z"
            out.append(mk_row(slug, "greenhouse", title, location, job_id, abs_url, posted_iso, "title_or_description"))
    return out

# ---------------- Lever ----------------
def fetch_lever(slug: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = SESSION.get(url, timeout=REQ_TIMEOUT)
    if r.status_code >= 400:
        _warn(f"[WARN] lever:{slug} -> HTTP {r.status_code}")
        return out
    try:
        postings = r.json()
        if not isinstance(postings, list):
            return out
    except Exception:
        _warn(f"[WARN] lever:{slug} invalid JSON")
        return out
    for p in postings:
        title = p.get("text") or p.get("title") or ""
        location = (p.get("categories") or {}).get("location") or ""
        job_id = p.get("id") or p.get("leverId") or p.get("hostedJobId") or ""
        apply_url = p.get("hostedUrl") or p.get("applyUrl") or (p.get("urls") or {}).get("apply") or ""
        desc = p.get("descriptionPlain") or p.get("description") or ""
        matched = None
        if job_matches_music(f"{title}\n{location}\n{desc}"):
            matched = "title_or_description"
        elif apply_url:
            jr = SESSION.get(apply_url, timeout=REQ_TIMEOUT)
            if jr.status_code < 400 and job_matches_music(jr.text or ""):
                matched = "description_html"
        if matched:
            iso = ""
            created_at = p.get("createdAt") or p.get("created_at")
            if created_at:
                try:
                    iso = (
                        datetime.datetime.utcfromtimestamp(int(created_at)/1000)
                        .replace(microsecond=0).isoformat() + "Z"
                    )
                except Exception:
                    iso = ""
            out.append(mk_row(slug, "lever", title, location, str(job_id), apply_url, iso, matched))
    return out

# ---------------- Workday (headless sniffer via Playwright) ----------------
def fetch_workday_headless(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Headless Workday adapter that *sniffs* the portal's own jobs XHR to discover:
      - variant: /wday/cx/ or /wday/cxs/
      - tenant:  actually used by the portal (may differ in case)
      - site:    the real site token
    Then it posts the same endpoint with searchText="music".
    """
    from playwright.sync_api import sync_playwright

    out: List[Dict[str, Any]] = []

    host = (entry.get("host") or "").strip()
    tenant_hint = (entry.get("tenant") or "").strip()
    site_hint = (entry.get("site") or "").strip()
    company = entry.get("company") or tenant_hint or host
    if not host:
        _warn(f"[WARN] workday(headless) missing host: {entry}")
        return out

    sniff = {"variant": None, "tenant": None, "site": None}

    def parse_jobs_url(url: str):
        m = re.search(r"/wday/(cxs|cx)/([^/]+)/([^/]+)/jobs", url)
        if m:
            sniff["variant"], sniff["tenant"], sniff["site"] = m.group(1), m.group(2), m.group(3)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True, user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 MusicJobs/HD2"
        ))
        page = context.new_page()

        def on_response(resp):
            url = resp.url
            if "/wday/cx" in url or "/wday/cxs" in url:
                parse_jobs_url(url)
        page.on("response", on_response)

        # establish cookies
        for url in [f"https://{host}/", f"https://{host}/en-US", f"https://{host}/career", f"https://{host}/careers"]:
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
                break
            except Exception:
                continue

        # try to trigger the XHR
        candidate_paths: List[str] = []
        try:
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))") or []
        except Exception:
            hrefs = []

        for h in hrefs:
            if not h:
                continue
            if h.startswith("//"):
                h = "https:" + h
            if h.startswith("/"):
                h = f"https://{host}{h}"
            if host not in h:
                continue
            if any(k in h.lower() for k in ["career", "careers", "jobs", "search", "/en-"]):
                candidate_paths.append(h)

        hardcoded = [
            f"https://{host}/en-US/Careers",
            f"https://{host}/en-US/External",
            f"https://{host}/en-US/Jobs",
            f"https://{host}/en-US/{site_hint}" if site_hint else None,
        ]
        candidate_paths = [u for u in hardcoded if u] + candidate_paths[:20]

        for u in candidate_paths[:25]:
            if sniff["variant"]:
                break
            try:
                page.goto(u, wait_until="networkidle", timeout=45000)
            except Exception:
                continue

        # direct attempts if still nothing
        def try_direct(variant: str, tenant: str, site: str):
            return page.evaluate(
                """async ({variant, tenant, site}) => {
                    const payload = {appliedFacets:{}, limit:50, offset:0, searchText:"music"};
                    const res = await fetch(`/wday/${variant}/${tenant}/${site}/jobs`, {
                      method: 'POST',
                      headers: {'content-type':'application/json'},
                      body: JSON.stringify(payload),
                      credentials: 'same-origin'
                    });
                    return {ok: res.ok, status: res.status};
                }""",
                {"variant": variant, "tenant": tenant, "site": site},
            )

        if not sniff["variant"]:
            tenants = [sniff["tenant"], tenant_hint, tenant_hint.lower() if tenant_hint else None,
                       tenant_hint.upper() if tenant_hint else None]
            sites = [sniff["site"], site_hint, "Careers", "External", "Jobs", "US", "Students", "Campus"]
            tenants = [t for t in tenants if t]
            sites = [s for s in sites if s]
            tried = set()
            for v in ("cxs", "cx"):
                for t in tenants:
                    for s in sites:
                        key = (v, t, s)
                        if key in tried:
                            continue
                        tried.add(key)
                        try:
                            res = try_direct(v, t, s)
                            if res and res.get("ok"):
                                sniff["variant"], sniff["tenant"], sniff["site"] = v, t, s
                                break
                        except Exception:
                            continue
                    if sniff["variant"]:
                        break
                if sniff["variant"]:
                    break

        if not sniff["variant"]:
            context.close(); browser.close()
            _warn(f"[WARN] workday({company}) sniff failed (no jobs endpoint found)")
            return out

        # final query for "music"
        resp = page.evaluate(
            """async ({variant, tenant, site}) => {
                const payload = {appliedFacets:{}, limit:50, offset:0, searchText:"music"};
                const r = await fetch(`/wday/${variant}/${tenant}/${site}/jobs`, {
                  method:'POST',
                  headers:{'content-type':'application/json'},
                  body: JSON.stringify(payload),
                  credentials:'same-origin'
                });
                if (!r.ok) return null;
                return await r.json();
            }""",
            sniff,
        )

        if resp:
            jobs = (resp.get("jobPostings") or resp.get("jobs") or [])
            for j in jobs:
                title = (j.get("title") or "").strip()
                urlp = j.get("externalPath") or j.get("externalUrl") or j.get("url") or ""
                if urlp and urlp.startswith("/"):
                    urlp = f"https://{host}{urlp}"
                loc = ""
                locs = j.get("locations") or j.get("bulletFields") or []
                if isinstance(locs, list):
                    loc = ", ".join(str(x) for x in locs if x)
                elif isinstance(locs, str):
                    loc = locs
                desc = " ".join([
                    j.get("shortDescription") or "",
                    j.get("jobPostingInfo", {}).get("jobDescription", "")
                ]).strip()
                if job_matches_music(f"{title}\n{loc}\n{desc}"):
                    jid = j.get("id") or j.get("jobId") or j.get("externalId") or ""
                    posted = (j.get("postedOn") or j.get("startDate") or "").replace(" ", "T")
                    if posted and not posted.endswith("Z"): posted += "Z"
                    out.append(mk_row(company, "workday", title, loc, str(jid), urlp, posted, "title_or_description"))

        context.close()
        browser.close()
    return out

# ---------------- .jobs / DirectEmployers (HTML + headless fallback) ----------------
import os
from urllib.parse import quote_plus

def fetch_dejobs(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Scrapes a .jobs / DirectEmployers career site (e.g., pearson.jobs).
    Strategy:
      1) Try server-rendered search result pages: https://<host>/search/?q=<kw> and /jobs/?q=<kw>
      2) Collect job detail links containing '/job/' (same host).
      3) If nothing found, use Playwright to render and extract those links.
      4) Visit job pages, match keyword(s) against title/description, emit rows.

    companies.yaml:
      dejobs:
        - { company: Pearson, host: pearson.jobs }
    """
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out

    kw = os.getenv("KEYWORDS", "music")  # supports alternation via env like: music|audio|sound
    queries = [f"https://{host}/search/?q={quote_plus(kw)}",
               f"https://{host}/jobs/?q={quote_plus(kw)}"]

    def collect_links_from_html(html: str) -> List[str]:
        soup = BeautifulSoup(html or "", "lxml")
        links = []
        for a in soup.select("a[href*='/job/']"):
            href = a.get("href")
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = f"https://{host}{href}"
            if host in href:
                links.append(href)
        # de-dup keep order
        seen, ordered = set(), []
        for u in links:
            if u not in seen:
                seen.add(u); ordered.append(u)
        return ordered

    # 1) Try static pages
    job_links: List[str] = []
    for url in queries:
        try:
            r = SESSION.get(url, timeout=REQ_TIMEOUT)
            if r.status_code < 400:
                job_links += collect_links_from_html(r.text)
        except Exception:
            continue

    # 2) Headless fallback if we saw no links
    if not job_links:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(ignore_https_errors=True)
                page = ctx.new_page()
                for url in queries:
                    try:
                        page.goto(url, wait_until="networkidle", timeout=45000)
                        # collect anchors with '/job/' in href
                        links = page.eval_on_selector_all(
                            "a[href*='/job/']",
                            "els => els.map(a => a.href)"
                        ) or []
                        for u in links:
                            if host in u and u not in job_links:
                                job_links.append(u)
                        if job_links:
                            break
                    except Exception:
                        continue
                ctx.close(); browser.close()
        except Exception:
            _warn(f"[WARN] dejobs:{host} headless fallback failed")

    # 3) Visit job pages and emit matches
    for job_url in job_links[:80]:
        try:
            jr = SESSION.get(job_url, timeout=REQ_TIMEOUT)
            if jr.status_code >= 400:
                continue
            jsoup = BeautifulSoup(jr.text, "lxml")
            # Title
            title_el = jsoup.find("h1") or jsoup.select_one("h1.job-title")
            title = (title_el.get_text(strip=True) if title_el else "").strip()
            # Heuristic location: often a line just under H1 or a <p> containing comma+country code
            loc = ""
            try:
                h1_parent = title_el.find_parent() if title_el else None
                if h1_parent:
                    nxt = h1_parent.find_next(string=True)
                    if nxt:
                        cand = str(nxt).strip()
                        if len(cand) <= 80 and ("," in cand or cand.isupper()):
                            loc = cand
            except Exception:
                pass
            # Full text for keyword match
            desc = jsoup.get_text(" ", strip=True)[:20000]
            if job_matches_music(f"{title}\n{loc}\n{desc}"):
                # ID: grab the GUID-like token before /job/
                m = re.search(r"/([A-Za-z0-9]{16,})/job/?", job_url)
                jid = m.group(1) if m else job_url
                out.append(mk_row(company, "dejobs", title, loc, jid, job_url, "", "title_or_description"))
        except Exception:
            continue

    return out


# ---------------- Workable (API first, HTML fallback) ----------------
def fetch_workable(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Workable adapter with API-first, HTML-fallback strategy.
    - API:  https://apply.workable.com/api/v3/accounts/<account>/jobs?state=published
    - HTML: https://apply.workable.com/<account>/  (parse job links, then job pages)
    """
    out: List[Dict[str, Any]] = []
    account = (entry.get("account") or "").strip()
    company = entry.get("company") or account
    if not account:
        return out

    # ---------- 1) Try public API ----------
    def try_api(acc: str) -> List[Dict[str, Any]]:
        url = f"https://apply.workable.com/api/v3/accounts/{acc}/jobs?state=published"
        r = SESSION.get(url, timeout=REQ_TIMEOUT)
        if r.status_code == 404:
            print(f"[INFO] workable:{acc} API 404 (using HTML fallback)", file=sys.stderr)
            return []
        if r.status_code >= 400:
            _warn(f"[WARN] workable:{acc} -> HTTP {r.status_code}")
            return []
        try:
            jobs = (r.json() or {}).get("results", []) or []
        except Exception:
            _warn(f"[WARN] workable:{acc} invalid JSON")
            return []
        rows: List[Dict[str, Any]] = []
        for j in jobs:
            title = j.get("title") or ""
            url = j.get("url") or ""
            loc = j.get("location") or {}
            location = ", ".join([loc.get("city",""), loc.get("region",""), loc.get("country","")]).strip(", ").replace(",,", ",")
            desc = (j.get("description") or "") if isinstance(j.get("description"), str) else ""
            if job_matches_music(f"{title}\n{location}\n{desc}"):
                jid = j.get("id") or j.get("shortcode") or ""
                rows.append(mk_row(company, "workable", title, location, str(jid), url, "", "title_or_description"))
        return rows

    # ---------- 2) HTML fallback ----------
    def try_html(acc: str) -> List[Dict[str, Any]]:
        list_url = f"https://apply.workable.com/{acc}/"
        r = SESSION.get(list_url, timeout=REQ_TIMEOUT)
        if r.status_code >= 400:
            _warn(f"[WARN] workable(html):{acc} list -> HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "lxml")

        links = set()
        for a in soup.select("a[href*='/j/']"):
            href = a.get("href")
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://apply.workable.com" + href
            elif href.startswith("http"):
                pass
            else:
                href = f"https://apply.workable.com/{acc}/{href}"
            if f"/{acc}/j/" in href:
                links.add(href)

        rows: List[Dict[str, Any]] = []
        for job_url in list(links)[:100]:
            jr = SESSION.get(job_url, timeout=REQ_TIMEOUT)
            if jr.status_code >= 400:
                continue
            jsoup = BeautifulSoup(jr.text, "lxml")
            h1 = jsoup.find("h1")
            title = h1.get_text(strip=True) if h1 else ""
            loc_el = jsoup.select_one("[data-ui='job-location'], .job-location, .job-details__location")
            location = loc_el.get_text(strip=True) if loc_el else ""
            desc = jsoup.get_text(" ", strip=True)[:20000]
            if job_matches_music(f"{title}\n{location}\n{desc}"):
                m = re.search(r"/j/([A-Z0-9]+)/", job_url)
                jid = m.group(1) if m else job_url
                rows.append(mk_row(company, "workable", title, location, jid, job_url, "", "html_text"))
        return rows

    out.extend(try_api(account))
    if not out and "-" in account:
        out.extend(try_api(account.replace("-", "")))
    if not out:
        out.extend(try_html(account))
        if not out and "-" in account:
            out.extend(try_html(account.replace("-", "")))
    return out

# ---------------- iCIMS (HTML) ----------------
def _text(el) -> str:
    try:
        return el.get_text(strip=True)
    except Exception:
        return ""

def fetch_icims(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out

    search_url = f"https://{host}/jobs/search"
    r = SESSION.get(search_url, timeout=REQ_TIMEOUT)
    if r.status_code >= 400:
        _warn(f"[WARN] icims:{host} -> HTTP {r.status_code}")
        return out

    soup = BeautifulSoup(r.text, "lxml")
    job_links = set()
    for a in soup.select("a[href*='/jobs/']"):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://{host}{href}"
        job_links.add(href)

    for job_url in list(job_links)[:120]:
        jr = SESSION.get(job_url, timeout=REQ_TIMEOUT)
        if jr.status_code >= 400:
            continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title = _text(jsoup.select_one("h1")) or _text(jsoup.select_one("h2")) or _text(jsoup.select_one(".iCIMS_JobTitle"))
        loc_el = jsoup.find("li", class_="iCIMS_JobLocation") or jsoup.find("span", class_="jobLocation")
        location = _text(loc_el)
        desc = jsoup.get_text(" ", strip=True)[:20000]
        if job_matches_music(f"{title}\n{location}\n{desc}"):
            m = re.search(r"/jobs/(\d+)", job_url)
            jid = m.group(1) if m else job_url
            out.append(mk_row(company, "icims", title, location, jid, job_url, "", "html_text"))
    return out

# ---------------- Teamtailor (JSON-LD in HTML) ----------------
def fetch_teamtailor(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out
    url = f"https://{host}/jobs"
    r = SESSION.get(url, timeout=REQ_TIMEOUT)
    if r.status_code >= 400:
        _warn(f"[WARN] teamtailor:{host} -> HTTP {r.status_code}")
        return out
    soup = BeautifulSoup(r.text, "lxml")
    scripts = soup.find_all("script", type="application/ld+json")
    jobs = []
    for sc in scripts:
        try:
            data = json.loads(sc.string or "")
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                jobs.append(data)
            elif isinstance(data, list):
                for it in data:
                    if isinstance(it, dict) and it.get("@type") == "JobPosting":
                        jobs.append(it)
        except Exception:
            continue
    for j in jobs:
        title = j.get("title") or ""
        url = j.get("url") or ""
        location = ""
        loc = j.get("jobLocation", {})
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            location = ", ".join([addr.get("addressLocality",""), addr.get("addressRegion",""), addr.get("addressCountry","")]).strip(", ")
        desc = j.get("description") or ""
        if job_matches_music(f"{title}\n{location}\n{desc}"):
            jid = j.get("identifier") or url
            out.append(mk_row(company, "teamtailor", title, location, str(jid), url, "", "jsonld"))
    return out

# ---------------- ADP Workforce Now (HTML) ----------------
def fetch_adp(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out
    urls = [f"https://{host}/career-center/search", f"https://{host}/career-center"]
    for url in urls:
        r = SESSION.get(url, timeout=REQ_TIMEOUT)
        if r.status_code >= 400:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='job?'], a[href*='/job/'], a[href*='positions']"):
            job_url = a.get("href")
            if not job_url:
                continue
            if job_url.startswith("/"):
                job_url = f"https://{host}{job_url}"
            jr = SESSION.get(job_url, timeout=REQ_TIMEOUT)
            if jr.status_code >= 400:
                continue
            jsoup = BeautifulSoup(jr.text, "lxml")
            title_el = jsoup.find("h1") or jsoup.find("h2")
            title = title_el.get_text(strip=True) if title_el else ""
            location = ""
            desc = jsoup.get_text(" ", strip=True)[:20000]
            if job_matches_music(f"{title}\n{location}\n{desc}"):
                out.append(mk_row(company, "adp", title, location, job_url, job_url, "", "html_text"))
    return out

# ---------------- SAP SuccessFactors (HTML) ----------------
def fetch_successfactors(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out
    r = SESSION.get(f"https://{host}", timeout=REQ_TIMEOUT)
    if r.status_code >= 400:
        _warn(f"[WARN] successfactors:{host} -> HTTP {r.status_code}")
        return out
    soup = BeautifulSoup(r.text, "lxml")
    links = set(a.get("href") for a in soup.select("a[href*='job']") if a.get("href"))
    for href in list(links)[:80]:
        job_url = href
        if job_url.startswith("/"):
            job_url = f"https://{host}{job_url}"
        jr = SESSION.get(job_url, timeout=REQ_TIMEOUT)
        if jr.status_code >= 400:
            continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1") or jsoup.find("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        location = ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        if job_matches_music(f"{title}\n{location}\n{desc}"):
            out.append(mk_row(company, "successfactors", title, location, job_url, job_url, "", "html_text"))
    return out

# ---------------- Jobvite (HTML) ----------------
def fetch_jobvite(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out
    r = SESSION.get(f"https://{host}/", timeout=REQ_TIMEOUT)
    if r.status_code >= 400:
        _warn(f"[WARN] jobvite:{host} -> HTTP {r.status_code}")
        return out
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.select("a[href*='jobs?'], a[href*='/job/'], a[href*='?jvi='], a[href*='/jobs/']"):
        job_url = a.get("href")
        if not job_url:
            continue
        if job_url.startswith("/"):
            job_url = f"https://{host}{job_url}"
        jr = SESSION.get(job_url, timeout=REQ_TIMEOUT)
        if jr.status_code >= 400:
            continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1") or jsoup.find("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        location = ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        if job_matches_music(f"{title}\n{location}\n{desc}"):
            out.append(mk_row(company, "jobvite", title, location, job_url, job_url, "", "html_text"))
    return out

# ---------------- Pereless / Submit4Jobs (HTML) ----------------
def fetch_pereless(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out
    r = SESSION.get(f"https://{host}/", timeout=REQ_TIMEOUT)
    if r.status_code >= 400:
        _warn(f"[WARN] pereless:{host} -> HTTP {r.status_code}")
        return out
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.select("a[href*='JobDetails'], a[href*='?fulldesc='], a[href*='/job/'], a[href*='?pos=']"):
        job_url = a.get("href")
        if not job_url:
            continue
        if job_url.startswith("/"):
            job_url = f"https://{host}{job_url}"
        jr = SESSION.get(job_url, timeout=REQ_TIMEOUT)
        if jr.status_code >= 400:
            continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1") or jsoup.find("h2") or jsoup.title
        title = title_el.get_text(strip=True) if title_el else ""
        location = ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        if job_matches_music(f"{title}\n{location}\n{desc}"):
            out.append(mk_row(company, "pereless", title, location, job_url, job_url, "", "html_text"))
    return out
