"""Daily job hunter: pulls fresh postings from LinkedIn, Wellfound and remote boards,
scores them against the resume with Gemini, and writes a ranked HTML report + CSV tracker.

Usage:  python job_hunter.py [--no-ai] [--no-open]
Standard library only.
"""
import csv
import html
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
REPORTS = os.path.join(ROOT, "reports")
LOGS = os.path.join(ROOT, "logs")
for d in (DATA, REPORTS, LOGS):
    os.makedirs(d, exist_ok=True)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
NOW = datetime.now(timezone.utc)
LOG_FILE = open(os.path.join(LOGS, "run.log"), "a", encoding="utf-8")


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line.encode("ascii", "replace").decode())
    LOG_FILE.write(line + "\n")
    LOG_FILE.flush()


def load_env():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def fetch(url, data=None, headers=None, timeout=30, retries=2):
    h = dict(UA, **(headers or {}))
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(8 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(4)
                continue
            raise


def pause(lo=1.2, hi=2.8):
    time.sleep(random.uniform(lo, hi))


def clean(text):
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def iso_date(value):
    """Posting time as UTC ISO with minutes, or a bare YYYY-MM-DD when that's all the source gives."""
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            dt = datetime.fromtimestamp(int(value), timezone.utc)
        elif isinstance(value, str) and "T" in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        else:
            return (value or "")[:10]
        return dt.astimezone(timezone.utc).isoformat(timespec="minutes")
    except Exception:
        return str(value or "")[:10]


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def read_resume(path):
    if path.lower().endswith(".docx"):
        xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
        return clean(xml.replace("</w:p>", "\n"))
    return open(path, encoding="utf-8", errors="replace").read()


# ---------------------------------------------------------------- sources

def src_linkedin(cfg, window_secs):
    jobs = []
    base = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
    for s in cfg["linkedin_searches"]:
        for kw in s.get("keywords") or cfg["linkedin_keywords"]:
            start = 0
            for _ in range(s.get("pages", 2)):
                params = {"keywords": kw, "location": s["location"], "f_TPR": f"r{window_secs}",
                          "sortBy": "DD", "start": start}
                if s.get("remote"):
                    params["f_WT"] = "2"
                try:
                    page = fetch(base + urllib.parse.urlencode(params))
                except Exception as e:
                    log(f"  linkedin '{kw}' @ {s['label']}: {e}")
                    break
                cards = page.split('<li>')[1:] if '<li>' in page else re.split(r'(?=<div[^>]+base-card)', page)[1:]
                found = 0
                for c in cards:
                    jid = re.search(r"urn:li:jobPosting:(\d+)", c)
                    title = re.search(r'base-search-card__title">\s*([^<]+?)\s*<', c)
                    if not jid or not title:
                        continue
                    found += 1
                    comp = re.search(r'base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*([^<]+?)\s*<', c)
                    loc = re.search(r'job-search-card__location">\s*([^<]+?)\s*<', c)
                    when = re.search(r'<time[^>]*datetime="([^"]+)"[^>]*>\s*([^<]*?)\s*<', c)
                    posted = when.group(1) if when else ""
                    rel = re.match(r"(\d+)\s+(minute|hour)s?\s+ago", when.group(2)) if when else None
                    if rel:
                        posted = (NOW - timedelta(**{rel.group(2) + "s": int(rel.group(1))})).isoformat(timespec="minutes")
                    jobs.append({
                        "id": f"li:{jid.group(1)}", "source": "LinkedIn",
                        "title": html.unescape(title.group(1)),
                        "company": html.unescape(comp.group(1)) if comp else "",
                        "location": html.unescape(loc.group(1)) + (" (Remote)" if s.get("remote") else ""),
                        "url": f"https://www.linkedin.com/jobs/view/{jid.group(1)}/",
                        "posted": posted, "description": "", "extra": "",
                    })
                if found < 10:
                    break
                start += found
                pause()
    return jobs


def linkedin_details(job):
    page = fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['id'][3:]}")
    m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
    job["description"] = clean(m.group(1)) if m else ""
    crit = re.findall(r'description__job-criteria-subheader">\s*([^<]+?)\s*<.*?'
                      r'description__job-criteria-text[^>]*>\s*([^<]+?)\s*<', page, re.S)
    job["extra"] = "; ".join(f"{k}: {v}" for k, v in crit if k.startswith(("Seniority", "Employment")))
    app = re.search(r'num-applicants__caption[^>]*>\s*([^<]+?)\s*<', page)
    job["applicants"] = html.unescape(app.group(1)) if app else ""


