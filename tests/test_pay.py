#!/usr/bin/env python3
"""Regression tests for reading pay out of a posting.

This has broken three separate ways against real postings, each time filing a
number that was wrong rather than none at all - which is worse, because the
ranking believes it:

  1. "(100% In-Office)" read as $100,000, from handing parse_salary a whole
     clause instead of the figure.
  2. "$15.43" read as $15,430 a year, from treating a district's hourly rate
     as an annual salary in thousands.
  3. "8.0 hrs / day. ... Pay Range: Starting at $17.12" read as a daily rate
     worth $3,252, from looking for the unit too far away from the figure.

Run: python3 tests/test_pay.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import sources as src  # noqa: E402

CASES = [
    # (label, posting text, expected (min, max))
    ("neogov range",    "Full Time - $49,858.00 - $56,090.00 Annually", (49858, 56090)),
    ("neogov range 2",  "Full Time - $38,470.00 - $42,798.00 Annually", (38470, 42798)),
    ("hourly stated",   "Starting Pay: $15.10 per hour", (31408, None)),
    ("hourly leading",  "Hourly Rate: $15.43", (32094, None)),
    ("rate, no unit",   "Pay Range: Starting at $17.12 (plus credit)", (35609, None)),
    ("schedule nearby", "8.0 hrs / day. Pay Range: Starting at $17.12", (35609, None)),
    ("schedule far",    "8.0 hrs / day. Does not include a 30 minute lunch. "
                        "Pay Range: Starting at $17.12", (35609, None)),
    ("daily, calendar", "Scheduled Work Days: 179 days Pay Range: "
                        "Starting at $106.54 / day", (19070, None)),
    ("daily, default",  "Salary Range: $113.25 per day", (21517, None)),
    ("annual range",    "Salary Range: $72,102 - $95,923", (72102, 95923)),
    ("k shorthand",     "Compensation: $85 - $95k", (85000, 95000)),
    ("hourly range",    "Pay: $22.00 - $27.50/hr", (45760, 57200)),
    ("percent nearby",  "(100% In-Office) Salary: $45,000+ annually", (45000, None)),
    ("monthly",         "Salary: $6,500 per month", (78000, None)),
    ("bonus only",      "Enjoy a $5,000 sign-on bonus", (None, None)),
    ("retirement",      "401k match, $500 equipment stipend, 100% remote", (None, None)),
    ("says nothing",    "Competitive compensation and benefits", (None, None)),
]

# Titles the shipped example role filter has to keep, and titles it has to
# leave alone. The first list exists because "Director of Technology" was
# being filtered out while "Technology Director" matched - job families get
# titled both ways round, and a pattern that only catches one silently drops
# half of what you care about. Change these lists when you change the filter.
KEEP = [
    "Director of Technology", "Technology Director", "Director of Technology Services",
    "Coordinator of Technology", "IT Director", "Chief Technology Officer",
    "Director of Information Technology", "Computer Technician",
    "Technology Integration Specialist", "Network Administrator",
    "Manager of Information Systems", "Systems Administrator", "Help Desk Technician",
    "SOC Analyst I", "DevOps Engineer", "Site Reliability Engineer",
]
DROP = [
    "Director of Special Education", "Transportation Director", "Food Service Manager",
    "Substitute Teacher", "Custodian Full-time", "High School Assistant Trap Coach",
    "Paraprofessional - Early Childhood", "Director of Curriculum",
    "Assistant Banking Center Manager", "Teller", "Massage Therapist",
]


# Pay scales, as school districts and other public employers publish them: a
# heading naming the job family, then a grid of steps and lanes. Taken from
# the shape of a real district's published schedule.
SCALE = """
Example District Technology Salary Schedule
2026-2027
Years    Level 1    Level 2    Level 3.1
   1   48,545.00  58,765.00  68,985.00
   2   49,170.00  59,390.00  69,610.00
  14   57,645.00  67,865.00  93,110.00

Example District Maintenance Salary Schedule
2026-2027
Years   Custodian
   1       15.43
   2       15.68
   3       15.93
   4       16.10
