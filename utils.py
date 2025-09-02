import csv, json, os, re
from datetime import datetime, timezone
from typing import Dict, Any
from urllib.parse import urlparse, urlunparse

MUSIC_PATTERN = re.compile(r"\bmusic\b", re.IGNORECASE)

CSV_PATH = "music_jobs.csv"
SEEN_PATH = "seen_music.json"

CSV_HEADERS = [
    "company", "platform", "title", "location", "job_id",
    "url", "posted_at_iso", "detected_on_iso", "matched_on"
]

def _normalize_url(u: str) -> str:
    if not u:
        return ""
    try:
        p = urlparse(u)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return u

def load_seen() -> set:
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
            except Exception:
                pass
    return set()

def save_seen(seen: set) -> None:
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, indent=2)

def ensure_csv() -> None:
    needs_header = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    if needs_header:
        with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)

def append_csv(row: Dict[str, Any]) -> None:
    ensure_csv()
    row = dict(row)
    row["url"] = _normalize_url(row.get("url", ""))
    with open(CSV_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([row.get(h, "") for h in CSV_HEADERS])

def job_matches_music(text: str) -> bool:
    return bool(MUSIC_PATTERN.search(text or ""))

def normalized_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def mk_row(company:str, platform:str, title:str, location:str, job_id:str, url:str, posted_at:str, matched_on:str) -> Dict[str, Any]:
    return {
        "company": company,
        "platform": platform,
        "title": title or "",
        "location": location or "",
        "job_id": str(job_id or ""),
        "url": _normalize_url(url or ""),
        "posted_at_iso": posted_at or "",
        "detected_on_iso": normalized_now(),
        "matched_on": matched_on or "title_or_description",
    }
