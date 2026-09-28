# Job hunter

Daily job search for Kamal Saloi (Product Designer). Pulls fresh postings, scores each one against the resume with Gemini, drafts application answers for strong matches, and publishes a password-protected report.

## What it does

- **Sources:** LinkedIn (public job search), Wellfound, Himalayas, Remotive, RemoteOK, Jobicy, We Work Remotely.
- **Locations:** any Indian city (on-site, hybrid, remote) plus remote roles in the US, UK, Europe, UAE, Singapore, Australia and Canada.
- **Scoring:** Gemini rates fit 0–100 (skills, seniority, odds of being hired from India, competition), explains why and flags red flags.
- **Application kits** for jobs scoring 70+: which portfolio project to lead with, cover note, "why this company", answers to the form's real questions when the role is on the company's Greenhouse/Ashby/Lever page (otherwise common questions), a recruiter note and resume tweaks. Nothing is submitted automatically.
- **Report:** `latest.html` locally; an encrypted copy at `docs/index.html` is pushed after every run.

## Files

| File | Purpose |
|---|---|
| `job_hunter.py` | The whole pipeline (standard library only) |
| `config.json` | Searches, filters, models, thresholds |
| `answers.json` | Facts the AI may use in application answers |
| `report_template.html` | Report UI (Basecoat / shadcn styles, vanilla JS) |
| `encrypt_page.mjs` | Encrypts the report (AES-256-GCM, PBKDF2) for hosting |
| `run.bat` | Entry point for Windows Task Scheduler (daily 9:00) |
| `.env` | **Not in git.** `GEMINI_API_KEY`, `GEMINI_API_KEY_2`, `PAGE_PASSWORD` |

## Commands

```
python job_hunter.py                  # full run (search, score, kits, report, publish)
python job_hunter.py --days 7         # look back 7 days on LinkedIn
python job_hunter.py --kits-only      # draft kits for recent strong matches, rebuild report
python job_hunter.py --report-only    # rebuild the report from saved results
python job_hunter.py --no-open        # don't open the browser at the end
python job_hunter.py --publish        # also encrypt + push (with --report-only / --kits-only)
```
