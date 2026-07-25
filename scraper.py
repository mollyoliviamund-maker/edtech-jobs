"""
scraper.py — orchestrator for the multi-ATS job watcher.

Fortification changes in this pass, each backed by a reproduced failure (see
chat history / test scripts used to derive this):

1. Per-job crash isolation: one malformed job dict (e.g. missing 'job_id') no
   longer crashes the entire run. Previously this happened *after* every job
   from every company processed so far had already been printed as [NEW], but
   *before* save_seen() was reached - meaning a single bad job silently
   erased that whole run's dedup progress, causing every already-processed
   company to be re-reported as "new" again on the next run.
2. save_seen() now runs in a `finally` block, so even an unforeseen exception
   (or a Ctrl-C / SIGTERM mid-run) still persists whatever was found before
   the interruption, instead of losing it entirely.
3. Email digest dedup key now includes platform, not just (company, job_id).
   Two genuinely different postings (different platform, different title,
   different URL) that happen to share a company name and a numeric ID were
   being collapsed into one entry in the email - confirmed with Edmentum,
   which is configured on both Greenhouse and (historically) iCIMS.
4. email_body.md content is now HTML-escaped. Company/title/location strings
   come from live scraped data, and "Barnes & Noble Education" (a literal,
   already-configured company) demonstrates the raw "&" bug isn't
   hypothetical; a title containing "<" or ">" would silently vanish from
   the rendered email since it'd be parsed as an unknown tag.
5. When a run finds zero new jobs, any stale email_body.md from a previous
   run is now removed instead of left on disk - otherwise downstream
   automation that just emails "whatever's in this file" without separately
   checking whether *this* run wrote it would resend an old digest.
6. Malformed companies.yaml now produces a clean [ERROR] message and exit
   code 2, instead of a raw YAML parser traceback.
7. --company now also matches on "account" and "board" (Workable/Ashby-style
   entries identified by those fields rather than host/tenant).
8. Minor: removed pointless no-op lambda wrappers in FETCHERS; added an
   error counter to the final summary line for operational visibility (exit
   code behavior is unchanged - a handful of flaky companies still shouldn't
   fail an entire scheduled run, but you can now see how many failed at a
   glance rather than having to scroll stderr).

Everything else - the two-tier FETCHERS/DICT_FETCHERS dispatch, the CSV/seen
format via utils.py, the CLI flags - is unchanged.
"""

import os, sys, html, yaml, argparse
from typing import Dict, Any, List, Optional

from utils import load_seen, save_seen, append_csv
from adapters import (
    fetch_greenhouse, fetch_lever,
    fetch_workday_headless,
    fetch_workable, fetch_icims, fetch_teamtailor,
    fetch_adp, fetch_successfactors, fetch_jobvite, fetch_pereless,
    fetch_dejobs,
)

EMAIL_BODY_PATH = "email_body.md"

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever":      fetch_lever,
}

DICT_FETCHERS = {
    "workday":         fetch_workday_headless,
    "workable":        fetch_workable,
    "icims":           fetch_icims,
    "teamtailor":      fetch_teamtailor,
    "adp":             fetch_adp,
    "successfactors":  fetch_successfactors,
    "jobvite":         fetch_jobvite,
    "pereless":        fetch_pereless,
    "dejobs":          fetch_dejobs,
}
# NOTE: "ashby" and "rippling" are intentionally not registered here yet -
# fetch_ashby()/fetch_rippling() don't exist in adapters.py yet, even though
# companies.yaml already has config sections for both (marked inert there).
# Registering them here before those functions exist would be an ImportError,
# not a graceful no-op.


def _process_job(j: Dict[str, Any], seen: set, email_keys: set, email_jobs: List[Dict[str, Any]]) -> bool:
    """
    Handle one scraped job: dedupe against 'seen', append to CSV if new, and
    queue it for the email digest. Returns True if this was a genuinely new
    job. Isolated per-job (rather than inlined in each loop) so:
      (a) one malformed job dict can't abort every other job in the run, and
      (b) both the list-based and dict-based platform loops below use
          identical dedup logic - previously this ~15-line block was
          duplicated verbatim in both loops, which is exactly how the missing
          "platform" in the email dedup key could've been fixed in one copy
          and silently left broken in the other.
    """
    try:
        key = f"{j['platform']}::{j['company']}::{j['job_id']}::{j['url']}"
    except KeyError as e:
        print(f"[WARN] skipping malformed job result (missing key {e}): {j}", file=sys.stderr)
        return False

    if key in seen:
        return False
    seen.add(key)
    append_csv(j)

    # Includes platform - (company, job_id) alone can collapse two genuinely
    # different postings from different ATSes into one email entry (e.g. a
    # company still configured on two platforms during a migration).
    ek = (j["platform"], j["company"], j["job_id"])
    if ek not in email_keys:
        email_keys.add(ek)
        email_jobs.append(j)

    print(f"[NEW] {j['company']} | {j.get('title','')} | {j['url']}")
    return True


