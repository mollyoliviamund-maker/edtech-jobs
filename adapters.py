"""
adapters.py — fortified

Changes in this pass, all driven by real failures observed via verify_endpoints.py:

1. Session hardening: dropped the custom "JobWatcher/x.x" UA suffix and the
   blanket "Content-Type: application/json" header on every request - both are
   dead giveaways of automation that a real browser would never send on a GET,
   and are exactly the kind of thing bot-detection/WAFs fingerprint on. Added
   full browser-like Accept/Accept-Language headers instead.
2. request_with_retry(): 403/404/406/429 get one retry with backoff before
   being trusted, since we've directly observed WAFs returning those codes for
   accounts later confirmed live (classdojo, grammarly, Princeton Review/"review",
   and a platform-wide false-405 across every configured iCIMS host in one run).
   Every HTTP call in this file now goes through this instead of a bare
   SESSION.get(), including inside the various HTML-scrape loops.
3. Greenhouse: HTML-board fallback when the JSON API 404s - some accounts
   disable public API access while keeping boards.greenhouse.io / job-boards.
   greenhouse.io live (confirmed real cases: classdojo, grammarly).
4. Lever: same HTML-board fallback pattern, mirroring Greenhouse and Workable
   (confirmed real case: "review"/The Princeton Review).
5. iCIMS: fetch_icims previously only tried /jobs/search, which came back as a
   uniform HTTP 405 across every single configured host in one verify run -
   including PowerSchool, an unquestionably real iCIMS customer. Now tries a
   short list of realistic path/param variants before giving up.
6. Workday (Playwright): dropped the same suspicious UA suffix used in the
   browser context, for the same fingerprinting reason as #1.
7. Added a small polite jittered delay between requests inside every loop that
   hits many detail pages back-to-back on the same host (iCIMS, ADP,
   SuccessFactors, Jobvite, Pereless, DirectEmployers, and the Greenhouse/Lever/
   Workable HTML fallbacks) - hammering one host with dozens of rapid requests
   is itself a big part of what trips bot detection in the first place.

8. Workday: the JS payload hardcoded searchText:"music" on every request,
   regardless of what KEYWORDS was set to. That was harmless only while
   KEYWORDS also happened to be "music"; with the current seniority/domain
   keywords it would have returned ~nothing from all 14 Workday companies
   while every other platform worked normally. Now sends an empty search.
   Because an empty search returns the full board instead of a few keyword
   hits, the old single limit=50 request would have silently truncated any
   company with 50+ openings (Scholastic, Wiley, Kaplan, Elsevier all
   qualify), so it now paginates with a 20-page / 1000-posting cap.
9. fetch_dejobs: previously put the raw KEYWORDS value into a single ?q=
   parameter. That worked for the single word "music" but not for a
   pipe-separated list ("?q=director%7Cmanager%7C..." is not a query any job
   board understands). Now issues one search per DOMAIN term (first 6, which
   utils.py deliberately interleaves across personalization AND assessment
   so neither half is missed) and applies the real filter locally.

Matching/parsing/output contracts are otherwise unchanged: mk_row() and
matches_kw() signatures and the return shape of each fetch_*() are the same.
The one behavioral change is that Workday and DirectEmployers now retrieve a
broader candidate set and rely on local filtering, rather than pre-filtering
server-side with a keyword that no longer reflects the configuration.
"""

from typing import List, Dict, Any
import os, sys, json, datetime, re, time, random, warnings

import requests
from bs4 import BeautifulSoup
try:
    from bs4 import MarkupResemblesLocatorWarning
except Exception:  # pragma: no cover
    class MarkupResemblesLocatorWarning(UserWarning):
        pass

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import quote_plus, urljoin

from utils import matches_kw, mk_row

# Silence noisy bs4 warning in CI
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

# ---------------- Shared HTTP session ----------------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        # Plain, standard browser UA - no custom suffix. A "JobWatcher/2.4"-style
        # tag on the end is a free bot signature; real browsers never send that.
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        # Deliberately NOT setting a blanket Content-Type here - it was applied
        # to every GET before, which no real browser does, and is another easy
        # bot fingerprint.
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

# Status codes that are as likely to mean "a WAF didn't like this request" as
# "this genuinely doesn't exist" - confirmed directly via verify_endpoints.py
# runs against known-good accounts. Worth one retry with backoff before trusting.
_SOFT_FAIL_CODES = {403, 404, 406, 429}