def src_wellfound(cfg):
    jobs = []
    paths = []
    for role in cfg["wellfound_roles"]:
        paths += [f"/role/l/{role}/{loc}" for loc in cfg["wellfound_location_slugs"]]
        if cfg.get("wellfound_include_remote"):
            paths.append(f"/role/r/{role}")
    for p in paths:
        try:
            page = fetch("https://wellfound.com" + p)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
            if not m:
                log(f"  wellfound {p}: blocked or no data")
                continue
            state = json.loads(m.group(1))["props"]["pageProps"].get("apolloState", {}).get("data", {})
        except Exception as e:
            log(f"  wellfound {p}: {e}")
            continue
        company_of = {}
        for v in state.values():
            if v.get("__typename") == "StartupResult":
                for ref in v.get("highlightedJobListings", []):
                    company_of[ref["__ref"]] = v
        for key, v in state.items():
            if v.get("__typename") != "JobListingSearchResult":
                continue
            co = company_of.get(key, {})
            locs = v.get("locationNames") or []
            remote_locs = v.get("acceptedRemoteLocationNames") or []
            loc = ", ".join(locs) + (f" (accepts: {', '.join(remote_locs)})" if remote_locs else "")
            yrs = v.get("yearsExperienceMin")
            extra = "; ".join(x for x in [
                v.get("compensation") or "",
                f"min {yrs} yrs exp" if yrs is not None else "",
                co.get("highConcept") or "",
                (co.get("companySize") or "").replace("SIZE_", "").replace("_", "-") + " people" if co.get("companySize") else "",
            ] if x)
            jobs.append({
                "id": f"wf:{v['id']}", "source": "Wellfound", "title": v.get("title", ""),
                "company": (co.get("name") or "").strip(), "location": loc,
                "url": f"https://wellfound.com/jobs/{v['id']}-{v.get('slug', '')}",
                "posted": iso_date(v.get("liveStartAt")), "description": clean(v.get("description", "")),
                "extra": extra, "remote_locs": remote_locs, "remote": v.get("remote"),
            })
        pause()
    return jobs


def src_remotive(cfg):
    data = json.loads(fetch("https://remotive.com/api/remote-jobs?category=design&limit=150"))
    return [{
        "id": f"rv:{j['id']}", "source": "Remotive", "title": j["title"], "company": j["company_name"],
        "location": "Remote - " + (j.get("candidate_required_location") or "?"), "url": j["url"],
        "posted": iso_date(j.get("publication_date", "")), "description": clean(j.get("description", "")),
        "extra": j.get("salary") or "", "remote_locs": [j.get("candidate_required_location") or ""],
    } for j in data.get("jobs", [])]


def src_remoteok(cfg):
    data = json.loads(fetch("https://remoteok.com/api?tag=design"))
    out = []
    for j in data:
        if not j.get("id"):
            continue
        sal = f"${j['salary_min']:,}-${j['salary_max']:,}" if j.get("salary_min") else ""
        out.append({
            "id": f"ro:{j['id']}", "source": "RemoteOK", "title": j.get("position", ""),
            "company": j.get("company", ""), "location": "Remote - " + (j.get("location") or "Worldwide"),
            "url": j.get("url", ""), "posted": iso_date(j.get("date", "")),
            "description": clean(j.get("description", "")), "extra": sal,
            "remote_locs": [j.get("location") or "Worldwide"],
        })
    return out


def src_himalayas(cfg):
    out = []
    for q in cfg["remote_board_queries"]:
        try:
            data = json.loads(fetch("https://himalayas.app/jobs/api/search?" +
                                    urllib.parse.urlencode({"q": q, "limit": 50})))
        except Exception as e:
            log(f"  himalayas '{q}': {e}")
            continue
        for j in data.get("jobs", []):
            restr = j.get("locationRestrictions") or []
            sal = ""
            if j.get("minSalary"):
                sal = f"{j.get('currency') or ''} {j['minSalary']:,}-{j.get('maxSalary') or 0:,} {j.get('salaryPeriod') or ''}"
            out.append({
                "id": "hm:" + norm(j.get("companySlug", "") + " " + j.get("title", "")).replace(" ", "-"),
                "source": "Himalayas", "title": j.get("title", ""), "company": j.get("companyName", ""),
                "location": "Remote - " + (", ".join(restr) if restr else "Worldwide"),
                "url": j.get("applicationLink") or j.get("guid", ""), "posted": iso_date(j.get("pubDate", "")),
                "description": clean(j.get("description", "")),
                "extra": "; ".join(x for x in [sal.strip(), ", ".join(j.get("seniority") or [])] if x),
                "remote_locs": restr or ["Worldwide"],
            })
        pause(0.5, 1.2)
    return out


