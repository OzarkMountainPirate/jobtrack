#!/usr/bin/env python3
"""Lead sources.

Every adapter is anonymous HTTP - no API keys, no OAuth - and returns the same
normalized dict, so the ingest path does not care where a lead came from.
Adding a source means writing one function and registering it in FETCHERS.

Normalized lead:
    company, role, location, url, source, work_mode, salary_min, salary_max, posted
"""

import asyncio
import difflib
import html
import json
import logging
import os
import re

import aiohttp

import payscale

log = logging.getLogger("jobtrack.sources")

UA = "jobtrack/1.0 (personal job tracker)"
TIMEOUT = aiohttp.ClientTimeout(total=30)

HOURS_PER_YEAR = 2080


CHARSET = re.compile(r"charset=([\w-]+)", re.I)


async def _text(response):
    """Response body as text, whatever the server actually sent.

    AppliTrack serves "Content-Type: text/html" with no charset while writing
    Windows-1252, so one em dash in a school district posting raised
    UnicodeDecodeError and took the whole poll cycle's source with it.
    """
    raw = await response.read()
    declared = CHARSET.search(response.headers.get("Content-Type") or "")
    for enc in (declared.group(1) if declared else None, "utf-8", "cp1252"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def parse_salary(text):
    """Free-text pay into (min, max) annual ints. Returns (None, None) when unsure.

    Hourly and weekly rates are annualized so a single threshold can be applied
    across sources that disagree about units.
    """
    if not text:
        return None, None
    t = str(text).lower().replace(",", "")
    # Retirement plans read as salaries otherwise: "401k match" -> $401,000.
    t = re.sub(r"\b(401\s*k|403\s*b|457\s*b|529)\b", " ", t)
    nums = []
    for raw, suffix in re.findall(r"\$?\s*(\d+(?:\.\d+)?)\s*(k\b)?", t):
        try:
            v = float(raw)
        except ValueError:
            continue
        if suffix:
            v *= 1000
        nums.append(v)
    nums = [n for n in nums if n >= 10]
    if not nums:
        return None, None

    per_hour = re.search(r"(/|per\s*)(hour|hr)\b", t) or " hourly" in t
    per_week = re.search(r"(/|per\s*)(week|wk)\b", t)
    per_month = re.search(r"(/|per\s*)(month|mo)\b", t)
    if per_hour:
        nums = [n * HOURS_PER_YEAR for n in nums]
    elif per_week:
        nums = [n * 52 for n in nums]
    elif per_month:
        nums = [n * 12 for n in nums]
    else:
        # Bare numbers under 1000 in a pay field are almost always thousands.
        nums = [n * 1000 if n < 1000 else n for n in nums]

    lo, hi = int(min(nums)), int(max(nums))
    # Implausible results mean the text was not really a salary.
    if lo < 15000 or hi > 1_000_000:
        return None, None
    return lo, (hi if hi != lo else None)


# parse_salary takes the min and max of every number handed to it, so it can
# only ever be shown a pay figure. Handed a whole posting it would read
# "(100% In-Office)" as $100,000 - which it did, on the first one tested. So
# the surrounding words decide whether a figure is pay, and only the figure
# itself and its unit are passed on.
AMOUNT = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"
                    r"(?:\s*(?:-|\u2013|\u2014|to)\s*\$?\s?\d[\d,]*(?:\.\d{2})?)?")
UNIT = re.compile(r"\b(?:per\s+(?:hour|hr|week|wk|month|mo|year|yr|annum)|hourly|"
                  r"annually|a\s+year|/\s*(?:hr|hour|yr|year))\b", re.I)
PAY_WORDS = re.compile(r"salar|compensat|wage|hourly|annual|\bpay\b|pay range|"
                       r"pay rate|rate of pay|starting at|base\b", re.I)
# Cents are optional on both sides: NeoGov writes "$38,470.00 - $42,798.00",
# and without the decimal here the range never matched.
PAY_RANGE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?\s*(?:-|\u2013|\u2014|to)"
                       r"\s*\$?\s?\d[\d,]*(?:\.\d{2})?")


PER_HOUR = re.compile(r"(?:/|per\s+)\s*(?:hour|hr)\b|hourly", re.I)
PER_DAY = re.compile(r"(?:/|per\s+)\s*day\b|daily|per diem", re.I)
PER_WEEK = re.compile(r"(?:/|per\s+)\s*(?:week|wk)\b|weekly", re.I)
PER_MONTH = re.compile(r"(?:/|per\s+)\s*(?:month|mo)\b|monthly", re.I)
CENTS = re.compile(r"\d+\.\d{2}\b")
# Districts state the contract length in the posting, and it is never a full
# year: 179 or 187 days is a school calendar, not part time.
WORK_DAYS = re.compile(r"(?:work\s*days?|contract|calendar)\D{0,14}(\d{2,3})\s*days?", re.I)


