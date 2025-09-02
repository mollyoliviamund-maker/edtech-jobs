# Multi-ATS Music Job Watcher

Watches a list of companies across **Greenhouse, Lever, Workday CxS, Workable, iCIMS, Teamtailor, ADP Workforce Now, SAP SuccessFactors, Jobvite, and Pereless/Submit4Jobs** for postings whose title or description contains **"Music"** (case-insensitive).

- Results → `music_jobs.csv`
- Dedupe → `seen_music.json`
- Configure targets → `companies.yaml`
- CI schedule → `.github/workflows/scrape.yml`

## Local Run

```bash
python -m venv .venv
# Windows: . .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scraper.py
# or target a platform/company:
python scraper.py --platform greenhouse --company duolingo