def src_jobicy(cfg):
    out = []
    for tag in ["designer", "ux%20designer"]:
        data = json.loads(fetch(f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={tag}"))
        for j in data.get("jobs", []):
            geo = [g.strip() for g in (j.get("jobGeo") or "Anywhere").split(",") if g.strip()]
            sal = ""
            if j.get("salaryMin"):
                sal = f"{j.get('salaryCurrency') or ''} {j['salaryMin']:,}-{j.get('salaryMax') or 0:,} {j.get('salaryPeriod') or ''}"
            out.append({
                "id": f"jc:{j['id']}", "source": "Jobicy", "title": html.unescape(j.get("jobTitle", "")),
                "company": html.unescape(j.get("companyName", "")), "location": "Remote - " + ", ".join(geo),
                "url": j.get("url", ""), "posted": iso_date(j.get("pubDate", "")),
                "description": clean(j.get("jobDescription", "")),
                "extra": "; ".join(x for x in [sal.strip(), j.get("jobLevel") or ""] if x), "remote_locs": geo,
            })
        pause(0.5, 1.2)
    return out


def src_wwr(cfg):
    feed = fetch("https://weworkremotely.com/categories/remote-design-jobs.rss")
    out = []
    for item in re.findall(r"<item>(.*?)</item>", feed, re.S):
        def tag(t):
            m = re.search(rf"<{t}>(.*?)</{t}>", item, re.S)
            return html.unescape(m.group(1)).strip() if m else ""
        company, _, title = tag("title").partition(": ")
        region = tag("region") or "Anywhere in the World"
        try:
            posted = parsedate_to_datetime(tag("pubDate")).astimezone(timezone.utc).isoformat(timespec="minutes")
        except Exception:
            posted = ""
        out.append({
            "id": "ww:" + tag("link").rstrip("/").rsplit("/", 1)[-1], "source": "WeWorkRemotely",
            "title": title or company, "company": company if title else "", "location": "Remote - " + region,
            "url": tag("link"), "posted": posted, "description": clean(tag("description")), "extra": "",
            "remote_locs": [region],
        })
    return out


INDIA_PLACES = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|thane|delhi|ncr|gurugram|gurgaon|noida|ghaziabad|faridabad|pune|"
    r"hyderabad|secunderabad|chennai|kolkata|ahmedabad|gandhinagar|jaipur|kochi|cochin|chandigarh|mohali|indore|"
    r"bhopal|coimbatore|thiruvananthapuram|trivandrum|bhubaneswar|lucknow|nagpur|mysuru|mysore|vadodara|surat|"
    r"visakhapatnam|vizag|goa|mangaluru|mangalore|karnataka|maharashtra|haryana|telangana|tamil nadu|"
    r"uttar pradesh|kerala|gujarat|west bengal|rajasthan|odisha|punjab|madhya pradesh|andhra pradesh)\b", re.I)


def infer_region(location, ai_region=None):
    return "India" if INDIA_PLACES.search(location or "") else (ai_region or "Global")


# ---------------------------------------------------------------- filtering

def prefilter(jobs, cfg, db):
    must = re.compile(cfg["title_must_match"], re.I)
    excl = re.compile(r"\b(" + "|".join(re.escape(w) for w in cfg["title_exclude"]) + r")", re.I)
    ok_locs = re.compile(r"\b(" + "|".join(re.escape(w) for w in cfg["remote_location_ok"]) + r")\b", re.I)
    cutoff = (NOW - timedelta(days=cfg["max_posting_age_days"])).date().isoformat()
    seen_ids = {r[0] for r in db.execute("SELECT id FROM jobs")}
    seen_keys = {r[0] for r in db.execute("SELECT dedupe_key FROM jobs")}
    kept, keys = [], set()
    for j in jobs:
        key = norm(j["company"]) + "|" + norm(j["title"])
        if j["id"] in seen_ids or key in seen_keys or key in keys:
            continue
        if not must.search(j["title"]) or excl.search(j["title"]):
            continue
        if j["posted"] and j["posted"] < cutoff:
            continue
        rl = j.get("remote_locs") or []
        if rl and not any(ok_locs.search(x) for x in rl):
            continue  # remote but restricted to regions outside the target list (e.g. LATAM-only)
        keys.add(key)
        j["dedupe_key"] = key
        kept.append(j)
    return kept


# ---------------------------------------------------------------- scoring

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "score": {"type": "INTEGER"},
            "verdict": {"type": "STRING", "enum": ["APPLY", "MAYBE", "SKIP"]},
            "why": {"type": "STRING"},
            "red_flags": {"type": "STRING"},
            "years_required": {"type": "INTEGER"},
            "location_ok": {"type": "BOOLEAN"},
            "region": {"type": "STRING", "enum": ["India", "US", "UK", "Europe", "Middle East", "APAC",
                                                  "Canada", "Global"]},
        },
        "required": ["id", "score", "verdict", "why", "red_flags", "years_required", "location_ok", "region"],
    },
}


YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|—|‑|‐|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs)\b", re.I)
REQ_WORDS = re.compile(r"experience|\bexp\b|designing|building|professional|hands-on|behind you|track record|"
                       r"\b(?:in|of) (?:product|ux|ui|design|software|front|web|saas|interaction|visual|motion)", re.I)
NARRATIVE = re.compile(r"\b(has been|have been|had been|founders?|founded|we've|we have|our (?:team|company|founders?)|"
                       r"spen[dt]|repeating|in business|since \d{4}|years? old|history|ago|anniversary)\b", re.I)


def required_years(text):
    """Minimum years of experience the posting asks of the candidate: the highest lower bound across requirement
    sentences ("4-6 years of experience" -> 4). Narrative sentences about founders or the company are ignored."""
    best, quote = None, ""
    for sent in re.split(r"(?<=[.!?])\s+|\n|•|;", text or ""):
        if NARRATIVE.search(sent) or not REQ_WORDS.search(sent):
            continue
        for m in YEARS_RE.finditer(sent):
            n = int(m.group(1))
            if n <= 20 and (best is None or n > best):
                best, quote = n, sent.strip()
    return best, quote[:180]


def apply_experience_rules(j, cfg):
    """Cap the score when the posting asks for more experience than the candidate has."""
    parsed, _ = required_years(j.get("description"))
    ai = j.get("years_required")
    req = parsed if parsed is not None else (ai if isinstance(ai, int) and ai >= 0 else None)
    j["years_required"] = req if req is not None else -1
    cap = None
    if req is not None:
        cap = next((c for min_years, c in cfg["experience_caps"] if req >= min_years), None)
    elif re.search(r"\b(senior|sr)\b", j["title"], re.I):
        cap = cfg["senior_title_cap"]
    if cap is not None and j["score"] > cap:
        j["score"] = cap
        flags = j.get("red_flags") or ""
        if not re.search(r"\d+\s*\+?\s*(?:years?|yrs)", flags, re.I):
            note = f"Asks for {req}+ years of experience" if req is not None else "Senior role"
            j["red_flags"] = f"{note} (you have under 1)" + (f"; {flags}" if flags else "")
    j["verdict"] = "APPLY" if j["score"] >= 70 else "MAYBE" if j["score"] >= 50 else "SKIP"