# A unit only counts when it is attached to the figure: immediately after it
# ("$106.54 / day"), or immediately before it ("Hourly Rate: $15.43"). Read any
# wider and "8.0 hrs / day. ... Pay Range: Starting at $17.12" becomes a daily
# rate worth $3,252 a year.
LEAD_UNIT = re.compile(r"(hourly|per\s+hour|/\s*hr|daily|per\s+day|/\s*day|"
                       r"weekly|per\s+week|monthly|per\s+month)"
                       r"\s*(?:rate|pay|wage)?\s*(?:of|:|is|at)?\s*$", re.I)


def _period(lead, tail):
    for name, rx in (("day", PER_DAY), ("hour", PER_HOUR),
                     ("week", PER_WEEK), ("month", PER_MONTH)):
        if rx.search(tail[:40]):
            return name
    m = LEAD_UNIT.search(lead[-25:])
    if not m:
        return None
    w = m.group(1).lower()
    for name in ("day", "hour", "hr", "week", "month"):
        if name in w:
            return "hour" if name == "hr" else name
    return None


def _annualize(amount, period, text):
    """The figures in one pay clause, as annual dollars.

    A rate is not a salary. "Pay Range: Starting at $15.43" is $32k a year,
    not $15,430, and "$106.54 / day" over a 179 day school calendar is $19k,
    not $106,540 - both of which this filed before rates were handled. Cents
    are the tell: nobody writes an annual salary as $15.43.
    """
    nums = [float(x.replace(",", "")) for x in
            re.findall(r"\d[\d,]*(?:\.\d+)?", amount)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return None, None

    if period == "day":
        m = WORK_DAYS.search(text or "")
        nums = [n * (int(m.group(1)) if m else 190) for n in nums]
    elif period == "hour" or (not period and CENTS.search(amount) and max(nums) < 1000):
        nums = [n * HOURS_PER_YEAR for n in nums]
    elif period == "week":
        nums = [n * 52 for n in nums]
    elif period == "month":
        nums = [n * 12 for n in nums]
    else:
        # A bare number under 1000 in a pay field is thousands: "$85 - $95k".
        nums = [n * 1000 if n < 1000 else n for n in nums]

    lo, hi = int(min(nums)), int(max(nums))
    if lo < 15000 or hi > 1_000_000:
        return None, None
    return lo, (hi if hi != lo else None)


def pay_from(text):
    """(min, max) from a posting body, or (None, None) when it does not say.

    A figure only counts when the words around it are about pay, or when it is
    written as a range - a lone "$5,000 sign-on bonus" is not this job's
    salary, and a wrong number here corrupts the ranking.
    """
    if not text:
        return None, None
    fallback = None
    for m in AMOUNT.finditer(text):
        lead = text[max(0, m.start() - 100):m.start()]
        tail = text[m.end():m.end() + 60]
        period = _period(lead, tail)
        if PAY_WORDS.search(lead) or PAY_WORDS.search(tail):
            # Keep looking rather than returning here: a posting often names a
            # benefit before it names the pay, and one unusable candidate must
            # not stop the scan.
            lo, hi = _annualize(m.group(0), period, text)
            if lo:
                return lo, hi
        elif fallback is None and PAY_RANGE.search(m.group(0)):
            fallback = (m.group(0), period)
    return _annualize(fallback[0], fallback[1], text) if fallback else (None, None)


def guess_work_mode(*fields):
    blob = " ".join(str(f or "") for f in fields).lower()
    if "remote" in blob:
        return "remote"
    if "hybrid" in blob:
        return "hybrid"
    return None


def _plain(markup, limit=6000):
    """HTML or CDATA to plain text, trimmed. Requirements sit near the top of a
    posting, so a leading slice keeps what matters."""
    if not markup:
        return None
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(markup),
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())[:limit] or None