def request_with_retry(url: str, method: str = "get", retries: int = 2, backoff: float = 1.5, **kwargs):
    """GET/HEAD with a retry for soft-fail status codes. Used everywhere in this
    file instead of a bare SESSION.get()/post()."""
    last_resp = None
    for attempt in range(retries + 1):
        try:
            resp = SESSION.request(method, url, timeout=REQ_TIMEOUT, allow_redirects=True, **kwargs)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(backoff * (attempt + 1) + random.uniform(0, 0.5))
            continue
        last_resp = resp
        if resp.status_code not in _SOFT_FAIL_CODES:
            return resp
        if attempt < retries:
            time.sleep(backoff * (attempt + 1) + random.uniform(0, 0.5))
    return last_resp

def _polite_sleep(min_s: float = 0.15, max_s: float = 0.45):
    """Small jittered pause between requests inside a detail-page loop.
    Hammering one host with dozens of rapid-fire requests is itself a large
    part of what trips bot detection - this alone won't defeat a determined
    WAF, but it materially reduces how "scrapey" the traffic pattern looks."""
    time.sleep(random.uniform(min_s, max_s))

# ------------- salary helpers -------------
_MONEY = re.compile(
    r"(\$\s?\d[\d,]*(?:\.\d{2})?\s?(?:-\s?\$\s?\d[\d,]*(?:\.\d{2})?)?\s*(?:k|per\s?(?:year|yr|hour|hr|annum|month))?)",
    re.IGNORECASE,
)

def _first_salary_from_text(text: str) -> str:
    if not text:
        return ""
    m = _MONEY.search(text.replace("\xa0", " "))
    return m.group(1).strip() if m else ""

def _salary_from_metadata(md) -> str:
    """Greenhouse and others sometimes include 'metadata' with name/value pairs."""
    try:
        for item in (md or []):
            name = (item.get("name") or "").lower()
            val = (item.get("value") or "").strip()
            if not val:
                continue
            if any(k in name for k in ["salary", "compensation", "pay", "wage", "rate"]):
                return val
    except Exception:
        pass
    return ""