def job_text_for_ai(j, limit=8000):
    """Full description when it fits; otherwise the opening plus the requirement-heavy sentences."""
    d = j.get("description") or ""
    if len(d) <= limit:
        return d or "(not available)"
    head = d[:limit // 2]
    keep = [x for x in re.split(r"(?<=[.!?])\s+|\n", d[limit // 2:]) if REQ_WORDS.search(x) or YEARS_RE.search(x)
            or re.search(r"\b(must|require|fluent|degree|language|on-?site|office|citizen|authori[sz])", x, re.I)]
    return head + "\n[...]\n" + "\n".join(keep)[:limit // 2]


def score_batch(batch, resume, cfg, keys):
    listing = "\n\n".join(
        f"### JOB id={j['id']}\nTitle: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
        f"Source: {j['source']}\nApplicants so far: {j.get('applicants') or 'unknown'}\nExtra: {j['extra']}\n"
        + (lambda y: f"Experience asked (parsed from the text): {y[0]}+ years - \"{y[1]}\"\n" if y[0] is not None else "")(required_years(j.get("description")))
        + f"Description:\n{job_text_for_ai(j)}"
        for j in batch)
    prompt = f"""You are a sharp tech recruiter screening job postings for one candidate.

CANDIDATE RESUME:
{resume}

CANDIDATE CONTEXT & PREFERENCES:
{cfg['preferences']}

Candidate is based in Bengaluru, India (IST) with no foreign work authorization. Today is {NOW:%Y-%m-%d}.

LOCATION POLICY (decides location_ok):
- India: on-site, hybrid or remote in ANY Indian city is fine.
- Abroad: REMOTE ONLY. Remote roles at companies in the US, UK, Europe, UAE/Middle East, Singapore, Australia, Canada etc. are wanted.
  location_ok=false only if the posting explicitly requires local residency, citizenship, work authorization, security clearance, or on-site/hybrid presence abroad (unless it offers visa sponsorship + relocation).
  If it just says "Remote (US)" or similar without an explicit requirement, location_ok=true but add red flag "may require <country> work authorization".

Score every job below 0-100 for how worthwhile it is for this candidate to apply:
- Skills & domain match (45%): product/UX/UI craft, design systems, B2B/AI products, frontend (React/TS) as a plus.
- Seniority fit (25%): candidate has ~8 months full-time as a founding designer plus projects. Roles requiring 4+ years should score <= 45 unless the posting is clearly flexible; 2-3 years is a stretch but viable.
- Odds of being hired from India (15%): contractor/EOR-friendly, async, timezone overlap with IST, "hire anywhere" signals raise it.
- Pay: never penalize a role for paying well; a stated high salary is a plus inside "odds"/"skills" weighting. If stated pay is clearly below INR {cfg['min_monthly_pay_inr']:,} per month (or equivalent), add red flag "pay below your minimum".
- Competition (15%): fewer applicants is better. Use the applicant count when given (under 50 = great, over 200 = poor). Non-US foreign remote roles (Europe, UAE, Singapore, Australia, Canada) and small startups usually draw fewer applicants than big US remote roles.
verdict: APPLY (score >= 70), MAYBE (50-69), SKIP (< 50).
why: <= 25 words, specific to this job (what matches, what's the hook). red_flags: <= 15 words or "".
years_required: the minimum years of experience the posting asks OF THE CANDIDATE (not company or founder history), or -1 if not stated.
HARD REQUIREMENTS: read the whole description. If the candidate clearly fails a hard requirement stated anywhere (years of experience, a required language such as German/Dutch/Arabic fluency, a required degree he lacks, security clearance, local residency, on-site abroad), score <= 40 and name it in red_flags.
region: where the hiring company/team is (India, US, UK, Europe, Middle East, APAC, Canada, or Global if unclear).

{listing}"""
    rows, model = gemini_json(prompt, SCHEMA, cfg, keys)
    return {r["id"]: dict(r, model=model) for r in rows}


def gemini_keys():
    """GEMINI_API_KEY, GEMINI_API_KEY_2, ... from the environment / .env, in that order."""
    keys = [v.strip() for k, v in sorted(os.environ.items()) if k.startswith("GEMINI_API_KEY") and v.strip()]
    return list(dict.fromkeys(keys))


def quota_left(cfg, keys):
    spent = cfg.get("_spent", set())
    return any((m, n) not in spent for m in cfg["gemini_models"] for n in range(1, len(keys) + 1))


def gemini_json(prompt, schema, cfg, keys, temperature=0.2):
    """Structured-output call. Walks the models in order and, for each, every key whose daily quota isn't spent."""
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                             "temperature": temperature},
    }).encode()
    spent = cfg.setdefault("_spent", set())
    last_err = None
    for model in cfg["gemini_models"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for n, key in enumerate(keys, 1):
            if (model, n) in spent:
                continue
            try:
                resp = json.loads(fetch(url, data=body, timeout=180, retries=1,
                                        headers={"Content-Type": "application/json", "x-goog-api-key": key}))
                text = resp["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text), model
            except urllib.error.HTTPError as e:
                last_err = f"{model} (key {n}): HTTP {e.code}"
                if e.code == 429 and "PerDay" in e.read().decode("utf-8", "replace"):
                    spent.add((model, n))
                    log(f"  {last_err}, daily quota used; trying next key/model")
                    continue
                log(f"  {last_err}, trying next model")
                break  # overload / other errors hit the model for every key
            except Exception as e:
                last_err = f"{model} (key {n}): {e}"
                log(f"  {last_err}, trying next model")
                break
    raise RuntimeError(f"all Gemini models failed ({last_err})")


# ---------------------------------------------------------------- application kits

ATS_SKIP = re.compile(r"\b(first name|last name|full name|name|e-?mail|phone|resume|cv|pronouns?|gender|race|"
                      r"ethnic\w*|veteran|disabilit\w*|hispanic|latin[oa]|sexual|privacy|consent|acknowledge?)\b", re.I)


def _similar(a, b):
    ta, tb = set(norm(a).split()), set(norm(b).split())
    return len(ta & tb) / max(1, len(ta | tb))


def _best(items, title, key):
    best = max(items, key=lambda x: _similar(x[key], title), default=None)
    return best if best and _similar(best[key], title) >= 0.6 else None


def _ats_lookup(ats, slug, title):
    if ats == "greenhouse":
        jobs = json.loads(fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", retries=0, timeout=15))["jobs"]
        best = _best(jobs, title, "title")
        if not best:
            return None
        q = json.loads(fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{best['id']}?questions=true",
                             retries=0, timeout=15))
        return {"ats": "Greenhouse", "apply_url": best["absolute_url"],
                "questions": [x.get("label", "") for x in q.get("questions", [])]}
    if ats == "ashby":
        jobs = json.loads(fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", retries=0, timeout=15))["jobs"]
        best = _best(jobs, title, "title")
        if not best:
            return None
        body = json.dumps({"operationName": "ApiJobPosting",
                           "variables": {"organizationHostedJobsPageName": slug, "jobPostingId": best["id"]},
                           "query": "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) "
                                    "{ jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, "
                                    "jobPostingId: $jobPostingId) { applicationForm { sections { fieldEntries { field } } } } }"}).encode()
        questions = []
        try:
            r = json.loads(fetch("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", data=body,
                                 headers={"Content-Type": "application/json"}, retries=0, timeout=15))
            questions = [fe["field"].get("title", "") for sec in r["data"]["jobPosting"]["applicationForm"]["sections"]
                         for fe in sec["fieldEntries"]]
        except Exception:
            pass
        return {"ats": "Ashby", "apply_url": best["jobUrl"], "questions": questions}
    if ats == "lever":
        jobs = json.loads(fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json", retries=0, timeout=15))
        best = _best(jobs, title, "text")
        if not best:
            return None
        questions = []
        try:
            page = fetch(best["hostedUrl"] + "/apply", retries=0, timeout=15)
            questions = [clean(x) for x in re.findall(r'<div class="application-label[^"]*">(.*?)</div>', page, re.S)]
        except Exception:
            pass
        return {"ats": "Lever", "apply_url": best["hostedUrl"] + "/apply", "questions": questions}


def find_ats(job):
    """Look for the same role on the company's own Greenhouse / Ashby / Lever board (direct apply + real questions)."""
    base = norm(job["company"])
    if not base:
        return None
    for slug in dict.fromkeys([base.replace(" ", ""), base.replace(" ", "-")]):
        for ats in ("greenhouse", "ashby", "lever"):
            try:
                hit = _ats_lookup(ats, slug, job["title"])
            except Exception:
                hit = None
            if hit:
                qs = []
                for q in hit["questions"]:
                    q = re.sub(r"\s+", " ", q.replace("*", "").replace("\u2731", "")).strip()
                    if q and not ATS_SKIP.search(q) and q not in qs:
                        qs.append(q)
                hit["questions"] = qs[:12]
                return hit
    return None


KIT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "lead_project": {"type": "STRING"},
            "cover_note": {"type": "STRING"},
            "why_company": {"type": "STRING"},
            "answers": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "question": {"type": "STRING"}, "answer": {"type": "STRING"}}, "required": ["question", "answer"]}},
            "recruiter_message": {"type": "STRING"},
            "resume_tweaks": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["id", "lead_project", "cover_note", "why_company", "answers", "recruiter_message", "resume_tweaks"],
    },
}