async def _get_json(session, url, params=None, headers=None):
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    async with session.get(url, params=params, headers=hdrs, timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s from %s" % (r.status, url))
        # Some boards serve JSON as text/plain.
        return json.loads(await r.text())


# ----------------------------------------------------------------- adapters

async def fetch_remotive(session, query=None, **_):
    """Remotive aggregates remote roles. Public, no auth."""
    params = {"limit": "50"}
    if query:
        params["search"] = query
    data = await _get_json(session, "https://remotive.com/api/remote-jobs", params)
    out = []
    for j in data.get("jobs") or []:
        lo, hi = parse_salary(j.get("salary"))
        out.append({
            "company": (j.get("company_name") or "").strip(),
            "role": (j.get("title") or "").strip(),
            "location": (j.get("candidate_required_location") or "Remote").strip(),
            "url": j.get("url"),
            "source": "remotive",
            "work_mode": "remote",
            "salary_min": lo,
            "salary_max": hi,
            "posted": (j.get("publication_date") or "")[:10],
            "description": _plain(j.get("description")),
        })
    return out


async def fetch_greenhouse(session, token, **_):
    data = await _get_json(
        session, "https://boards-api.greenhouse.io/v1/boards/%s/jobs" % token)
    out = []
    for j in data.get("jobs") or []:
        loc = (j.get("location") or {}).get("name")
        out.append({
            "company": token,
            "role": (j.get("title") or "").strip(),
            "location": loc,
            "url": j.get("absolute_url"),
            "source": "greenhouse",
            "work_mode": guess_work_mode(loc, j.get("title")),
            "salary_min": None, "salary_max": None,
            "posted": (j.get("updated_at") or "")[:10],
        })
    return out


async def fetch_lever(session, token, **_):
    data = await _get_json(
        session, "https://api.lever.co/v0/postings/%s" % token, {"mode": "json"})
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        loc = cat.get("location")
        out.append({
            "company": token,
            "role": (j.get("text") or "").strip(),
            "location": loc,
            "url": j.get("hostedUrl"),
            "source": "lever",
            "work_mode": guess_work_mode(loc, cat.get("commitment"), j.get("workplaceType")),
            "salary_min": None, "salary_max": None,
            "posted": "",
        })
    return out


async def fetch_ashby(session, token, **_):
    data = await _get_json(
        session, "https://api.ashbyhq.com/posting-api/job-board/%s" % token)
    out = []
    for j in data.get("jobs") or []:
        comp = j.get("compensation") or {}
        lo, hi = parse_salary(comp.get("compensationTierSummary"))
        out.append({
            "company": token,
            "role": (j.get("title") or "").strip(),
            "location": j.get("location"),
            "url": j.get("jobUrl"),
            "source": "ashby",
            "work_mode": "remote" if j.get("isRemote") else guess_work_mode(j.get("location")),
            "salary_min": lo, "salary_max": hi,
            "posted": (j.get("publishedAt") or "")[:10],
        })
    return out


async def fetch_workable(session, token, **_):
    data = await _get_json(
        session, "https://apply.workable.com/api/v1/widget/accounts/%s" % token)
    out = []
    for j in data.get("jobs") or []:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append({
            "company": token,
            "role": (j.get("title") or "").strip(),
            "location": loc or None,
            "url": j.get("url") or j.get("application_url"),
            "source": "workable",
            "work_mode": guess_work_mode(loc, j.get("title"), j.get("telecommuting")),
            "salary_min": None, "salary_max": None,
            "posted": (j.get("published_on") or "")[:10],
        })
    return out


# A page that reprints the clock changes every time it is fetched. One board
# fired "PAGE CHANGED" on nothing but "Postings current as of <date> <time>"
# - one line in, one line out, the same 2,666 characters.
VOLATILE = re.compile(
    r"(?i)\b(current as of|openings as of|last (?:updated|modified|refreshed)|"
    r"generated (?:on|at)|retrieved|page loaded|"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)\b)")


# A job title is a noun phrase, not a sentence. A district's page changed, the
# line "Basic computer skills are required." matched the role filter on the
# word "computer", and it was ingested as a posting and alerted on - while the
# district had no opening in that field at all.
SENTENCE = re.compile(
    r"(?i)(\b(?:is|are|was|were|be|been|will|would|must|should|can|may|have|"
    r"has|do|does|please|we|you|your|our|their|they|it|this|that)\b|"
    r"[.!?]\s*$|^\W)")


def looks_like_title(line):
    """Whether a line off a changed page reads like a job title."""
    if not line or not (3 < len(line) <= 90):
        return False
    if len(line.split()) > 8:
        return False
    return not SENTENCE.search(line)


def added_lines(prev, cur):
    """The lines this change actually added, ignoring the clock."""
    out = []
    for line in difflib.unified_diff((prev or "").splitlines(),
                                     (cur or "").splitlines(),
                                     n=0, lineterm=""):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:].strip()
        if body and not VOLATILE.search(body):
            out.append(body)
    return out


