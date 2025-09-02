import os, re, csv, json
from datetime import datetime, timezone
from typing import Dict, Any

# --- Keywords from environment ---
KW = os.getenv("KEYWORDS", "assessment|director|program|project|product|testing|math|science")
raw_terms = [t.strip() for t in KW.split("|") if t.strip()]

def _term_to_regex(term: str) -> str:
    """
    Match a term as a whole word/phrase.
    Example: 'program manager' -> \bprogram\s+manager\b
    """
    if " " in term:
        parts = [re.escape(p) for p in term.split()]
        return r"\b" + r"\s+".join(parts) + r"\b"
    else:
        return r"\b" + re.escape(term) + r"\b"

pattern_src = r"(?:%s)" % "|".join(_term_to_regex(t) for t in raw_terms)
KEYWORD_REGEX = re.compile(pattern_src, re.IGNORECASE)

def job_matches_music(text: str) -> bool:
    """Return True if any keyword matches the text."""
    if not text:
        return False
    return bool(KEYWORD_REGEX.search(text))

# --- File paths per keyword set ---
TAG = os.getenv("TAG", "edtech")

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:50] or "jobs"

CSV_PATH = f"{_slug(TAG)}_jobs.csv"
SEEN_PATH = f"seen_{_slug(TAG)}.json"

# --- Helpers ---
def mk_row(company: str, platform: str, title: str, location: str, job_id: str, url: str,
           posted_iso: str, match_basis: str) -> Dict[str, Any]:
    return {
        "company": company,
        "platform": platform,
        "title": title,
        "location": location,
        "job_id": job_id,
        "url": url,
        "posted_iso": posted_iso,
        "matched_on": match_basis,
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
    header = ["company","platform","title","location","job_id","url","posted_iso","matched_on","seen_at_utc"]
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
                row.get("posted_iso",""), row.get("matched_on",""), seen_at
            ])
    except Exception:
        pass
