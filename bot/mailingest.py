#!/usr/bin/env python3
"""Turn mail into database activity.

Two jobs from one pass over the mailbox:

  tracking  - an employer replies, so log an event against that application and
              reset its quiet clock. Rejections flip the status.
  discovery - a job alert arrives, so record the postings as leads. This is the
              only route to districts that offer email signup and nothing else.

Matching is deliberately conservative. A wrong match writes a false event onto
a real application and corrupts the follow-up clock, which is worse than
missing one, so anything unmatched is reported rather than guessed at.
"""

import logging
import re

import jobtrack as jt

log = logging.getLogger("jobtrack.mailingest")

# Actual job boards. Generic noreply/notification patterns are deliberately
# absent: they match every transactional email a bank or SaaS product sends,
# which files bank statements as job alerts.
ALERT_SENDERS = re.compile(
    r"(?i)(moreap|applitrack|frontlineeducation|schoolspring|k12jobspot|"
    r"hirenimble|indeed\.com|ziprecruiter|glassdoor|monster\.com|"
    r"governmentjobs|neogov|remotive|myworkdayjobs|paylocity|"
    r"bamboohr|smartrecruiters|greenhouse\.io|jobs\.lever\.co|"
    r"linkedin.*job|job.*alert|careers?@|recruit)")

# Mail that is plausibly about a job. Anything else is ordinary inbox traffic
# and is dropped silently rather than reported as unmatched every poll.
JOB_RELATED = re.compile(
    r"(?i)\b(applica(tion|nt)|position|job|role|interview|resume|"
    r"cv\b|hiring|candidate|recruit|opening|vacancy|employment|offer|"
    r"onboarding|human resources|\bHR\b)")

REJECTION = re.compile(
    r"(?i)(unfortunately|not (be )?(moving|proceeding|selected)|another candidate|"
    r"other candidates|decided to (move|go) (forward|another)|"
    r"will not be (moving|considering)|no longer under consideration|"
    r"position has been filled|pursue other applicants|not to move forward)")

INTERVIEW = re.compile(
    r"(?i)(schedule (an?|your) interview|invite you to interview|"
    r"set up (a|an) (call|interview|time)|phone screen|would like to (meet|speak)|"
    r"availability (for|to) (a|an)? ?(call|interview|chat))")

OFFER = re.compile(r"(?i)(offer letter|pleased to offer|formal offer|job offer)")

# Generic mail hosts, useless for identifying an employer.
FREEMAIL = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "aol.com",
            "icloud.com", "live.com", "msn.com", "protonmail.com"}


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


TLDS = {"com", "org", "net", "edu", "gov", "mil", "int", "us", "co", "uk",
        "io", "coop", "info", "biz", "dev", "app", "tech", "online", "site",
        "email", "mail", "cloud", "health", "care", "ca", "au", "de"}


def _domain_root(addr):
    """example.co.uk -> example. Good enough to compare against a company name."""
    dom = (addr or "").split("@")[-1].lower()
    parts = [p for p in dom.split(".") if p not in TLDS]
    return parts[-1] if parts else dom