def _salary_from_html(html: str) -> str:
    """Generic HTML scan: look for labeled salary blocks, then fallback to regex."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    selectors = [
        ".salary", ".compensation", ".pay", "[itemprop='baseSalary']",
        "[class*='salary']", "[class*='compensation']", "[class*='pay']",
    ]
    for sel in selectors:
        try:
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(" ", strip=True)
                if txt:
                    # If it doesn't look like money, still allow, as some sites say "Competitive"
                    return txt
        except Exception:
            pass
    # fallback to money regex
    return _first_salary_from_text(html)

# ================== GREENHOUSE ==================
def _fetch_greenhouse_html(slug: str) -> List[Dict[str, Any]]:
    """
    Fallback for Greenhouse accounts that have disabled public JSON API access
    (boards-api returns 404) while still serving a public HTML board. Confirmed
    real-world cases: classdojo, grammarly - the slug is correct, only the JSON
    API is switched off. Scrapes the HTML board instead, same pattern as
    fetch_workable's HTML fallback below.

    Caveat: title extraction (<h1>) is reliable across Greenhouse templates;
    the location selectors below are a best-effort guess (untested against
    live page source from this environment) and may come back empty even when
    a real location exists on the page. Doesn't affect keyword matching when
    MATCH_SCOPE=title (the default) - only affects the displayed location.
    """
    out: List[Dict[str, Any]] = []
    scope = os.getenv("MATCH_SCOPE", "title").lower()

    list_urls = [f"https://job-boards.greenhouse.io/{slug}", f"https://boards.greenhouse.io/{slug}"]
    links = set()
    for list_url in list_urls:
        try:
            r = request_with_retry(list_url)
        except Exception as e:
            _warn(f"[WARN] greenhouse(html):{slug} list fetch failed for {list_url} -> {e}")
            continue
        if r is None or r.status_code >= 400:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select(f"a[href*='/{slug}/jobs/'], a[href*='jobs/']"):
            href = a.get("href") or ""
            if not href:
                continue
            # urljoin resolves any relative form (leading "/", leading "//", or
            # a nested relative path) against the page we actually fetched.
            # A previous version only special-cased hrefs starting with
            # exactly "/{slug}" and left everything else as a broken relative
            # path that silently failed later - urljoin has no such gap.
            abs_href = urljoin(list_url, href)
            if re.search(r"/jobs/\d+", abs_href) and slug in abs_href:
                links.add(abs_href.split("?")[0])
        if links:
            break  # got a working board host, no need to try the other domain too

    for job_url in list(links)[:150]:
        _polite_sleep()
        try:
            # retries=1 here (vs. the default 2 used for the primary existence
            # checks above): this runs once per job on boards with up to 150
            # postings, so the retry budget is deliberately tighter to bound
            # worst-case runtime on companies with many dead/flaky links.
            jr = request_with_retry(job_url, retries=1)
        except Exception:
            continue
        if jr is None or jr.status_code >= 400:
            continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1")
        title = title_el.get_text(strip=True) if title_el else ""
        loc_el = jsoup.select_one("[class*='location'], .job__location, .location")
        location = loc_el.get_text(strip=True) if loc_el else ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
        if matches_kw(title, location, desc):
            m = re.search(r"/jobs/(\d+)", job_url)
            job_id = m.group(1) if m else job_url
            out.append(
                mk_row(slug, "greenhouse", title, location, job_id, job_url, "",
                       "title" if scope == "title" else "html_fallback",
                       salary=salary)
            )
    return out


def fetch_greenhouse(slug: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = request_with_retry(url)
    except Exception as e:
        _warn(f"[WARN] greenhouse:{slug} -> request failed: {e}")
        return out
    if r is None or r.status_code >= 400:
        status = r.status_code if r is not None else "no response"
        if r is not None and r.status_code == 404:
            # Slug may still be correct - some accounts disable the public JSON API
            # while keeping the HTML board live. Try scraping that before giving up.
            html_jobs = _fetch_greenhouse_html(slug)
            if html_jobs:
                _warn(f"[INFO] greenhouse:{slug} API 404, recovered {len(html_jobs)} jobs via HTML fallback")
                return html_jobs
        _warn(f"[WARN] greenhouse:{slug} -> HTTP {status}")
        return out
    try:
        jobs = (r.json() or {}).get("jobs", []) or []
    except Exception:
        _warn(f"[WARN] greenhouse:{slug} invalid JSON")
        return out

    scope = os.getenv("MATCH_SCOPE", "title").lower()
    for j in jobs:
        title = j.get("title") or ""
        job_id = str(j.get("id") or "")
        abs_url = j.get("absolute_url") or ""
        offices = j.get("offices") or []
        location = ", ".join([o.get("name","") for o in offices if isinstance(o, dict)]) or ""
        desc = j.get("content") or ""
        salary = _salary_from_metadata(j.get("metadata")) or _salary_from_html(desc)
        if matches_kw(title, location, desc):
            posted_iso = (j.get("updated_at") or "").replace(" ", "T")
            if posted_iso and not posted_iso.endswith("Z"):
                posted_iso += "Z"
            out.append(
                mk_row(slug, "greenhouse", title, location, job_id, abs_url, posted_iso,
                       "title" if scope == "title" else "title_or_description",
                       salary=salary)
            )
    return out

# ================== LEVER ==================
_LEVER_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

def _fetch_lever_html(slug: str) -> List[Dict[str, Any]]:
    """
    Fallback for Lever accounts where the JSON API 404s but the HTML board is
    still live - same pattern as the Greenhouse fallback above. Confirmed real
    case: "review" (The Princeton Review) came back 404 on the API in one
    verify run despite very recent live postings on jobs.lever.co/review.

    Caveat: same as the Greenhouse fallback - title (<h1>) is reliable,
    location selectors are a best-effort guess untested against live page
    source and may come back empty on a real, valid posting.
    """
    out: List[Dict[str, Any]] = []
    scope = os.getenv("MATCH_SCOPE", "title").lower()

    list_url = f"https://jobs.lever.co/{slug}"
    try:
        r = request_with_retry(list_url)
    except Exception as e:
        _warn(f"[WARN] lever(html):{slug} list fetch failed -> {e}")
        return out
    if r is None or r.status_code >= 400:
        return out

    soup = BeautifulSoup(r.text, "lxml")
    links = set()
    for a in soup.select(f"a[href*='/{slug}/']"):
        href = a.get("href") or ""
        if not href:
            continue
        abs_href = urljoin(list_url, href)
        if _LEVER_ID_RE.search(abs_href):
            links.add(abs_href.split("?")[0])

    for job_url in list(links)[:150]:
        _polite_sleep()
        try:
            jr = request_with_retry(job_url, retries=1)
        except Exception:
            continue
        if jr is None or jr.status_code >= 400:
            continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1")
        title = title_el.get_text(strip=True) if title_el else ""
        loc_el = jsoup.select_one(
            "[class*='location'], .posting-categories .location, .sort-by-time.posting-category"
        )
        location = loc_el.get_text(strip=True) if loc_el else ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
        if matches_kw(title, location, desc):
            m = _LEVER_ID_RE.search(job_url)
            job_id = m.group(0) if m else job_url
            out.append(
                mk_row(slug, "lever", title, location, job_id, job_url, "",
                       "title" if scope == "title" else "html_fallback",
                       salary=salary)
            )
    return out


def fetch_lever(slug: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = request_with_retry(url)
    except Exception as e:
        _warn(f"[WARN] lever:{slug} -> request failed: {e}")
        return out
    if r is None or r.status_code >= 400:
        status = r.status_code if r is not None else "no response"
        if r is not None and r.status_code == 404:
            html_jobs = _fetch_lever_html(slug)
            if html_jobs:
                _warn(f"[INFO] lever:{slug} API 404, recovered {len(html_jobs)} jobs via HTML fallback")
                return html_jobs
        _warn(f"[WARN] lever:{slug} -> HTTP {status}")
        return out
    try:
        postings = r.json()
        if not isinstance(postings, list):
            return out
    except Exception:
        _warn(f"[WARN] lever:{slug} invalid JSON")
        return out

    scope = os.getenv("MATCH_SCOPE", "title").lower()
    for p in postings:
        title = p.get("text") or p.get("title") or ""
        location = (p.get("categories") or {}).get("location") or ""
        job_id = p.get("id") or p.get("leverId") or p.get("hostedJobId") or ""
        apply_url = p.get("hostedUrl") or p.get("applyUrl") or (p.get("urls") or {}).get("apply") or ""
        desc = p.get("descriptionPlain") or p.get("description") or ""
        salary = _first_salary_from_text(desc)  # Lever often embeds range in description

        matched = None
        if matches_kw(title, location, desc):
            matched = "title" if scope == "title" else "title_or_description"
        elif apply_url and scope != "title":
            # Wrapped so one flaky description fetch can't lose every posting
            # already matched earlier in this same loop.
            try:
                jr = request_with_retry(apply_url, retries=1)
            except Exception:
                jr = None
            if jr is not None and jr.status_code < 400:
                salary = salary or _salary_from_html(jr.text)
                if matches_kw("", "", jr.text or ""):
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
            out.append(mk_row(slug, "lever", title, location, str(job_id), apply_url, iso, matched, salary=salary))
    return out

# ================== WORKDAY (headless via Playwright) ==================
def fetch_workday_headless(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        # Plain, standard Chrome UA - no custom suffix, for the same
        # fingerprinting reason as the shared SESSION above. NOTE: unlike the
        # Greenhouse/Lever/iCIMS fixes in this file, this change is precautionary,
        # not a confirmed bug fix - the 406s we actually observed came from
        # verify_endpoints.py's separate plain-HTTP check, not from this
        # Playwright-driven function, which drives a real Chromium instance and
        # was never directly observed failing. Removing an unnecessary
        # automation fingerprint is good hygiene regardless, but treat this one
        # as "hardened," not "fixed," until it's actually been tested.
        context = browser.new_context(ignore_https_errors=True, user_agent=UA)
        page = context.new_page()
        page.on("response", lambda resp: parse_jobs_url(resp.url))

        # warm up / find candidate links
        for url in [f"https://{host}/", f"https://{host}/en-US", f"https://{host}/career", f"https://{host}/careers"]:
            try:
                page.goto(url, wait_until="networkidle", timeout=45000); break
            except Exception:
                pass
        try:
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.getAttribute('href'))") or []
        except Exception:
            hrefs = []
        cand = []
        for h in hrefs:
            if not h: continue
            if h.startswith("//"): h = "https:" + h
            if h.startswith("/"):  h = f"https://{host}{h}"
            if host not in h: continue
            if any(k in h.lower() for k in ["career","careers","jobs","search","/en-"]):
                cand.append(h)
        for u in cand[:25]:
            if sniff["variant"]: break
            try:
                page.goto(u, wait_until="networkidle", timeout=45000)
            except Exception:
                pass

        # sniff or guess /wday/* endpoint
        if not sniff["variant"]:
            tenants = [sniff["tenant"], tenant_hint, tenant_hint.lower() if tenant_hint else None,
                       tenant_hint.upper() if tenant_hint else None]
            sites = [sniff["site"], site_hint, "Careers", "External", "Jobs", "US", "Students", "Campus"]
            tenants = [t for t in tenants if t]
            sites   = [s for s in sites if s]
            tried=set()
            for v in ("cxs","cx"):
                for t in tenants:
                    for s_ in sites:
                        key=(v,t,s_)
                        if key in tried: continue
                        tried.add(key)
                        try:
                            res = page.evaluate(
                                """async ({v,t,s})=>{
                                    // Empty searchText: this call only needs to confirm the
                                    // endpoint shape is correct, and an empty search is the
                                    // most reliable way to get a 200 regardless of what
                                    // keywords the run is configured for.
                                    const body={appliedFacets:{},limit:1,offset:0,searchText:""};
                                    const r=await fetch(`/wday/${v}/${t}/${s}/jobs`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body),credentials:'same-origin'});
                                    return {ok:r.ok,status:r.status};
                                }""", {"v":v, "t":t, "s":s_}
                            )
                            if res and res.get("ok"):
                                sniff["variant"], sniff["tenant"], sniff["site"] = v, t, s_
                                break
                        except Exception:
                            pass
                    if sniff["variant"]: break
                if sniff["variant"]: break

        if not sniff["variant"]:
            context.close(); browser.close()
            _warn(f"[WARN] workday({company}) sniff failed")
            return out

        # Final query. Two changes from the original here:
        #
        #  1. searchText is now "" instead of a hardcoded "music". The original
        #     sent "music" on every request regardless of what KEYWORDS was set
        #     to. That was harmless only while KEYWORDS happened to also be
        #     "music" - with the seniority/domain keywords now in use, a
        #     server-side "music" filter would return ~nothing from all 14
        #     Workday companies while every other platform worked fine.
        #
        #  2. Because an empty search returns the FULL board rather than a
        #     handful of keyword hits, the old single request with limit=50
        #     would now silently truncate any company with more than 50 open
        #     roles (Scholastic, Wiley, Kaplan and Elsevier are all well past
        #     that). So this now pages through results and filters locally.
        WD_PAGE_SIZE = 50
        WD_MAX_PAGES = 20  # hard cap => at most 1000 postings/company, bounds runtime

        all_jobs = []
        for page_idx in range(WD_MAX_PAGES):
            payload = dict(sniff)
            payload["limit"] = WD_PAGE_SIZE
            payload["offset"] = page_idx * WD_PAGE_SIZE
            try:
                resp = page.evaluate(
                    """async ({variant,tenant,site,limit,offset})=>{
                        const body={appliedFacets:{},limit:limit,offset:offset,searchText:""};
                        const r=await fetch(`/wday/${variant}/${tenant}/${site}/jobs`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body),credentials:'same-origin'});
                        if(!r.ok) return null;
                        return await r.json();
                    }""", payload
                )
            except Exception as e:
                _warn(f"[WARN] workday({company}) page {page_idx} failed -> {e}")
                break
            if not resp:
                break
            batch = (resp.get("jobPostings") or resp.get("jobs") or [])
            if not batch:
                break
            all_jobs.extend(batch)
            if len(batch) < WD_PAGE_SIZE:
                break  # short page => last page
        else:
            _warn(f"[INFO] workday({company}) hit the {WD_MAX_PAGES}-page cap "
                  f"({WD_MAX_PAGES * WD_PAGE_SIZE} postings); results may be truncated")

        if all_jobs:
            for j in all_jobs:
                title = (j.get("title") or "").strip()
                urlp = j.get("externalPath") or j.get("externalUrl") or j.get("url") or ""
                if urlp and urlp.startswith("/"): urlp = f"https://{host}{urlp}"
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
                salary = _first_salary_from_text(desc)  # Workday often exposes in description text
                if matches_kw(title, loc, desc):
                    jid = j.get("id") or j.get("jobId") or j.get("externalId") or ""
                    posted = (j.get("postedOn") or j.get("startDate") or "").replace(" ", "T")
                    if posted and not posted.endswith("Z"): posted += "Z"
                    out.append(mk_row(company, "workday", title, loc, str(jid), urlp, posted,
                                      "title" if os.getenv("MATCH_SCOPE","title")=="title" else "title_or_description",
                                      salary=salary))

        context.close(); browser.close()
    return out

# ================== WORKABLE ==================
def fetch_workable(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    account = (entry.get("account") or "").strip()
    company = entry.get("company") or account
    if not account:
        return out

    def try_api(acc: str) -> List[Dict[str, Any]]:
        url = f"https://apply.workable.com/api/v3/accounts/{acc}/jobs?state=published"
        try:
            r = request_with_retry(url)
        except Exception as e:
            _warn(f"[WARN] workable:{acc} -> request failed: {e}")
            return []
        if r is None:
            _warn(f"[WARN] workable:{acc} -> no response")
            return []
        if r.status_code == 404:
            _warn(f"[INFO] workable:{acc} API 404 (using HTML fallback)")
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
            desc = j.get("description") or ""
            salary = _first_salary_from_text(desc)
            if matches_kw(title, location, desc):
                jid = j.get("id") or j.get("shortcode") or ""
                rows.append(mk_row(company, "workable", title, location, str(jid), url, "",
                                   "title" if os.getenv("MATCH_SCOPE","title")=="title" else "title_or_description",
                                   salary=salary))
        return rows

    def try_html(acc: str) -> List[Dict[str, Any]]:
        list_url = f"https://apply.workable.com/{acc}/"
        try:
            r = request_with_retry(list_url)
        except Exception as e:
            _warn(f"[WARN] workable(html):{acc} list -> request failed: {e}")
            return []
        if r is None or r.status_code >= 400:
            _warn(f"[WARN] workable(html):{acc} list -> HTTP {r.status_code if r is not None else 'no response'}")
            return []
        soup = BeautifulSoup(r.text, "lxml")
        links = set()
        for a in soup.select("a[href*='/j/']"):
            href = a.get("href") or ""
            if href.startswith("//"): href = "https:" + href
            elif href.startswith("/"): href = "https://apply.workable.com" + href
            elif not href.startswith("http"): href = f"https://apply.workable.com/{acc}/{href}"
            if f"/{acc}/j/" in href:
                links.add(href)

        rows: List[Dict[str, Any]] = []
        for job_url in list(links)[:100]:
            _polite_sleep()
            try:
                jr = request_with_retry(job_url, retries=1)
            except Exception:
                continue
            if jr is None or jr.status_code >= 400:
                continue
            jsoup = BeautifulSoup(jr.text, "lxml")
            title = (jsoup.find("h1").get_text(strip=True) if jsoup.find("h1") else "")
            location = (jsoup.select_one("[data-ui='job-location'], .job-location, .job-details__location") or "").get_text(strip=True) if jsoup.select_one("[data-ui='job-location'], .job-location, .job-details__location") else ""
            desc = jsoup.get_text(" ", strip=True)[:20000]
            salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
            if matches_kw(title, location, desc):
                m = re.search(r"/j/([A-Z0-9]+)/", job_url)
                jid = m.group(1) if m else job_url
                rows.append(mk_row(company, "workable", title, location, jid, job_url, "",
                                   "title" if os.getenv("MATCH_SCOPE","title")=="title" else "html_text",
                                   salary=salary))
        return rows

    out.extend(try_api(account))
    if not out and "-" in account:
        out.extend(try_api(account.replace("-", "")))
    if not out:
        out.extend(try_html(account))
        if not out and "-" in account:
            out.extend(try_html(account.replace("-", "")))
    return out

# ================== iCIMS ==================
def _text(el) -> str:
    try:
        return el.get_text(strip=True)
    except Exception:
        return ""

def fetch_icims(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    A bare GET to /jobs/search with no query string previously came back as a
    uniform HTTP 405 across every single configured iCIMS host in one run -
    including PowerSchool, which is unquestionably a real, live iCIMS customer.
    A platform-wide identical failure is the WAF/endpoint-shape rejecting the
    request itself, not proof every host is wrong, so this now tries a short
    list of realistic path/param variants before giving up.
    """
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host:
        return out

    candidates = ["/jobs/search?ss=1", "/jobs/search", "/jobs", "/"]
    r = None
    for p in candidates:
        try:
            r = request_with_retry(f"https://{host}{p}")
        except Exception as e:
            _warn(f"[WARN] icims:{host}{p} -> request failed: {e}")
            r = None
            continue
        if r is not None and r.status_code == 200:
            break
    if r is None or r.status_code != 200:
        _warn(f"[WARN] icims:{host} -> HTTP {r.status_code if r is not None else 'no response'} on all path variants")
        return out

    soup = BeautifulSoup(r.text, "lxml")
    job_links = set()
    for a in soup.select("a[href*='/jobs/']"):
        href = a.get("href")
        if not href: continue
        if href.startswith("/"): href = f"https://{host}{href}"
        job_links.add(href)

    for job_url in list(job_links)[:120]:
        _polite_sleep()
        try:
            jr = request_with_retry(job_url, retries=1)
        except Exception:
            continue
        if jr is None or jr.status_code >= 400:
            continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title = _text(jsoup.find("h1") or jsoup.find("h2") or jsoup.select_one(".iCIMS_JobTitle"))
        loc_el = jsoup.find("li", class_="iCIMS_JobLocation") or jsoup.find("span", class_="jobLocation")
        location = _text(loc_el)
        desc = jsoup.get_text(" ", strip=True)[:20000]
        salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
        if matches_kw(title, location, desc):
            m = re.search(r"/jobs/(\d+)", job_url)
            jid = m.group(1) if m else job_url
            out.append(mk_row(company, "icims", title, location, jid, job_url, "",
                              "title" if os.getenv("MATCH_SCOPE","title")=="title" else "html_text",
                              salary=salary))
    return out