def make_kits(batch, resume, answers, cfg, keys):
    blocks = []
    for j in batch:
        ats = j.get("ats") or {}
        qs = ats.get("questions") or cfg["common_questions"]
        form = f"{ats['ats']} form (these are its real questions)" if ats.get("questions") else "not visible (use these common questions)"
        blocks.append(
            f"### JOB id={j['id']}\nTitle: {j['title']}\nCompany: {j['company']}\nLocation: {j['location']}\n"
            f"Region: {j.get('region') or 'unknown'}\nApplication form: {form}\nQUESTIONS:\n- " + "\n- ".join(qs) +
            f"\nDescription:\n{(j.get('description') or '')[:3000] or '(not available)'}")
    facts = {k: v for k, v in answers.items() if not k.startswith("_")}
    prompt = f"""You help {facts.get('name', 'the candidate')} apply to jobs. They will review and edit everything before sending.

RESUME:
{resume}

ANSWER BANK (facts you may use; empty value = unknown):
{json.dumps(facts, ensure_ascii=False, indent=1)}

RULES
- Write in first person as the candidate. Plain, specific, confident. No cliches ("I am passionate", "I am excited to apply", "perfect fit"), no flattery.
- Use ONLY facts from the resume and answer bank. Never invent metrics, employers, tools, dates or results.
- If a question needs a fact that is missing or empty, put [FILL IN: what is needed] in the answer instead of guessing.
- Salary: follow salary_strategy exactly. Never state a low number or a minimum. Current CTC only if given.
- Work authorization / location questions: answer truthfully from the answer bank.
- Tie answers to 1-2 concrete details from THIS posting and the most relevant portfolio highlight.

FOR EACH JOB RETURN
- lead_project: which portfolio highlight to lead with, and one sentence on why it maps to this job.
- cover_note: <= 150 words, usable as a cover letter or "note to the hiring team".
- why_company: <= 80 words answer to "Why do you want to join <company>?".
- answers: answer every question listed for that job, in order, keeping the question text. Factual questions get one-line answers.
- recruiter_message: <= 280 characters LinkedIn connection note to the recruiter or hiring manager.
- resume_tweaks: up to 3 concrete edits to tailor the resume for this job.

{chr(10).join(blocks)}"""
    rows, _ = gemini_json(prompt, KIT_SCHEMA, cfg, keys, temperature=0.5)
    return {r["id"]: r for r in rows}


