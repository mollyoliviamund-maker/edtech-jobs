"""
utils.py — keyword matching, CSV/seen-state persistence.

MATCHING MODEL (changed from the original single-keyword "music" setup):

There are now TWO independent filters, and a job must pass BOTH to be recorded:

  1. SENIORITY  (env: KEYWORDS)         -> matched against the job TITLE
  2. DOMAIN     (env: DOMAIN_KEYWORDS)  -> matched against TITLE **or** DESCRIPTION

Why two filters instead of one big OR list: matching seniority alone
("director|manager|vp") across ~100 edtech companies returns every Director of
Finance and Sales Manager in the sector - hundreds of irrelevant hits per run.
But requiring BOTH words in the title alone ("Director ... Assessment") is too
strict, because a genuinely relevant role is often titled just "VP, Product" at
a company whose whole product IS assessment - the domain signal lives in the
description, not the title. Splitting the two scopes handles both cases.

To disable the domain filter and match on seniority alone:
    DOMAIN_KEYWORDS="" python scraper.py

To go back to a single-keyword setup (original behavior):
    KEYWORDS="music" DOMAIN_KEYWORDS="" TAG=music python scraper.py

Terms are pipe-separated. Multi-word terms allow flexible whitespace
("learning science" matches "learning  science"). Matching is case-insensitive
and word-boundaried, with an optional trailing "s" so "assessment" also matches
"assessments" (see _term_to_regex).
"""

import os, re, csv, json
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# =========================
# Keyword matching controls
# =========================

# ---- 1. SENIORITY terms (matched against TITLE by default) ----
# Note on redundancy: word-boundary matching means "director" already covers
# "Senior Director", "Executive Director", and "Managing Director" - listing
# those separately would be a no-op, so they're omitted.
#
# Note on deliberate EXCLUSIONS:
#   - bare "executive" is NOT here: it would match "Account Executive", which is
#     an individual-contributor sales role, not leadership. "Executive Director"
#     is already covered by "director".
#   - "senior" alone is NOT here: it would match every "Senior Engineer".
#
# Broad terms you may want to trim: "manager" catches Project/Account/Customer
# Success Manager; "lead" catches "Lead Generation". Both are included because
# you asked for manager-level, but they're the first things to cut if the
# volume is too high.
DEFAULT_SENIORITY = "|".join([
    "director",          # + senior/executive/managing director
    "manager",           # + senior manager
    "vice president",
    "vp", "svp", "evp",
    "head of",
    "chief",
    "principal",
    "lead",
])
KW = os.getenv("KEYWORDS", DEFAULT_SENIORITY)

# ---- 2. DOMAIN terms (matched against TITLE or DESCRIPTION by default) ----
# Includes British spellings deliberately: several configured companies are
# non-US (Education Perfect/AU, Kahoot!/NO, Zen Educate/UK, Renaissance EMEA),
# and "personalisation" would otherwise silently miss every one of their roles.
DEFAULT_DOMAIN = "|".join([
    # ORDER MATTERS for one caller: adapters.fetch_dejobs() issues server-side
    # searches using only the FIRST 6 terms of this list (DirectEmployers sites
    # like pearson.jobs have thousands of postings, so it needs a narrow query
    # to get a usable candidate set). The first six are therefore deliberately
    # interleaved across both halves of the domain - an earlier version led with
    # six personalization terms in a row, which meant Pearson (one of the
    # largest assessment employers in the config) was never searched for
    # assessment roles at all. Every other platform filters locally and is
    # unaffected by ordering.
    "personalization",
    "assessment",
    "adaptive learning",
    "psychometrics",
    "personalized learning",
    "measurement",
    # --- remainder: personalization / adaptive ---
    # Noun forms and explicit two-word phrases only. Bare "personalized" and
    # bare "adaptive" were deliberately REMOVED: "we deliver personalized
    # instruction" and "our adaptive curriculum" are marketing boilerplate in
    # essentially every edtech job description, so as description-scoped terms
    # they matched almost everything. The noun/phrase forms indicate the role
    # is actually ABOUT personalization, not just at a company that mentions it.
    "personalisation", "personalised learning",
    "adaptive engine", "adaptive instruction",
    "differentiated instruction",
    "recommendation engine", "learner model", "knowledge tracing",
    # --- remainder: assessment / measurement ---
    "psychometric", "efficacy",
    "item development", "item writing", "item bank", "test development",
    "formative assessment", "summative assessment", "benchmark assessment",
    "learning science", "learning sciences",
    "student growth", "score report", "standardized test", "test design",
])
DOMAIN_KW = os.getenv("DOMAIN_KEYWORDS", DEFAULT_DOMAIN)