def match_application(con, msg):
    """The application this message is about, or None.

    Tried in order of confidence: a stored contact's exact address, then the
    sender's domain against the company name, then the company name appearing
    in the subject.
    """
    addr = msg.get("from_addr") or ""
    dom = addr.split("@")[-1].lower()

    if addr:
        row = con.execute(
            "SELECT application_id FROM contacts WHERE lower(email) = ?",
            (addr,)).fetchone()
        if row:
            return row["application_id"], "contact email"

    apps = [dict(r) for r in con.execute(
        "SELECT id, company, role, status FROM applications")]
    if not apps:
        return None

    if dom and dom not in FREEMAIL:
        root = _domain_root(addr)
        # Prefix rather than substring, and a longer minimum. A bare substring
        # test matched an electric bill from smarthub.coop to "Laclede Electric
        # Cooperative", because "coop" appears inside "cooperative".
        if len(root) >= 5:
            for a in apps:
                flat = _norm(a["company"]).replace(" ", "")
                if flat.startswith(root) or root.startswith(flat):
                    return a["id"], "sender domain"

    subject = _norm(msg.get("subject"))
    blob = "%s %s" % (subject, _norm(msg.get("preview")))

    # Whole company name appearing verbatim is high confidence, and is the only
    # thing that works for short company names whose individual words
    # are too common to match on.
    phrase = [a for a in apps if len(_norm(a["company"])) > 6
              and _norm(a["company"]) in blob]
    if len(phrase) == 1:
        return phrase[0]["id"], "company name in text"

    best = None
    for a in apps:
        comp = _norm(a["company"])
        # Compare on the distinctive part of the name, not "inc" or "school".
        words = [w for w in comp.split()
                 if len(w) > 3 and w not in ("school", "district", "county",
                                             "city", "health", "systems",
                                             "technologies", "solutions", "inc")]
        if words and all(w in subject for w in words[:2]):
            if best is None or len(comp) > len(_norm(apps[best]["company"])):
                best = apps.index(a)
    if best is not None:
        return apps[best]["id"], "subject line"
    return None


def classify(msg):
    """What kind of employer message this is."""
    blob = "%s %s" % (msg.get("subject") or "", msg.get("preview") or "")
    if REJECTION.search(blob):
        return "rejection", "rejected"
    if OFFER.search(blob):
        return "offer", "offer"
    if INTERVIEW.search(blob):
        return "interview", "interviewing"
    return "email_received", None


def is_alert(msg):
    return bool(ALERT_SENDERS.search(
        "%s %s" % (msg.get("from_addr") or "", msg.get("from_name") or "")))


def process(con, messages):
    """Apply a batch of messages. Returns a summary for the caller to report."""
    tracked, alerts, unmatched = [], [], []
    for msg in messages:
        key = "mail_seen:%s" % msg.get("id")
        if jt.kv_get(con, key):
            continue
        jt.kv_set(con, key, msg.get("received") or "1")

        if is_alert(msg):
            alerts.append(msg)
            continue

        hit = match_application(con, msg)
        if not hit:
            # Only worth surfacing if it reads like job correspondence.
            blob = "%s %s" % (msg.get("subject") or "", msg.get("preview") or "")
            if JOB_RELATED.search(blob):
                unmatched.append(msg)
            continue

        app_id, how = hit
        kind, new_status = classify(msg)
        note = "%s (%s)" % (msg["subject"][:120], msg["from_addr"])
        jt.log_touch(con, app_id, kind=kind, note=note,
                     happened_on=(msg.get("received") or "")[:10] or None)
        before = jt._row(con, app_id)
        if jt.may_advance(before["status"], new_status):
            try:
                jt.set_fields(con, app_id, status=new_status)
            except jt.JobtrackError:
                pass
        elif new_status and new_status != before["status"]:
            log.info("#%s: not moving %s -> %s from mail", app_id,
                     before["status"], new_status)
            new_status = None
        row = jt._row(con, app_id)
        tracked.append({"app_id": app_id, "company": row["company"],
                        "kind": kind, "status": new_status, "how": how,
                        "subject": msg["subject"]})
    return {"tracked": tracked, "alerts": alerts, "unmatched": unmatched}


def render(summary):
    lines = []
    for t in summary["tracked"]:
        label = {"rejection": "REJECTED", "offer": "OFFER",
                 "interview": "INTERVIEW"}.get(t["kind"], "reply")
        lines.append("  #%d %s - %s" % (t["app_id"], t["company"], label))
        lines.append("      %s" % t["subject"][:90])
    if summary["alerts"]:
        lines.append("  %d job alert(s) received" % len(summary["alerts"]))
    for m in summary["unmatched"]:
        lines.append("  unmatched: %s" % m["subject"][:80])
        lines.append("      from %s - %sadd a contact to link it"
                     % (m["from_addr"], ""))
    return "\n".join(lines)