def build_kits(db, cfg, resume, keys):
    answers = json.load(open(os.path.join(ROOT, "answers.json"), encoding="utf-8"))
    cols = [r[1] for r in db.execute("PRAGMA table_info(jobs)")]
    todo = [dict(zip(cols, r)) for r in db.execute(
        "SELECT * FROM jobs WHERE kit IS NULL AND ai_scored = 1 AND score >= ? AND first_seen >= ? "
        "ORDER BY score DESC LIMIT ?",
        (cfg["kit_min_score"], (NOW - timedelta(days=7)).isoformat(), cfg["max_kits_per_run"]))]
    if not todo:
        return
    log(f"Preparing application kits for {len(todo)} jobs")
    for j in todo:
        j["ats"] = find_ats(j)
        if j["ats"]:
            log(f"  {j['company']}: direct on {j['ats']['ats']} ({len(j['ats']['questions'])} form questions)")
    size, done = 3, 0
    for i in range(0, len(todo), size):
        if not quota_left(cfg, keys):
            log("  Gemini daily quotas used; remaining kits will be prepared next run")
            break
        batch = todo[i:i + size]
        try:
            kits = make_kits(batch, resume, answers, cfg, keys)
        except Exception as e:
            log(f"  kit batch {i // size}: {e}; will retry next run")
            continue
        for j in batch:
            k = kits.get(j["id"])
            if k:
                ats = j["ats"] or {}
                k.update(ats=ats.get("ats"), apply_url=ats.get("apply_url"), form_questions=bool(ats.get("questions")))
                db.execute("UPDATE jobs SET kit = ? WHERE id = ?", (json.dumps(k, ensure_ascii=False), j["id"]))
                done += 1
        db.commit()
        time.sleep(7)
    log(f"  {done} application kits ready")


def score_jobs(cands, resume, cfg, keys, use_ai=True):
    results = {}
    if use_ai:
        size = cfg.get("jobs_per_ai_request", 10)
        for i in range(0, len(cands), size):
            if not quota_left(cfg, keys):
                log("  all Gemini daily quotas used; remaining jobs will be scored next run")
                break
            try:
                results.update(score_batch(cands[i:i + size], resume, cfg, keys))
            except Exception as e:
                log(f"  batch {i // size}: {e}; will retry these next run")
            time.sleep(7)  # stay under free-tier requests/minute
    for j in cands:
        r = results.get(j["id"])
        j["ai_scored"] = int(r is not None)
        r = r or keyword_score(j)
        j.update({k: r.get(k, "") for k in ["score", "verdict", "why", "red_flags", "years_required", "region"]})
        j["region"] = infer_region(j["location"], j["region"])
        if r.get("location_ok") is False:
            j["score"] = min(j["score"], 20)
        apply_experience_rules(j, cfg)
    return results


def rescore_recent(db, cfg, resume, keys):
    """Re-score everything in the report window with the current prompt and rules."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(jobs)")]
    rows = [dict(zip(cols, r)) for r in db.execute(
        "SELECT * FROM jobs WHERE first_seen >= ?", ((NOW - timedelta(days=cfg["report_window_days"])).isoformat(),))]
    log(f"Re-scoring {len(rows)} jobs from the past {cfg['report_window_days']} days")
    before = {j["id"]: j["score"] for j in rows}
    results = score_jobs(rows, resume, cfg, keys)
    done = [j for j in rows if j["id"] in results]
    db.executemany("UPDATE jobs SET score=?, verdict=?, why=?, red_flags=?, years_required=?, region=?, ai_scored=1 "
                   "WHERE id=?", [(j["score"], j["verdict"], j["why"], j["red_flags"], j["years_required"],
                                   j["region"], j["id"]) for j in done])
    db.commit()
    dropped = sum(1 for j in done if before[j["id"]] >= 70 > j["score"])
    log(f"  re-scored {len(done)}/{len(rows)}; {dropped} fell out of 'Apply now'")


def keyword_score(j):
    t = (j["title"] + " " + j["description"]).lower()
    s = 40
    s += 15 * any(w in t for w in ["product designer", "ux", "ui/ux", "design engineer"])
    s += 10 * any(w in t for w in ["design system", "figma"])
    s += 10 * any(w in t for w in ["react", "typescript", "frontend", "front-end"])
    s += 5 * any(w in t for w in ["ai", "b2b", "saas", "startup"])
    s -= 25 * bool(re.search(r"\b([4-9]|1\d)\+?\s*(years|yrs)", t))
    s -= 15 * ("senior" in j["title"].lower())
    return {"score": max(0, min(100, s)), "verdict": "MAYBE", "why": "Keyword match (AI scoring off)",
            "red_flags": "", "years_required": -1, "location_ok": True,
            "region": infer_region(j["location"])}


# ---------------------------------------------------------------- storage & report

def open_db():
    db = sqlite3.connect(os.path.join(DATA, "jobs.db"))
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, dedupe_key TEXT, source TEXT, title TEXT, company TEXT, location TEXT,
        url TEXT, posted TEXT, first_seen TEXT, score INTEGER, verdict TEXT, why TEXT, red_flags TEXT,
        years_required INTEGER, extra TEXT, description TEXT, ai_scored INTEGER, region TEXT, applicants TEXT)""")
    have = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    for col in ("region", "applicants", "kit"):
        if col not in have:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
    db.execute("CREATE TABLE IF NOT EXISTS runs (ts TEXT, stats TEXT)")
    if "stats" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}:
        db.execute("ALTER TABLE runs ADD COLUMN stats TEXT")
    return db