# ---- 3. EXCLUDED title terms (hard veto, checked against TITLE only) ----
# A title containing any of these is rejected outright, no matter what the
# seniority/domain filters say.
#
# Why this exists: "Lead Math Tutor" contains the seniority word "lead" but is
# an individual-contributor teaching role, not leadership. Tutoring-heavy
# boards (Think Academy, Nerdy/Varsity Tutors, Princeton Review, StudyPoint,
# Tutored by Teachers) can produce dozens of these in a single run and drown
# out the handful of real leadership roles.
#
# Note on word boundaries + the optional-plural rule: "tutor" does NOT match
# "Tutoring", and "coach" does NOT match "Coaching" - so genuinely senior
# titles like "Director of Tutoring" or "Head of Coaching" still get through.
#
# Tradeoff to be aware of: this also vetoes titles like "Manager, Teacher
# Success" or "Director of Instructor Operations", which may be roles you
# actually want. If you're missing things, trim this list first.
DEFAULT_EXCLUDE_TITLE = "|".join([
    "tutor", "teacher", "instructor", "coach",
    "trainee", "intern", "internship",
    "faculty", "aide", "paraprofessional", "substitute",
    "proctor", "grader", "scorer", "rater",
    "babysitter", "nanny",
])
EXCLUDE_TITLE_KW = os.getenv("EXCLUDE_TITLE_KEYWORDS", DEFAULT_EXCLUDE_TITLE)


def _term_to_regex(term: str) -> str:
    """Word-boundaried regex for one term, with an optional trailing 's'.

    The trailing 's?' means "assessment" also matches "assessments" and
    "psychometric" matches "psychometrics" - forgetting a plural is an easy way
    to silently drop real matches. Harmless on terms where it doesn't apply
    ("head of" -> "head ofs?" still matches "head of", since s is optional).
    """
    if " " in term:
        parts = [re.escape(p) for p in term.split()]
        return r"\b" + r"\s+".join(parts) + r"s?\b"
    return r"\b" + re.escape(term) + r"s?\b"


def _compile_terms(terms_str: str) -> Optional[re.Pattern]:
    """Compile a pipe-separated term list into one alternation regex.
    Returns None if the list is empty (i.e. that filter is disabled)."""
    terms = [t.strip() for t in (terms_str or "").split("|") if t.strip()]
    if not terms:
        return None
    return re.compile(r"(?:%s)" % "|".join(_term_to_regex(t) for t in terms), re.IGNORECASE)


SENIORITY_REGEX = _compile_terms(KW)
DOMAIN_REGEX = _compile_terms(DOMAIN_KW)
EXCLUDE_TITLE_REGEX = _compile_terms(EXCLUDE_TITLE_KW)

# Back-compat: some callers/log lines still reference KEYWORD_REGEX.
KEYWORD_REGEX = SENIORITY_REGEX

# Scope for each filter: 'title' or 'title_or_description'.
# Seniority defaults to title-only (a description mentioning "our director"
# says nothing about the seniority of THIS role).
# Domain defaults to title_or_description (see module docstring).
MATCH_SCOPE = os.getenv("MATCH_SCOPE", "title").lower()
DOMAIN_SCOPE = os.getenv("DOMAIN_SCOPE", "title_or_description").lower()

if MATCH_SCOPE != "title":
    # This is a genuine footgun, not a style preference, so it's worth saying
    # out loud on every run: virtually every job description contains the verb
    # "lead" ("lead sessions", "lead projects", "lead a team"), so scoping the
    # SENIORITY filter to descriptions makes it match nearly everything and
    # silently collapses the two-filter design into a domain-only match.
    # Observed in practice: it turned a run into 47 hits dominated by
    # part-time math tutors.
    import sys as _sys
    print(
        f"[WARN] MATCH_SCOPE={MATCH_SCOPE!r}: the seniority filter is being matched against "
        "job DESCRIPTIONS, not just titles. Words like 'lead' appear in almost every "
        "description, so this will match large numbers of individual-contributor roles. "
        "Set MATCH_SCOPE=title (the default) unless you specifically want this.",
        file=_sys.stderr,
    )