async def fetch_changedetection(session, base_url=None, api_key=None,
                               tag="districts", **_):
    """Career pages with no feed, watched by changedetection.io.

    Returns one entry per watch whose content genuinely changed, carrying the
    lines that were added. The caller decides whether any of them look like a
    posting; a watch that only moved its timestamp is dropped here and never
    reaches the room.
    """
    base_url = (base_url or os.environ.get("CD_URL", "http://changedetection:5000")).rstrip("/")
    api_key = api_key or os.environ.get("CD_API_KEY")
    if not api_key:
        raise RuntimeError("no CD_API_KEY set")
    headers = {"x-api-key": api_key, "User-Agent": UA}

    async def text(path):
        async with session.get(base_url + path, headers=headers,
                               timeout=TIMEOUT) as r:
            return await r.text()

    async with session.get("%s/api/v1/watch" % base_url, params={"tag": tag},
                           headers=headers, timeout=TIMEOUT) as r:
        watches = json.loads(await r.text())

    out = []
    for uuid, w in watches.items():
        changed = int(w.get("last_changed") or 0)
        if not changed:
            continue
        title = (w.get("title") or w.get("url") or "").replace(" - jobs", "")
        snapshot, added = "", []
        try:
            stamps = sorted(json.loads(await text("/api/v1/watch/%s/history" % uuid)),
                            key=int)
            snapshot = await text("/api/v1/watch/%s/history/%s" % (uuid, stamps[-1]))
            if len(stamps) > 1:
                prev = await text("/api/v1/watch/%s/history/%s" % (uuid, stamps[-2]))
                added = added_lines(prev, snapshot)
            else:
                added = [l.strip() for l in snapshot.splitlines() if l.strip()]
        except Exception as e:
            log.warning("changedetection history for %s: %s", title, e)
            continue

        # Nothing but the clock moved. Saying so out loud is pure noise: an
        # alert you check, find nothing in, and learn to skip.
        if not added:
            log.info("changedetection: %s changed, but only volatile text", title)
            continue

        out.append({
            "company": title.strip(),
            "role": _category_summary(snapshot),
            "location": None,
            "url": w.get("url"),
            "source": "changedetection",
            "work_mode": "onsite",
            "salary_min": None, "salary_max": None,
            "posted": "",
            "_changed_at": changed,
            "_uuid": uuid,
            "_added": added,
        })
    return out


CATEGORY_RE = re.compile(r"^\s*\*\s*(.+?)\s*\((\d+)\)\s*$", re.M)


def _category_summary(snapshot):
    """Turn the rendered category block into one line, keeping the counts."""
    cats = CATEGORY_RE.findall(snapshot or "")
    if not cats:
        return "postings changed"
    return ", ".join("%s (%s)" % (name, n) for name, n in cats)


PAYLOCITY_JOB_RE = re.compile(r'\{"JobId":\d+,.*?"IndeedRemoteType":\d+\}')


async def fetch_paylocity(session, token, company=None, **_):
    """Paylocity recruiting boards. The listing page embeds its jobs as JSON,
    so this parses structured rows rather than scraping rendered HTML."""
    url = "https://recruiting.paylocity.com/recruiting/jobs/All/%s/x" % token
    async with session.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s from paylocity" % r.status)
        body = await _text(r)

    out = []
    for m in PAYLOCITY_JOB_RE.finditer(body):
        try:
            j = json.loads(m.group(0))
        except ValueError:
            continue
        loc = (j.get("LocationName") or "").replace(" Office", "")
        out.append({
            "company": company or token,
            "role": (j.get("JobTitle") or "").strip(),
            "location": loc or None,
            "url": "https://recruiting.paylocity.com/recruiting/jobs/Details/%s" % j.get("JobId"),
            "source": "paylocity",
            "work_mode": "remote" if j.get("IsRemote") else guess_work_mode(loc),
            "salary_min": None, "salary_max": None,
            "posted": (j.get("PublishedDate") or "")[:10],
        })
    return out


async def fetch_hiretrue(session, host, board_key, board_id=None,
                        employer=None, **_):
    """HireTrue / "ce3" public job boards. Public JSON, no auth.

    Several US state governments run these. `host` is the board's hostname,
    `board_key` the numeric jobBoardPrimaryKey in its URL, and `board_id` the
    UUID that appears in per-job links. Open the board in a browser and both
    are visible in the address bar.
    """
    url = ("https://%s/hiretrue/api/ce3/job-board/requisitions"
           "?jobBoardPrimaryKey=%s" % (host, board_key))
    data = await _get_json(session, url)
    board = board_id or ""
    out = []
    for j in data if isinstance(data, list) else []:
        loc = j.get("location") or j.get("facility")
        out.append({
            "company": "%s - %s" % (employer, (j.get("department") or "").strip())
                       if (employer and j.get("department")) else
                       (employer or j.get("department") or "unknown"),
            "role": (j.get("title") or "").strip(),
            "location": loc,
            "url": "https://%s/hiretrue/ce3/job-board/%s/job/%s" % (
                host, board, j.get("externalId")),
            "source": "hiretrue",
            "work_mode": guess_work_mode(loc, j.get("positionType")),
            "salary_min": None, "salary_max": None,
            "posted": "",
        })
    return out