def last_run(db):
    row = db.execute("SELECT MAX(ts) FROM runs").fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def append_tracker(rows):
    path = os.path.join(ROOT, "tracker.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["found", "score", "verdict", "title", "company", "location", "source", "url",
                        "why", "red_flags", "status", "notes"])
        for j in rows:
            w.writerow([NOW.date().isoformat(), j["score"], j["verdict"], j["title"], j["company"], j["location"],
                        j["source"], j["url"], j["why"], j["red_flags"], "", ""])


BASECOAT_URL = "https://cdn.jsdelivr.net/npm/basecoat-css@1.0.2/dist/basecoat.cdn.min.css"
REPORT_FIELDS = ["id", "title", "company", "location", "url", "source", "posted", "first_seen", "score", "verdict", "why",
                 "red_flags", "years_required", "region", "applicants", "extra", "ai_scored", "kit"]


def basecoat_css():
    """shadcn/ui styles for plain HTML (Basecoat). Cached locally and inlined so reports open offline."""
    path = os.path.join(DATA, "basecoat-1.0.2.min.css")
    if not os.path.exists(path):
        try:
            open(path, "w", encoding="utf-8").write(fetch(BASECOAT_URL))
        except Exception as e:
            log(f"  basecoat download failed ({e}); report will load it from the CDN")
            return f'<link rel="stylesheet" href="{BASECOAT_URL}">'
    return "<style>" + open(path, encoding="utf-8").read() + "</style>"


def write_report(db, cfg, run_ts, stats):
    min_score = cfg["min_score_to_show"]
    cols = [r[1] for r in db.execute("PRAGMA table_info(jobs)")]
    week = (NOW - timedelta(days=7)).isoformat()
    jobs = []
    oldest = (NOW - timedelta(days=cfg["report_window_days"])).date().isoformat()
    for row in db.execute("SELECT * FROM jobs WHERE first_seen >= ? AND score >= ?", (week, min_score)):
        r = dict(zip(cols, row))
        if (r["posted"] or r["first_seen"]) < oldest:
            continue  # posted more than a week ago
        j = {k: r.get(k) for k in REPORT_FIELDS}
        j["region"] = infer_region(j["location"], j["region"])
        j["is_new"] = r["first_seen"] == run_ts
        j["kit"] = json.loads(j["kit"]) if j.get("kit") else None
        jobs.append(j)
    meta = dict(stats, candidate=cfg.get("candidate_name", ""),
                generated=f"{NOW.astimezone():%A, %d %B %Y · %H:%M}")
    dump = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    page = open(os.path.join(ROOT, "report_template.html"), encoding="utf-8").read()
    page = page.replace("__BASECOAT__", basecoat_css()).replace("__META__", dump(meta)).replace("__DATA__", dump(jobs))
    path = os.path.join(REPORTS, f"{NOW.astimezone():%Y-%m-%d_%H%M}.html")
    for out in (path, os.path.join(ROOT, "latest.html")):
        open(out, "w", encoding="utf-8").write(page)
    open(os.path.join(DATA, "publish.html"), "w", encoding="utf-8").write(
        page.replace(basecoat_css(), f'<link rel="stylesheet" href="{BASECOAT_URL}">'))
    return path, sum(j["is_new"] for j in jobs)


def cfg_repo():
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return re.sub(r"^.*github\.com[/:]|\.git$", "", url)


def publish():
    """Encrypt the report into docs/index.html and push it; the host (Vercel/Pages) redeploys on push."""
    if not os.environ.get("PAGE_PASSWORD") or not os.path.isdir(os.path.join(ROOT, ".git")):
        log("  publish skipped (PAGE_PASSWORD or git repo missing)")
        return
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")

    def run(*cmd):
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd[:2])}: {(r.stderr or r.stdout).strip()[:300]}")

    try:
        # Safety net: the repo holds personal files, so never push while it is publicly visible.
        try:
            fetch("https://api.github.com/repos/" + cfg_repo(), retries=0, timeout=20)
            log("  publish skipped: GitHub repo is PUBLIC. Make it private first.")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        run("node", "encrypt_page.mjs", os.path.join(DATA, "publish.html"), os.path.join(ROOT, "docs", "index.html"),
            os.path.join(DATA, "page_salt.txt"))
        run("git", "add", "docs/index.html")
        run("git", "commit", "-m", f"Report {NOW.astimezone():%Y-%m-%d %H:%M}")
        run("git", "push", "--quiet", "origin", "HEAD")
        log("  published encrypted report")
    except Exception as e:
        log(f"  publish failed: {e}")