def matches_kw(title: str = "", location: str = "", desc: str = "") -> bool:
    """True only if the job passes the exclusion veto AND both the seniority
    and domain filters.

    A filter with no terms configured is skipped entirely, so setting
    DOMAIN_KEYWORDS="" reduces this to a plain single-keyword match (the
    original behavior), and EXCLUDE_TITLE_KEYWORDS="" disables the veto.
    """
    if SENIORITY_REGEX is None and DOMAIN_REGEX is None:
        return False  # nothing configured - match nothing rather than everything

    title_text = title or ""
    full_text = "\n".join([title or "", location or "", desc or ""])

    # 0. Hard veto on excluded title terms. Checked FIRST and against the title
    #    only - an IC teaching role stays an IC teaching role no matter what
    #    seniority word also happens to appear in its title.
    if EXCLUDE_TITLE_REGEX is not None and title_text and EXCLUDE_TITLE_REGEX.search(title_text):
        return False

    if SENIORITY_REGEX is not None:
        target = title_text if MATCH_SCOPE == "title" else full_text
        if not (target and SENIORITY_REGEX.search(target)):
            return False

    if DOMAIN_REGEX is not None:
        target = title_text if DOMAIN_SCOPE == "title" else full_text
        if not (target and DOMAIN_REGEX.search(target)):
            return False

    return True


def match_detail(title: str = "", location: str = "", desc: str = "") -> str:
    """Human-readable reason a job matched, for the CSV 'matched_on' column.
    Distinguishes a title-only domain hit from a description-only one, which is
    the difference between 'obviously relevant' and 'worth a skim'."""
    title_text = title or ""
    full_text = "\n".join([title or "", location or "", desc or ""])
    if EXCLUDE_TITLE_REGEX is not None and title_text:
        m = EXCLUDE_TITLE_REGEX.search(title_text)
        if m:
            return f"EXCLUDED:{m.group(0).lower()}"
    bits = []
    if SENIORITY_REGEX is not None:
        m = SENIORITY_REGEX.search(title_text) or (
            SENIORITY_REGEX.search(full_text) if MATCH_SCOPE != "title" else None
        )
        if m:
            bits.append(f"seniority:{m.group(0).lower()}")
    if DOMAIN_REGEX is not None:
        m_title = DOMAIN_REGEX.search(title_text)
        if m_title:
            bits.append(f"domain-title:{m_title.group(0).lower()}")
        else:
            m_desc = DOMAIN_REGEX.search(full_text)
            if m_desc:
                bits.append(f"domain-desc:{m_desc.group(0).lower()}")
    return "+".join(bits) if bits else "match"


# Back-compat shim if any older code still calls this directly:
def job_matches_music(text: str) -> bool:
    return bool(text and SENIORITY_REGEX and SENIORITY_REGEX.search(text))


# =========================
# Filenames per run TAG
# =========================
# TAG no longer defaults to KEYWORDS: the seniority list is now a long
# pipe-separated string, which would produce an unreadable 50-char-truncated
# filename like "director_manager_vice_president_vp_svp_evp_head_of_jobs.csv".
TAG = os.getenv("TAG", "edtech")

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:50] or "jobs"

CSV_PATH = f"{_slug(TAG)}_jobs.csv"      # -> edtech_jobs.csv
SEEN_PATH = f"seen_{_slug(TAG)}.json"    # -> seen_edtech.json

# =========================
# CSV / Seen helpers
# =========================
def mk_row(company: str, platform: str, title: str, location: str, job_id: str, url: str,
           posted_iso: str, match_basis: str, salary: str = "") -> Dict[str, Any]:
    return {
        "company": company,
        "platform": platform,
        "title": title,
        "location": location,
        "job_id": job_id,
        "url": url,
        "posted_iso": posted_iso,
        "matched_on": match_basis,
        "salary": salary,
    }

def load_seen():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen)), f)
    except Exception:
        pass

def append_csv(row: Dict[str, Any]):
    header = [
        "company","platform","title","location","job_id","url",
        "posted_iso","matched_on","salary","seen_at_utc"
    ]
    seen_at = datetime.now(timezone.utc).isoformat()
    try:
        need_header = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if need_header:
                w.writerow(header)
            w.writerow([
                row.get("company",""), row.get("platform",""), row.get("title",""),
                row.get("location",""), row.get("job_id",""), row.get("url",""),
                row.get("posted_iso",""), row.get("matched_on",""),
                row.get("salary",""), seen_at
            ])
    except Exception:
        pass