def _company_matches_filter(company_filter: Optional[str], cname: str, entry: Dict[str, Any]) -> bool:
    """True if this dict-based entry should be included given --company.
    Matches against the resolved display name, or any of the raw identifying
    fields a dict-based platform might use (host/tenant for Workday/iCIMS,
    account for Workable, board for Ashby)."""
    if not company_filter:
        return True
    candidates = {cname, entry.get("host"), entry.get("tenant"), entry.get("account"), entry.get("board")}
    return company_filter in candidates


def run(platform_filter=None, company_filter=None):
    cfg_path = "companies.yaml"
    if not os.path.exists(cfg_path):
        print("[ERROR] companies.yaml not found", file=sys.stderr)
        return 2

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"[ERROR] companies.yaml is not valid YAML -> {e}", file=sys.stderr)
        return 2

    seen = load_seen()
    total_new = 0
    error_count = 0

    email_jobs: List[Dict[str, Any]] = []
    email_keys = set()  # dedupe key: (platform, company, job_id)

    try:
        # simple list-based platforms
        for plat in ["greenhouse", "lever"]:
            if plat not in cfg: continue
            if platform_filter and platform_filter != plat: continue
            slugs = cfg.get(plat) or []
            for slug in slugs:
                if not isinstance(slug, str) or not slug.strip():
                    continue
                if company_filter and slug != company_filter:
                    continue
                try:
                    jobs = FETCHERS[plat](slug.strip())
                except Exception as e:
                    print(f"[WARN] {plat}:{slug} failed -> {e}", file=sys.stderr)
                    error_count += 1
                    continue
                new_count = 0
                for j in jobs:
                    if _process_job(j, seen, email_keys, email_jobs):
                        total_new += 1
                        new_count += 1
                print(f"[SUMMARY] {plat}:{slug} -> {new_count} new", file=sys.stderr)

        # dict-based platforms
        for plat, fetcher in DICT_FETCHERS.items():
            if plat not in cfg: continue
            if platform_filter and platform_filter != plat: continue
            entries = cfg.get(plat) or []
            if not isinstance(entries, list):
                print(f"[WARN] {plat}: expected a list of entries in companies.yaml, got {type(entries).__name__} - skipping", file=sys.stderr)
                continue
            for entry in entries:
                if not isinstance(entry, dict): continue
                cname = entry.get("company") or entry.get("tenant") or entry.get("host") or "unknown"
                if not _company_matches_filter(company_filter, cname, entry):
                    continue
                try:
                    jobs = fetcher(entry)
                except Exception as e:
                    print(f"[WARN] {plat}:{cname} failed -> {e}", file=sys.stderr)
                    error_count += 1
                    continue
                new_count = 0
                for j in jobs:
                    if _process_job(j, seen, email_keys, email_jobs):
                        total_new += 1
                        new_count += 1
                print(f"[SUMMARY] {plat}:{cname} -> {new_count} new", file=sys.stderr)
    finally:
        # Runs even on an unforeseen exception or a Ctrl-C/SIGTERM mid-scrape,
        # so a run that gets interrupted after 80 of 100 companies still
        # persists dedup progress for those 80 instead of losing everything.
        save_seen(seen)

    # write (or clear) the email digest
    if email_jobs:
        tag_label = os.getenv("TAG", "jobs")
        unique_count = len(email_jobs)
        with open(EMAIL_BODY_PATH, "w", encoding="utf-8") as f:
            f.write(f"<h2>{html.escape(str(unique_count))} new {html.escape(tag_label)} jobs found</h2>\n<ul>\n")
            for j in email_jobs:
                title   = html.escape(j.get("title","") or "")
                company = html.escape(j.get("company","") or "")
                loc     = html.escape(j.get("location","") or "")
                url     = html.escape(j.get("url","") or "", quote=True)
                salary  = html.escape(j.get("salary","") or "")
                bits = [f"<b>{company}</b> — {title}"]
                if loc:
                    bits.append(f" · {loc}")
                if salary:
                    bits.append(f" · {salary}")
                line = "".join(bits) + f' — <a href="{url}">View</a>'
                f.write(f"<li>{line}</li>\n")
            f.write("</ul>\n")
    elif os.path.exists(EMAIL_BODY_PATH):
        # No new jobs this run - don't leave a previous run's digest sitting
        # here looking like it's current.
        os.remove(EMAIL_BODY_PATH)

    print(f"Done. New matches: {total_new}. Fetch errors: {error_count}.")
    return 0

def parse_args():
    ap = argparse.ArgumentParser(description="Multi-ATS Job Watcher")
    ap.add_argument("--platform", help="Limit to one platform")
    ap.add_argument("--company", help="Limit to one company slug/host/name/account")
    return ap.parse_args()

if __name__ == "__main__":
    args = parse_args()
    sys.exit(run(args.platform, args.company))