# ---------------------------------------------------------------- main

def main():
    use_ai = "--no-ai" not in sys.argv
    load_env()
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    keys = gemini_keys()
    if use_ai and not keys:
        log("GEMINI_API_KEY missing; falling back to keyword scoring")
        use_ai = False
    db = open_db()
    if "--rescore" in sys.argv and keys:
        rescore_recent(db, cfg, read_resume(cfg["resume_path"]), keys)
        build_kits(db, cfg, read_resume(cfg["resume_path"]), keys)
    if "--kits-only" in sys.argv and keys:
        build_kits(db, cfg, read_resume(cfg["resume_path"]), keys)
    if "--report-only" in sys.argv or "--kits-only" in sys.argv or "--rescore" in sys.argv:
        row = db.execute("SELECT ts, stats FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
        stats = json.loads(row[1]) if row and row[1] else {"fetched": 0, "candidates": 0, "by_source": {}}
        path, n_new = write_report(db, cfg, row[0] if row else "", stats)
        log(f"Report rebuilt ({n_new} jobs from the latest run): {path}")
        if "--publish" in sys.argv:
            publish()
        if "--no-open" not in sys.argv:
            os.startfile(os.path.join(ROOT, "latest.html"))
        return
    resume = read_resume(cfg["resume_path"])

    prev = last_run(db)
    window = int((NOW - prev).total_seconds()) + 6 * 3600 if prev else 3 * 86400
    window = max(86400, min(window, 7 * 86400))
    if "--days" in sys.argv:
        window = int(sys.argv[sys.argv.index("--days") + 1]) * 86400
    log(f"Run start (LinkedIn window {window // 3600}h, AI={'on, ' + str(len(keys)) + ' key(s)' if use_ai else 'off'})")

    all_jobs, by_source = [], {}
    for name, fn in [("LinkedIn", lambda: src_linkedin(cfg, window)), ("Wellfound", lambda: src_wellfound(cfg)),
                     ("Himalayas", lambda: src_himalayas(cfg)), ("Remotive", lambda: src_remotive(cfg)),
                     ("RemoteOK", lambda: src_remoteok(cfg)), ("Jobicy", lambda: src_jobicy(cfg)),
                     ("WeWorkRemotely", lambda: src_wwr(cfg))]:
        try:
            got = fn()
        except Exception as e:
            log(f"{name} failed: {e}")
            got = []
        by_source[name] = len(got)
        log(f"{name}: {len(got)} postings")
        all_jobs += got

    cands = prefilter(all_jobs, cfg, db)
    cands.sort(key=lambda j: j["posted"] or "", reverse=True)
    cands = cands[: cfg["max_jobs_to_score_per_run"]]
    log(f"{len(cands)} new candidates after filters/dedupe")

    cols = [r[1] for r in db.execute("PRAGMA table_info(jobs)")]
    retry = [dict(zip(cols, r)) for r in db.execute(
        "SELECT * FROM jobs WHERE ai_scored = 0 AND first_seen >= ?",
        ((NOW - timedelta(days=7)).isoformat(),))] if use_ai else []
    if retry:
        log(f"{len(retry)} jobs from earlier runs still need AI scoring")

    failed = set()
    for j in cands:
        if j["source"] == "LinkedIn":
            try:
                linkedin_details(j)
            except Exception as e:
                log(f"  details {j['id']}: {e}")
                failed.add(j["id"])
            pause(0.8, 1.8)
    if failed:
        cands = [j for j in cands if j["id"] not in failed]
        log(f"  {len(failed)} LinkedIn jobs skipped (description didn't load); they'll be retried next run")

    cands = retry + cands
    score_jobs(cands, resume, cfg, keys, use_ai)

    first_seen = NOW.isoformat()
    db.executemany("""INSERT OR REPLACE INTO jobs (id, dedupe_key, source, title, company, location, url, posted,
        first_seen, score, verdict, why, red_flags, years_required, extra, description, ai_scored, region, applicants)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
        (j["id"], j["dedupe_key"], j["source"], j["title"], j["company"], j["location"], j["url"], j["posted"],
         j.get("first_seen") or first_seen, j["score"], j["verdict"], j["why"], j["red_flags"],
         j["years_required"], j["extra"], j["description"], j["ai_scored"], j["region"], j.get("applicants", ""))
        for j in cands])
    stats = {"fetched": len(all_jobs), "candidates": len(cands), "by_source": by_source}
    db.execute("INSERT INTO runs (ts, stats) VALUES (?, ?)", (NOW.isoformat(), json.dumps(stats)))
    db.commit()

    if use_ai:
        build_kits(db, cfg, resume, keys)

    min_score = cfg["min_score_to_show"]
    shown = sorted([j for j in cands if j["score"] >= min_score], key=lambda j: -j["score"])
    append_tracker(shown)
    path, n_new = write_report(db, cfg, first_seen, stats)
    log(f"Done: {n_new} new matches (>= {min_score}). Report: {path}")
    if cfg.get("publish") or "--publish" in sys.argv:
        publish()
    if "--no-open" not in sys.argv and n_new:
        os.startfile(os.path.join(ROOT, "latest.html"))


if __name__ == "__main__":
    main()