# ================== Teamtailor ==================
def fetch_teamtailor(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host: return out
    url = f"https://{host}/jobs"
    try:
        r = request_with_retry(url)
    except Exception as e:
        _warn(f"[WARN] teamtailor:{host} -> request failed: {e}")
        return out
    if r is None or r.status_code >= 400:
        _warn(f"[WARN] teamtailor:{host} -> HTTP {r.status_code if r is not None else 'no response'}")
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
        salary = j.get("baseSalary") or _first_salary_from_text(desc)
        if matches_kw(title, location, desc):
            jid = j.get("identifier") or url
            out.append(mk_row(company, "teamtailor", title, location, str(jid), url, "",
                              "title" if os.getenv("MATCH_SCOPE","title")=="title" else "jsonld",
                              salary=str(salary) if salary else ""))
    return out

# ================== ADP Workforce Now ==================
def fetch_adp(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host: return out
    urls = [f"https://{host}/career-center/search", f"https://{host}/career-center"]
    for url in urls:
        try:
            r = request_with_retry(url)
        except Exception:
            continue
        if r is None or r.status_code >= 400:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href*='job?'], a[href*='/job/'], a[href*='positions']"):
            job_url = a.get("href") or ""
            if not job_url: continue
            if job_url.startswith("/"): job_url = f"https://{host}{job_url}"
            _polite_sleep()
            try:
                jr = request_with_retry(job_url, retries=1)
            except Exception:
                continue
            if jr is None or jr.status_code >= 400: continue
            jsoup = BeautifulSoup(jr.text, "lxml")
            title_el = jsoup.find("h1") or jsoup.find("h2")
            title = title_el.get_text(strip=True) if title_el else ""
            location = ""
            desc = jsoup.get_text(" ", strip=True)[:20000]
            salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
            if matches_kw(title, location, desc):
                out.append(mk_row(company, "adp", title, location, job_url, job_url, "",
                                  "title" if os.getenv("MATCH_SCOPE","title")=="title" else "html_text",
                                  salary=salary))
    return out

# ================== SAP SuccessFactors ==================
def fetch_successfactors(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host: return out
    try:
        r = request_with_retry(f"https://{host}")
    except Exception as e:
        _warn(f"[WARN] successfactors:{host} -> request failed: {e}")
        return out
    if r is None or r.status_code >= 400:
        _warn(f"[WARN] successfactors:{host} -> HTTP {r.status_code if r is not None else 'no response'}")
        return out
    soup = BeautifulSoup(r.text, "lxml")
    links = set(a.get("href") for a in soup.select("a[href*='job']") if a.get("href"))
    for job_url in list(links)[:80]:
        if job_url.startswith("/"): job_url = f"https://{host}{job_url}"
        _polite_sleep()
        try:
            jr = request_with_retry(job_url, retries=1)
        except Exception:
            continue
        if jr is None or jr.status_code >= 400: continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1") or jsoup.find("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        location = ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
        if matches_kw(title, location, desc):
            out.append(mk_row(company, "successfactors", title, location, job_url, job_url, "",
                              "title" if os.getenv("MATCH_SCOPE","title")=="title" else "html_text",
                              salary=salary))
    return out

# ================== Jobvite ==================
def fetch_jobvite(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host: return out
    try:
        r = request_with_retry(f"https://{host}/")
    except Exception as e:
        _warn(f"[WARN] jobvite:{host} -> request failed: {e}")
        return out
    if r is None or r.status_code >= 400:
        _warn(f"[WARN] jobvite:{host} -> HTTP {r.status_code if r is not None else 'no response'}")
        return out
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.select("a[href*='jobs?'], a[href*='/job/'], a[href*='?jvi='], a[href*='/jobs/']"):
        job_url = a.get("href") or ""
        if not job_url: continue
        if job_url.startswith("/"): job_url = f"https://{host}{job_url}"
        _polite_sleep()
        try:
            jr = request_with_retry(job_url, retries=1)
        except Exception:
            continue
        if jr is None or jr.status_code >= 400: continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1") or jsoup.find("h2")
        title = title_el.get_text(strip=True) if title_el else ""
        location = ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
        if matches_kw(title, location, desc):
            out.append(mk_row(company, "jobvite", title, location, job_url, job_url, "",
                              "title" if os.getenv("MATCH_SCOPE","title")=="title" else "html_text",
                              salary=salary))
    return out

# ================== Pereless / Submit4Jobs ==================
def fetch_pereless(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host: return out
    try:
        r = request_with_retry(f"https://{host}/")
    except Exception as e:
        _warn(f"[WARN] pereless:{host} -> request failed: {e}")
        return out
    if r is None or r.status_code >= 400:
        _warn(f"[WARN] pereless:{host} -> HTTP {r.status_code if r is not None else 'no response'}")
        return out
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.select("a[href*='JobDetails'], a[href*='?fulldesc='], a[href*='/job/'], a[href*='?pos=']"):
        job_url = a.get("href") or ""
        if not job_url: continue
        if job_url.startswith("/"): job_url = f"https://{host}{job_url}"
        _polite_sleep()
        try:
            jr = request_with_retry(job_url, retries=1)
        except Exception:
            continue
        if jr is None or jr.status_code >= 400: continue
        jsoup = BeautifulSoup(jr.text, "lxml")
        title_el = jsoup.find("h1") or jsoup.find("h2") or jsoup.title
        title = title_el.get_text(strip=True) if title_el else ""
        location = ""
        desc = jsoup.get_text(" ", strip=True)[:20000]
        salary = _salary_from_html(jr.text) or _first_salary_from_text(desc)
        if matches_kw(title, location, desc):
            out.append(mk_row(company, "pereless", title, location, job_url, job_url, "",
                              "title" if os.getenv("MATCH_SCOPE","title")=="title" else "html_text",
                              salary=salary))
    return out

# ================== .jobs / DirectEmployers (e.g., pearson.jobs) ==================
def fetch_dejobs(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    host = (entry.get("host") or "").strip().rstrip("/")
    company = entry.get("company") or host
    if not host: return out

    # The original sent the raw KEYWORDS value as a single ?q= parameter. That
    # worked when KEYWORDS was the single word "music", but the value is now a
    # long pipe-separated list, and "?q=director%7Cmanager%7Cvice+president..."
    # is not a query any job board understands - it would return junk or
    # nothing. So: issue one search per DOMAIN term (those are the narrow,
    # high-signal ones - Pearson has thousands of postings, so searching
    # "director" server-side would be uselessly broad), then apply the real
    # seniority+domain filter locally via matches_kw() as usual.
    from utils import DOMAIN_KW, KW as SENIORITY_KW

    def _terms(s: str) -> List[str]:
        return [t.strip() for t in (s or "").split("|") if t.strip()]

    search_terms = _terms(DOMAIN_KW) or _terms(SENIORITY_KW)
    # Cap the number of server-side searches: each term costs 2 requests per
    # host, and the local filter is what actually decides matches anyway.
    MAX_DEJOBS_QUERIES = 6
    search_terms = search_terms[:MAX_DEJOBS_QUERIES]
    if not search_terms:
        search_terms = [""]

    queries: List[str] = []
    for term in search_terms:
        queries.append(f"https://{host}/search/?q={quote_plus(term)}")
        queries.append(f"https://{host}/jobs/?q={quote_plus(term)}")

    def collect_links(html: str) -> List[str]:
        soup = BeautifulSoup(html or "", "lxml")
        links = []
        for a in soup.select("a[href*='/job/']"):
            href = a.get("href") or ""
            if href.startswith("//"): href = "https:" + href
            elif href.startswith("/"): href = f"https://{host}{href}"
            if host in href: links.append(href)
        seen=set(); ordered=[]
        for u in links:
            if u not in seen:
                seen.add(u); ordered.append(u)
        return ordered

    job_links: List[str] = []
    for u in queries:
        try:
            r = request_with_retry(u)
            if r is not None and r.status_code < 400:
                job_links += collect_links(r.text)
        except Exception:
            pass

    if not job_links:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                c = b.new_context(ignore_https_errors=True, user_agent=UA)
                p = c.new_page()
                for u in queries:
                    try:
                        p.goto(u, wait_until="networkidle", timeout=45000)
                        links = p.eval_on_selector_all("a[href*='/job/']", "els => els.map(a => a.href)") or []
                        for L in links:
                            if host in L and L not in job_links: job_links.append(L)
                        if job_links: break
                    except Exception:
                        pass
                c.close(); b.close()
        except Exception:
            _warn(f"[WARN] dejobs:{host} headless fallback failed")

    scope = os.getenv("MATCH_SCOPE", "title").lower()
    for job_url in job_links[:100]:
        _polite_sleep()
        try:
            jr = request_with_retry(job_url, retries=1)
            if jr is None or jr.status_code >= 400: continue
            jsoup = BeautifulSoup(jr.text, "lxml")
            title_el = jsoup.find("h1") or jsoup.select_one("h1.job-title")
            title = (title_el.get_text(strip=True) if title_el else "").strip()
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
            html = jr.text or ""
            desc = jsoup.get_text(" ", strip=True)[:20000]
            salary = _salary_from_html(html) or _first_salary_from_text(desc)
            if matches_kw(title, loc, desc):
                m = re.search(r"/([A-Za-z0-9]{16,})/job/?", job_url)
                jid = m.group(1) if m else job_url
                out.append(mk_row(company, "dejobs", title, loc, jid, job_url, "",
                                  "title" if scope == "title" else "title_or_description",
                                  salary=salary))
        except Exception:
            pass
    return out