"""


def scale_checks():
    import payscale
    out = []
    found = payscale.schedules(SCALE)
    tech = next((v for k, v in found.items() if "Technology" in k), None)
    if tech != (48545, 93110):
        out.append("scale technology range: %s, wanted (48545, 93110)" % (tech,))
    # An hourly grid has to be annualized, not skipped: 15.43 * 2080.
    maint = next((v for k, v in found.items() if "Maintenance" in k), None)
    if not maint or maint[0] != 32094:
        out.append("scale hourly grid: %s, wanted a floor of 32094" % (maint,))
    lo, _, head = payscale.for_role(SCALE, "Director of Technology", "Administration")
    if lo != 48545:
        out.append("scale for Director of Technology: %s via %s" % (lo, head))
    if payscale.family("Itinerant Staff") == "technology":
        out.append("scale family: 'Itinerant' read as technology")
    return out


def decode_checks():
    """AppliTrack declares text/html with no charset and writes Windows-1252.

    A single 0x97 em dash in a school district posting raised UnicodeDecodeError
    cost the whole source on that poll cycle.
    """
    import asyncio

    raw = b"<html><p>the regular teacher\x97upholding our culture</p></html>"

    class Resp:
        status = 200
        headers = {"Content-Type": "text/html"}

        async def read(self):
            return raw

    out = []
    try:
        raw.decode("utf-8")
        out.append("decode: the sample is no longer invalid utf-8")
    except UnicodeDecodeError:
        pass
    text = asyncio.run(src._text(Resp()))
    if "teacher\u2014upholding" not in text:
        out.append("decode: cp1252 fallback did not produce an em dash: %r" % text[:60])
    return out


def title_checks():
    """A language in the job title is a hard requirement.

    A "Golang Kubernetes Engineer" posting arrived with a 565 character stub
    of a description, so extraction found nothing and it scored near the top,
    while another posting whose long description spelled out the same
    requirement was correctly blocked.
    """
    import gaps

    profile = {"skills": {"automation": ["Python", "Bash", "PowerShell"]},
               "blockers": {"writes": ["python", "bash", "powershell"]}}
    blocked = ["Golang Kubernetes Engineer", "Senior Go Engineer",
               "Java Developer", "Rust Systems Engineer"]
    clean = ["SOC Analyst I", "Systems Administrator - MOVERS",
             "Tier III Service Desk Engineer", "Director of Technology",
             "Professional Services DevOps Engineer", "Python Automation Engineer"]
    out = []
    for title in blocked:
        _, b = gaps.analyse({}, profile, None, role=title)
        if not b:
            out.append("title not blocked: %s" % title)
    for title in clean:
        _, b = gaps.analyse({}, profile, None, role=title)
        if b:
            out.append("title wrongly blocked: %s -> %s" % (title, b))
    return out


def page_title_checks():
    """A line off a changed page has to read like a job title.

    A district's page changed, "Basic computer skills are required." matched
    the role filter on the word "computer", and it was ingested as a posting
    and alerted on - while the district had no opening in that field at all.
    """
    mb = src
    out = []
    for line in ["Basic computer skills are required.",
                 "Applicants must have basic computer skills",
                 "You will support classroom technology",
                 "Please see the attached job description"]:
        if mb.looks_like_title(line):
            out.append("page title accepted a sentence: %s" % line)
    for line in ["Director of Technology", "Technology Coordinator",
                 "Network Administrator - High School",
                 "Help Desk Technician (Part Time)", "Computer Technician"]:
        if not mb.looks_like_title(line):
            out.append("page title rejected a real title: %s" % line)
    return out


def status_checks():
    """Automation may carry an application forward or end it, never back.

    A signed offer letter arrived two days after an acceptance was recorded by
    hand, and the mail ingest demoted the row from accepted back to offer.
    """
    import jobtrack as jt
    allowed = [("offer", "accepted"), ("applied", "interviewing"),
               ("lead", "applied"), ("applied", "rejected"),
               ("interviewing", "ghosted")]
    blocked = [("accepted", "offer"), ("accepted", "rejected"),
               ("interviewing", "applied"), ("offer", "interviewing"),
               ("applied", "applied")]
    out = []
    for cur, new in allowed:
        if not jt.may_advance(cur, new):
            out.append("status blocked a legitimate move: %s -> %s" % (cur, new))
    for cur, new in blocked:
        if jt.may_advance(cur, new):
            out.append("status allowed a backwards move: %s -> %s" % (cur, new))
    return out


def mail_checks():
    """Drafts are not replies.

    /me/messages spans every folder, Outlook writes a new message id on each
    autosave, and a draft has no sender - so one reply being composed to an
    employer arrived as six replies *from* that employer, each with an empty
    sender the seen-key could not collapse.
    """
    import mail

    sent = {"id": "a", "subject": "RE: your application",
            "from": {"emailAddress": {"address": "recruiter@example.com"}}}
    draft = {"id": "b", "subject": "Re: your application", "isDraft": True,
             "from": {"emailAddress": {"address": ""}}}
    headless = {"id": "c", "subject": "Re: your application", "from": {}}
    out = []
    if not mail.is_correspondence(sent):
        out.append("mail: a real reply was dropped")
    if mail.is_correspondence(draft):
        out.append("mail: a draft was treated as correspondence")
    if mail.is_correspondence(headless):
        out.append("mail: a message with no sender was treated as correspondence")
    return out


def main():
    import re
    failures = (scale_checks() + decode_checks() + title_checks()
                + page_title_checks() + status_checks()
                + mail_checks())

    for label, text, want in CASES:
        got = src.pay_from(text)
        if got != want:
            failures.append("pay  %-16s %s, wanted %s" % (label, got, want))

    rx = re.compile(src.DEFAULT_ROLE_FILTER)
    for title in KEEP:
        if not rx.search(title):
            failures.append("role missed: %s" % title)
    for title in DROP:
        if rx.search(title):
            failures.append("role false positive: %s" % title)

    total = len(CASES) + len(KEEP) + len(DROP) + 6 + 10 + 9 + 10 + 3
    if failures:
        print("FAILED %d of %d" % (len(failures), total))
        for f in failures:
            print("  " + f)
        return 1
    print("passed %d checks" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
