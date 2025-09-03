import os, re, csv, json
from datetime import datetime, timezone
from typing import Dict, Any

# =========================
# Keyword matching controls
# =========================
KW = os.getenv("KEYWORDS", "music")
raw_terms = [t.strip() for t in KW.split("|") if t.strip()]

def _term_to_regex(term: str) -> str:
    if " " in term:
        parts = [re.escape(p) for p in term.split()]
        return r"\b" + r"\s+".join(parts) + r"\b"
    else:
        return r"\b" + re.escape(term) + r"\b"

pattern_src = r"(?:%s)" % "|".join(_term_to_regex(t) for t in raw_terms) if raw_terms else r"^$"
KEYWORD_REGEX = re.compile(pattern_src, re.IGNORECASE)

# 'title' (default) OR 'title_or_description'
MATCH_SCOPE = os.getenv("MATCH_SCOPE", "title").lower()

def matches_kw(title: str = "", location: str = "", desc: str = "") -> bool:
    if MATCH_SCOPE == "title":
        text = title or ""
    else:
        text = "\n".join([title or "", location or "", desc or ""])
    return bool(text and KEYWORD_REGEX.search(text))

# Back-compat if any older code calls this directly:
def job_matches_music(text: str) -> bool:
    return bool(text and KEYWORD_REGEX.search(text))

# =========================
# Filenames per run TAG
# =========================
TAG = os.getenv("TAG", KW)

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:50] or "jobs"

CSV_PATH = f"{_slug(TAG)}_jobs.csv"
SEEN_PATH = f"seen_{_slug(TAG)}.json"

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