APPLITRACK_URL = re.compile(r"(?i)https?://([a-z0-9.-]+)/([a-z0-9_-]+)/onlineapp")
AT_JOBID = re.compile(r"^JobID:\s*(\d+)$", re.I)
AT_LABELS = ("position type:", "date posted:", "location:", "closing date:",
             "additional information:", "jobid:", "email to a friend")
AT_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _text_lines(markup):
    """Markup to non-empty text lines.

    AppliTrack lays each posting out as a label on one line and its value on
    the next, so the line breaks are the structure and collapsing whitespace
    the way _plain does would destroy it.
    """
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    return [l.strip() for l in html.unescape(t).split("\n") if l.strip()]


def _at_date(value):
    m = AT_DATE.match((value or "").strip())
    return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2))) if m else ""


def parse_applitrack(markup, base):
    """Every posting on an AppliTrack board, with its body."""
    lines = _text_lines(markup)
    marks = [i for i, l in enumerate(lines) if AT_JOBID.match(l)]
    out = []
    for n, i in enumerate(marks):
        end = marks[n + 1] - 1 if n + 1 < len(marks) else len(lines)
        block, title = lines[i:end], (lines[i - 1] if i else "")

        def field(label):
            for k, l in enumerate(block):
                if l.lower() == label:
                    vals = []
                    for v in block[k + 1:k + 4]:
                        if v.lower() in AT_LABELS or v.lower().startswith("show/hide"):
                            break
                        vals.append(v)
                    return " ".join(vals).strip().rstrip("/").strip()
            return None

        body = []
        for k, l in enumerate(block):
            if l.lower().startswith("show/hide"):
                body = block[k + 1:]
                break
        job_id = AT_JOBID.match(block[0]).group(1)
        out.append({
            "job_id": job_id,
            "title": title,
            "category": field("position type:"),
            "location": field("location:"),
            "posted": _at_date(field("date posted:")),
            "description": _plain(" ".join(body)),
            "url": "%s?all=1&AppliTrackJobId=%s&AppliTrackLayoutMode=detail"
                   "&AppliTrackViewPosting=1" % (base, job_id),
        })
    return out


class _Lazy:
    """Fetch once, on first use, and only if something actually asks.

    Most district postings state their own pay or are not tech roles, so the
    scale document is usually not needed at all.
    """

    def __init__(self, fn, *args):
        self._fn, self._args, self._done, self._value = fn, args, False, None

    async def get(self):
        if not self._done:
            self._done = True
            try:
                self._value = await self._fn(*self._args)
            except Exception as e:
                log.warning("pay scale unavailable: %s", e)
        return self._value


async def _payscale_text(session, url):
    """A district pay scale as text, whether it is served as PDF or HTML."""
    if not url:
        return None
    async with session.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s from pay scale" % r.status)
        data = await r.read()
        ctype = (r.headers.get("Content-Type") or "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf") or data[:4] == b"%PDF":
        return payscale.text_from_pdf(data)
    return _plain(data.decode("utf-8", "replace"), limit=200000)


async def fetch_applitrack(session, url=None, host=None, tenant=None,
                           company=None, salary_schedule=None, **_):
    """Frontline/AppliTrack district boards.

    Districts link to an embedded view that renders a category summary and
    nothing else, which is why these started life as changedetection watches:
    a notification that a page moved, with no posting behind it. The same view
    with all=1 renders every posting in full - title, category, date, location
    and body - so a district board can be ingested like any other source, and
    the role filter decides what is worth seeing, rather than you reading a
    category count.
    """
    if url and not (host and tenant):
        m = APPLITRACK_URL.search(url)
        if not m:
            raise RuntimeError("not an applitrack url: %s" % url)
        host, tenant = host or m.group(1), tenant or m.group(2)
    if not (host and tenant):
        raise RuntimeError("applitrack needs a url, or host and tenant")

    base = "https://%s/%s/onlineapp/JobPostings/view.asp" % (host, tenant)
    scale = _Lazy(_payscale_text, session, salary_schedule)
    async with session.get(base, params={"embed": "1", "all": "1"},
                           headers={"User-Agent": UA}, timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s from applitrack" % r.status)
        body = await _text(r)

    out = []
    for j in parse_applitrack(body, base):
        if not j["title"]:
            continue
        lo, hi = pay_from(j["description"])
        loc = j["location"]
        # Districts are required to publish a pay scale and it is public, but
        # it lives on the district site rather than inside the posting. Where
        # the posting stays silent, carry the link so the number is one click
        # away instead of a search - guessing a lane and step off a schedule
        # would put a wrong figure into the ranking.
        note = "category: %s" % j["category"] if j["category"] else None
        if not lo and salary_schedule:
            # The posting is silent, so fall back to the district's published
            # scale. Only the floor is recorded: the top of these grids is
            # thirty years of steps, and treating it as this job's pay would
            # tell the ranking a Level 1 opening pays his target.
            floor, ceiling, heading = payscale.for_role(
                await scale.get(), j["title"], j["category"])
            if floor:
                lo = floor
                note = "; ".join(filter(None, [
                    note, "%s scale $%s-$%s" % (heading, f"{floor:,}", f"{ceiling:,}")]))
            else:
                note = "; ".join(filter(None,
                                        [note, "pay scale: %s" % salary_schedule]))
        out.append({
            "company": company or tenant.replace("-", " ").title(),
            "role": j["title"],
            "location": loc,
            "url": j["url"],
            "source": "applitrack",
            "work_mode": guess_work_mode(loc, j["description"]) or "onsite",
            "salary_min": lo, "salary_max": hi,
            "posted": j["posted"],
            "description": j["description"],
            "notes": note,
        })
    return out


async def fetch_neogov(session, agency, company=None, **_):
    """NeoGov / governmentjobs.com agency portals.

    The public page is a shell - its listing arrives from careers/home/index,
    which needs no sign-in and carries the pay range, category, department and
    the opening of the description. NeoGov has no per-agency alert to
    subscribe to, so polling this replaces a signup that does not exist.
    """
    async with session.get("https://www.governmentjobs.com/careers/home/index",
                           params={"agency": agency, "sort": "PostingDate",
                                   "isDescendingSort": "true"},
                           headers={"User-Agent": UA,
                                    "X-Requested-With": "XMLHttpRequest"},
                           timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s from governmentjobs" % r.status)
        body = await _text(r)

    out = []
    for chunk in body.split('<li class="list-item"')[1:]:
        link = re.search(r'class="item-details-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         chunk, re.S)
        if not link:
            continue
        meta = re.search(r'<ul class="list-meta">(.*?)</ul>', chunk, re.S)
        cells = [_plain(x) or "" for x in
                 re.findall(r"<li[^>]*>(.*?)</li>", meta.group(1), re.S)] if meta else []
        pay = next((c for c in cells if "$" in c), "")
        lo, hi = pay_from(pay)
        category = next((c for c in cells if c.lower().startswith("category:")), "")
        desc = re.search(r'<div class="list-entry">(.*?)</div>', chunk, re.S)
        loc = cells[0] if cells else None
        out.append({
            "company": company or agency.title(),
            "role": _plain(link.group(2)),
            "location": loc,
            "url": "https://www.governmentjobs.com" + link.group(1),
            "source": "neogov",
            "work_mode": guess_work_mode(loc, pay),
            "salary_min": lo, "salary_max": hi,
            "posted": "",
            "description": _plain(desc.group(1)) if desc else None,
            "notes": category or None,
        })
    return out


async def fetch_acadp(session, url, company=None, pages=2, **_):
    """Advanced Classifieds & Directory Pro boards.

    A WordPress plugin that a lot of small regional job boards run on.

    Local employers post here directly rather than through an ATS, so there is
    no structure to lean on beyond the plugin's own markup, and the role
    filter is the only thing between these and the database.
    """
    out, base = [], url.rstrip("/")
    for page in range(1, max(1, int(pages)) + 1):
        target = base + ("/" if page == 1 else "/page/%d/" % page)
        async with session.get(target, headers={"User-Agent": UA},
                               timeout=TIMEOUT) as r:
            if r.status != 200:
                break
            body = await _text(r)

        entries = re.split(r'<div class="row acadp-entry"', body)[1:]
        if not entries:
            break
        for chunk in entries:
            link = re.search(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                             chunk, re.S)
            if not link:
                continue
            desc = re.search(r'class="acadp-listings-desc"[^>]*>(.*?)</p>', chunk, re.S)
            loc = re.search(r'listing-location/([^/"]+)/', chunk)
            cat = re.search(r'listing-category/([^/"]+)/', chunk)
            title = _plain(link.group(2)) or ""
            text = _plain(desc.group(1)) if desc else None
            lo, hi = pay_from("%s %s" % (title, text or ""))
            place = loc.group(1).replace("-", " ").title() if loc else None
            out.append({
                "company": company or "Lake Job",
                "role": title,
                "location": place,
                "url": link.group(1),
                "source": "acadp",
                "work_mode": guess_work_mode(title, place, text),
                "salary_min": lo, "salary_max": hi,
                "posted": "",
                "description": text,
                "notes": cat.group(1).replace("-", " ") if cat else None,
            })
    return out


async def fetch_workday(session, tenant, site, wd="wd1", company=None,
                        query="", max_pages=5, **_):
    """Workday job boards. Every tenant exposes the same public JSON endpoint
    its own careers UI calls, so no scraping and no auth.

    tenant/wd/site come from the careers URL:
        https://TENANT.WD.myworkdayjobs.com/en-US/SITE
    """
    base = "https://%s.%s.myworkdayjobs.com" % (tenant, wd)
    url = "%s/wday/cxs/%s/%s/jobs" % (base, tenant, site)
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Content-Type": "application/json"}
    out, offset, total = [], 0, None
    for _ in range(max_pages):
        payload = {"appliedFacets": {}, "limit": 20, "offset": offset,
                   "searchText": query}
        async with session.post(url, json=payload, headers=headers,
                                timeout=TIMEOUT) as r:
            if r.status != 200:
                raise RuntimeError("HTTP %s from workday/%s" % (r.status, tenant))
            data = json.loads(await r.text())
        posts = data.get("jobPostings") or []
        for j in posts:
            loc = j.get("locationsText")
            path = j.get("externalPath") or ""
            out.append({
                "company": company or tenant,
                "role": (j.get("title") or "").strip(),
                "location": loc,
                "url": "%s/en-US/%s%s" % (base, site, path),
                "source": "workday",
                "work_mode": guess_work_mode(loc, j.get("title")),
                "salary_min": None, "salary_max": None,
                "posted": "",
            })
        # Workday reports the result count on the first page only and sends
        # total=0 thereafter, so it has to be captured once.
        if total is None:
            total = int(data.get("total") or 0)
        offset += len(posts)
        if len(posts) < 20 or (total and offset >= total):
            break
    return out


async def fetch_bamboohr(session, token, company=None, **_):
    """BambooHR career sites expose a public JSON list at /careers/list."""
    data = await _get_json(
        session, "https://%s.bamboohr.com/careers/list" % token)
    out = []
    for j in data.get("result") or []:
        loc = j.get("location") or {}
        if isinstance(loc, dict):
            loc = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
        out.append({
            "company": company or token,
            "role": (j.get("jobOpeningName") or "").strip(),
            "location": loc or None,
            "url": "https://%s.bamboohr.com/careers/%s" % (token, j.get("id")),
            "source": "bamboohr",
            "work_mode": "remote" if j.get("isRemote") else guess_work_mode(loc),
            "salary_min": None, "salary_max": None,
            "posted": "",
        })
    return out


async def fetch_remoteok(session, query=None, **_):
    """RemoteOK's public feed. Roughly 100 recent remote roles, and one of the
    few sources that carries a salary range at all."""
    data = await _get_json(session, "https://remoteok.com/api")
    out = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        lo, hi = j.get("salary_min"), j.get("salary_max")
        out.append({
            "company": (j.get("company") or "").strip(),
            "role": (j.get("position") or "").strip(),
            "location": j.get("location") or "Remote",
            "url": j.get("url") or j.get("apply_url"),
            "source": "remoteok",
            "work_mode": "remote",
            "salary_min": int(lo) if lo else None,
            "salary_max": int(hi) if hi else None,
            "posted": (j.get("date") or "")[:10],
            "description": _plain(j.get("description")),
        })
    return out


WWR_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
WWR_FIELD = {
    "title": re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S),
    "link": re.compile(r"<link>(.*?)</link>", re.S),
    "region": re.compile(r"<region>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</region>", re.S),
    "description": re.compile(
        r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", re.S),
}


async def fetch_weworkremotely(session, category="remote-devops-sysadmin-jobs", **_):
    """WeWorkRemotely category RSS. Their devops-sysadmin category is squarely
    the work you want, and RSS is published for exactly this use."""
    url = "https://weworkremotely.com/categories/%s.rss" % category
    async with session.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s from weworkremotely" % r.status)
        body = await _text(r)

    out = []
    for raw in WWR_ITEM.findall(body):
        got = {}
        for key, rx in WWR_FIELD.items():
            m = rx.search(raw)
            got[key] = html.unescape(m.group(1)).strip() if m else None
        title = got.get("title") or ""
        # Titles read "Company: Role".
        company, _, role = title.partition(":")
        if not role:
            company, role = "", title
        out.append({
            "company": company.strip() or "unknown",
            "role": role.strip(),
            "location": got.get("region") or "Remote",
            "url": got.get("link"),
            "source": "weworkremotely",
            "work_mode": "remote",
            "salary_min": None, "salary_max": None,
            "posted": "",
            "description": _plain(got.get("description")),
        })
    return out


FETCHERS = {
    "bamboohr": fetch_bamboohr,
    "remoteok": fetch_remoteok,
    "weworkremotely": fetch_weworkremotely,
    "changedetection": fetch_changedetection,
    "applitrack": fetch_applitrack,
    "neogov": fetch_neogov,
    "acadp": fetch_acadp,
    "paylocity": fetch_paylocity,
    "hiretrue": fetch_hiretrue,
    "workday": fetch_workday,
    "remotive": fetch_remotive,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
}


# ------------------------------------------------------------------ config

def config_path():
    return os.environ.get("SOURCES_FILE", "/data/sources.json")


# Aggregator search parameters are loose - Remotive returns sales and marketing
# roles for a "system administrator" query - so relevance is enforced here rather
# than trusted to the upstream API.
DEFAULT_ROLE_FILTER = (
    r"(?i)\b(sysadmin|system[s]? admin\w*|systems? engineer|"
    r"devops|dev ?ops|sre\b|site reliab\w*|"
    r"platform engineer|cloud (engineer|architect|admin\w*|ops)|"
    r"infrastructure( engineer| architect| admin\w*)?|"
    r"kubernetes|terraform|ansible|"
    r"help ?desk|service desk|desktop support|desktop technician|"
    r"network (admin\w*|engineer|technician|specialist|analyst|architect)|"
    r"database admin\w*|"
    r"it (support|technician|specialist|manager|director|admin\w*|analyst)|"
    r"information (technology|systems?)|"
    r"security (engineer|analyst|operations|architect|specialist)|"
    r"secops|sec ?ops|soc\b|noc\b|cyber ?security|"
    r"tech(nology)? (specialist|coordinator|director|technician|engineer|"
    r"integration|administrator|manager|supervisor|analyst)|"
    # Job families get titled both ways round - "Director of Technology" and
    # "Technology Director" - and a pattern that only matches one form will
    # silently drop half the postings you care about.
    r"(director|coordinator|manager|supervisor|head|chief) of "
    r"(technolog\w*|information (technolog\w*|systems?)|it\b|computer|"
    r"network|infrastructure)|"
    r"chief (technology|information|information security) officer|"
    r"computer (technician|specialist|support|operator)|"
    r"support (technician|specialist|engineer|analyst)|"
    r"endpoint|jamf|intune|active directory|vmware|m365|microsoft 365)\b"
)

DEFAULT_CONFIG = {
    "_comment": [
        "One entry per source. 'kind' picks the adapter, 'token' is that board's",
        "company identifier. For greenhouse/lever/ashby/workable the token is the",
        "company slug in its job board URL - e.g. boards.greenhouse.io/TOKEN.",
        "Set enabled=false to park one without deleting it.",
        "role_filter is a regex every lead's title must match to be ingested;",
        "set it to null to accept everything from that source.",
        "location_filter does the same against the location, for employer boards",
        "where you want every local opening regardless of job title.",
    ],
    "role_filter": DEFAULT_ROLE_FILTER,
    "sources": [
        {"kind": "remotive", "query": "system administrator", "enabled": True},
        {"kind": "remotive", "query": "help desk", "enabled": True},
        {"kind": "remotive", "query": "network engineer", "enabled": True},
        {"kind": "greenhouse", "token": "EXAMPLE-replace-me", "enabled": False},
    ],
}


def load_config():
    path = config_path()
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.info("wrote starter source config to %s", path)
        return DEFAULT_CONFIG
    with open(path) as f:
        return json.load(f)


async def poll_all(config=None):
    """Run every enabled source. Returns (leads, errors).

    Leads are filtered against role_filter here, so callers receive only
    relevant rows and the database never accumulates noise.
    """
    config = config or load_config()
    global_filter = config.get("role_filter", DEFAULT_ROLE_FILTER)
    global_loc = config.get("location_filter")
    leads, errors = [], []
    async with aiohttp.ClientSession() as session:
        tasks = []
        for entry in config.get("sources") or []:
            if not entry.get("enabled", True):
                continue
            fn = FETCHERS.get(entry.get("kind"))
            if not fn:
                errors.append("unknown source kind: %s" % entry.get("kind"))
                continue
            tasks.append((entry, fn(session, **{k: v for k, v in entry.items()
                                                if k not in ("kind", "enabled")})))
        for entry, task in tasks:
            label = "%s/%s" % (entry.get("kind"),
                               entry.get("company") or entry.get("agency")
                               or entry.get("token") or entry.get("query")
                               or entry.get("url") or "")
            try:
                got = await task
                pattern = entry.get("role_filter", global_filter)
                keep = [x for x in got if x.get("company") and x.get("role")]
                if pattern:
                    rx = re.compile(pattern)
                    keep = [x for x in keep if rx.search(x["role"])]
                # Employer-specific sources want every local opening rather than
                # only IT-titled ones, so they filter on geography instead.
                loc_pattern = entry.get("location_filter", global_loc)
                if loc_pattern:
                    lrx = re.compile(loc_pattern)
                    keep = [x for x in keep if lrx.search(str(x.get("location") or ""))]
                leads.extend(keep)
                log.info("%s returned %d, kept %d", label, len(got), len(keep))
            except Exception as e:
                errors.append("%s: %s" % (label, e))
                log.warning("%s failed: %s", label, e)
    return leads, errors
