#!/usr/bin/env python3
"""jobtrack - SQLite job/prospect tracker.

Every operation is a plain function taking a connection and keyword args.
The CLI wraps them. The Matrix bot wraps them. In phase 3 the LLM tool
definitions wrap the same functions, so there is exactly one implementation
of "add an application" no matter who is asking.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

ACTIVE = ("lead", "applied", "screening", "interviewing", "offer")
# Statuses you are actually waiting on someone for. Leads are untriaged
# intake, so they stay out of the stale/digest path.
PIPELINE = ("applied", "screening", "interviewing", "offer")
TERMINAL = ("rejected", "ghosted", "withdrawn", "accepted", "closed")
STATUSES = ACTIVE + TERMINAL

# The pipeline has a direction. Automation may move an application along it or
# end it, but never walk one backwards. A signed offer letter arriving two
# days after an acceptance was recorded by hand once demoted the row from
# accepted back to offer: the automation overwrote a decision with a
# notification.
RANK = {"lead": 0, "applied": 1, "screening": 2, "interviewing": 3, "offer": 4,
        "accepted": 5}
ENDINGS = ("rejected", "ghosted", "withdrawn", "closed")


def may_advance(current, new):
    """Whether automation is allowed to move `current` to `new`.

    Hand-set statuses are decisions; a later email is only ever news. News can
    carry an application forward, or close it, and nothing else.
    """
    if not new or new == current:
        return False
    if current == "accepted":
        return False
    if new in ENDINGS:
        return True
    return RANK.get(new, -1) > RANK.get(current, -1)

KINDS = ("applied", "email_sent", "email_received", "call", "voicemail",
         "interview", "offer", "rejection", "followup", "note")

WORK_MODES = ("onsite", "hybrid", "remote")

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY,
    company          TEXT NOT NULL,
    role             TEXT NOT NULL,
    source           TEXT,
    url              TEXT,
    location         TEXT,
    work_mode        TEXT,
    salary_min       INTEGER,
    salary_max       INTEGER,
    status           TEXT NOT NULL DEFAULT 'lead',
    applied_on       TEXT,
    next_action      TEXT,
    next_action_date TEXT,
    notes            TEXT,
    score            INTEGER,
    score_reason     TEXT,
    scored_at        TEXT,
    description      TEXT,
    gaps             TEXT,
    blockers         TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_app_dedupe
    ON applications(lower(company), lower(role), lower(coalesce(location, '')));
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    happened_on    TEXT NOT NULL DEFAULT (date('now')),
    kind           TEXT NOT NULL,
    note           TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id, happened_on DESC);

CREATE TABLE IF NOT EXISTS contacts (
    id             INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    title          TEXT,
    email          TEXT,
    phone          TEXT,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_app ON contacts(application_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    company_re  TEXT,
    role_re     TEXT,
    location_re TEXT,
    min_salary  INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIEW IF NOT EXISTS app_view AS
SELECT a.*,
    (SELECT max(happened_on) FROM events e WHERE e.application_id = a.id) AS last_touch,
    CAST(julianday('now') - julianday(coalesce(
        (SELECT max(happened_on) FROM events e WHERE e.application_id = a.id),
        a.applied_on, date(a.created_at))) AS INTEGER) AS days_quiet
FROM applications a;
"""


class JobtrackError(Exception):
    """Anything the caller did wrong. The CLI prints it, the bot replies with it."""


# ------------------------------------------------------------------ plumbing

def db_path():
    p = os.environ.get("JOBTRACK_DB")
    if not p:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        p = os.path.join(base, "jobtrack", "jobtrack.db")
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    return p


def connect(path=None):
    con = sqlite3.connect(path or db_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """Add columns to databases created before they existed."""
    have = {r["name"] for r in con.execute("PRAGMA table_info(applications)")}
    for col, decl in (("score", "INTEGER"), ("score_reason", "TEXT"),
                      ("scored_at", "TEXT"), ("description", "TEXT"),
                      ("gaps", "TEXT"), ("blockers", "TEXT")):
        if col not in have:
            con.execute("ALTER TABLE applications ADD COLUMN %s %s" % (col, decl))
    con.commit()


def today():
    return date.today().isoformat()


def next_business_day(d):
    """Roll a Saturday or Sunday forward to Monday.

    Follow-ups are phone calls and emails to employers, so a due date on a
    weekend is just a date he will ignore until Monday.
    """
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _date(value, field):
    if value is None:
        return None
    if value in ("today", "now"):
        return today()
    if value in ("tomorrow",):
        return (date.today() + timedelta(days=1)).isoformat()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise JobtrackError("%s must be YYYY-MM-DD or today/tomorrow, got: %s" % (field, value))
    return value


def _choice(value, allowed, field):
    if value is not None and value not in allowed:
        raise JobtrackError("%s must be one of: %s" % (field, ", ".join(allowed)))
    return value


def _row(con, app_id):
    r = con.execute("SELECT * FROM app_view WHERE id = ?", (app_id,)).fetchone()
    if r is None:
        raise JobtrackError("no application with id %s" % app_id)
    return dict(r)


# ------------------------------------------------------------------ the API

def add_application(con, company, role, source=None, url=None, location=None,
                    work_mode=None, salary_min=None, salary_max=None,
                    status="lead", applied_on=None, next_action=None,
                    next_action_date=None, notes=None, description=None):
    _choice(status, STATUSES, "status")
    _choice(work_mode, WORK_MODES, "work_mode")
    applied_on = _date(applied_on, "applied_on")
    next_action_date = _date(next_action_date, "next_action_date")
    if status == "applied" and not applied_on:
        applied_on = today()

    fields = dict(company=company, role=role, source=source, url=url,
                  location=location, work_mode=work_mode, salary_min=salary_min,
                  salary_max=salary_max, status=status, applied_on=applied_on,
                  next_action=next_action, next_action_date=next_action_date,
                  notes=notes, description=description)
    keys = [k for k, v in fields.items() if v is not None]
    sql = "INSERT INTO applications (%s) VALUES (%s)" % (
        ", ".join(keys), ", ".join("?" * len(keys)))
    try:
        cur = con.execute(sql, [fields[k] for k in keys])
    except sqlite3.IntegrityError:
        raise JobtrackError("already tracked: %s / %s" % (company, role))
    app_id = cur.lastrowid
    if applied_on:
        con.execute("INSERT INTO events (application_id, happened_on, kind, note)"
                    " VALUES (?,?,?,?)",
                    (app_id, applied_on, "applied", "application submitted"))
    con.commit()
    return _row(con, app_id)


def set_fields(con, app_id, clear_next=False, **kw):
    before = _row(con, app_id)
    updates = {}
    for key in ("status", "next_action", "url", "location", "work_mode",
                "notes", "salary_min", "salary_max", "source", "company", "role",
                "description", "gaps", "blockers"):
        if kw.get(key) is not None:
            updates[key] = kw[key]
    _choice(updates.get("status"), STATUSES, "status")
    _choice(updates.get("work_mode"), WORK_MODES, "work_mode")
    if kw.get("next_action_date") is not None:
        updates["next_action_date"] = _date(kw["next_action_date"], "next_action_date")
    if kw.get("applied_on") is not None:
        updates["applied_on"] = _date(kw["applied_on"], "applied_on")
    if kw.get("in_days") is not None:
        updates["next_action_date"] = next_business_day(
            date.today() + timedelta(days=int(kw["in_days"]))).isoformat()
    if clear_next:
        updates["next_action"] = None
        updates["next_action_date"] = None
    if not updates:
        raise JobtrackError("nothing to change")

    # A terminal status has no next action by definition.
    if updates.get("status") in TERMINAL:
        updates.setdefault("next_action", None)
        updates.setdefault("next_action_date", None)

    sets = ", ".join("%s = ?" % k for k in updates) + ", updated_at = datetime('now')"
    con.execute("UPDATE applications SET %s WHERE id = ?" % sets,
                list(updates.values()) + [app_id])
    if "status" in updates and updates["status"] != before["status"]:
        con.execute("INSERT INTO events (application_id, kind, note) VALUES (?,?,?)",
                    (app_id, "note", "status %s -> %s" % (before["status"], updates["status"])))
    con.commit()
    return _row(con, app_id)


def log_touch(con, app_id, kind="note", note=None, happened_on=None,
              next_action=None, next_action_date=None, in_days=None, status=None):
    _row(con, app_id)
    _choice(kind, KINDS, "kind")
    con.execute("INSERT INTO events (application_id, happened_on, kind, note)"
                " VALUES (?,?,?,?)",
                (app_id, _date(happened_on, "happened_on") or today(), kind, note))
    con.commit()
    followup = {k: v for k, v in dict(
        next_action=next_action, next_action_date=next_action_date,
        in_days=in_days, status=status).items() if v is not None}
    if followup:
        return set_fields(con, app_id, **followup)
    con.execute("UPDATE applications SET updated_at = datetime('now') WHERE id = ?", (app_id,))
    con.commit()
    return _row(con, app_id)


def add_contact(con, app_id, name, title=None, email=None, phone=None, notes=None):
    _row(con, app_id)
    cur = con.execute("INSERT INTO contacts (application_id, name, title, email, phone, notes)"
                      " VALUES (?,?,?,?,?,?)", (app_id, name, title, email, phone, notes))
    con.commit()
    return {"id": cur.lastrowid, "application_id": app_id, "name": name}


def list_applications(con, status="pipeline"):
    order = (" ORDER BY next_action_date IS NULL, next_action_date,"
             " days_quiet DESC, id")
    if status == "all":
        rows = con.execute("SELECT * FROM app_view" + order).fetchall()
    elif status == "pipeline":
        # Things you are actually working, plus any lead with an action due.
        # Untriaged leads belong in !leads, not here.
        q = ",".join("?" * len(PIPELINE))
        rows = con.execute(
            "SELECT * FROM app_view WHERE status IN (%s)"
            " OR (status = 'lead' AND next_action_date IS NOT NULL)%s" % (q, order),
            PIPELINE).fetchall()
    elif status == "active":
        q = ",".join("?" * len(ACTIVE))
        rows = con.execute("SELECT * FROM app_view WHERE status IN (%s)%s" % (q, order),
                           ACTIVE).fetchall()
    else:
        _choice(status, STATUSES, "status")
        rows = con.execute("SELECT * FROM app_view WHERE status = ?%s" % order,
                           (status,)).fetchall()
    return [dict(r) for r in rows]


def get_application(con, app_id):
    row = _row(con, app_id)
    row["contacts"] = [dict(r) for r in con.execute(
        "SELECT * FROM contacts WHERE application_id = ? ORDER BY id", (app_id,))]
    row["events"] = [dict(r) for r in con.execute(
        "SELECT * FROM events WHERE application_id = ?"
        " ORDER BY happened_on DESC, id DESC", (app_id,))]
    return row


def due(con, days=0):
    cutoff = (date.today() + timedelta(days=int(days))).isoformat()
    q = ",".join("?" * len(ACTIVE))
    return [dict(r) for r in con.execute(
        "SELECT * FROM app_view WHERE status IN (%s)"
        " AND next_action_date IS NOT NULL AND next_action_date <= ?"
        " ORDER BY next_action_date" % q, list(ACTIVE) + [cutoff])]


def stale(con, days=7):
    q = ",".join("?" * len(PIPELINE))
    return [dict(r) for r in con.execute(
        "SELECT * FROM app_view WHERE status IN (%s) AND days_quiet >= ?"
        " AND (next_action_date IS NULL OR next_action_date <= date('now'))"
        " ORDER BY days_quiet DESC" % q, list(PIPELINE) + [int(days)])]


def check(con, days=7):
    """What needs attention. Returns {'due': [...], 'stale': [...]}."""
    d = due(con, 0)
    seen = {r["id"] for r in d}
    return {"due": d, "stale": [r for r in stale(con, days) if r["id"] not in seen]}


# ------------------------------------------------------- leads and watchlist

def ingest_lead(con, company, role, **kw):
    """Insert a polled lead. Returns (row, True) when new, (None, False) when
    already known. Pollers call this blindly and let the unique index dedupe.

    A duplicate still enriches the stored row with a posting description or a
    salary it did not have. That backfills rows captured before those fields
    existed, and picks up a salary an employer adds after posting.
    """
    kw.pop("status", None)
    kw.pop("posted", None)
    fields = {k: v for k, v in kw.items()
              if k in ("source", "url", "location", "work_mode",
                       "salary_min", "salary_max", "notes", "description")}
    try:
        return add_application(con, company, role, status="lead", **fields), True
    except JobtrackError:
        row = con.execute(
            "SELECT id, description, salary_min FROM applications"
            " WHERE lower(company) = lower(?) AND lower(role) = lower(?)"
            " AND lower(coalesce(location,'')) = lower(?)",
            (company, role, fields.get("location") or "")).fetchone()
        if row:
            add = {}
            if fields.get("description") and not row["description"]:
                add["description"] = fields["description"]
            if fields.get("salary_min") and not row["salary_min"]:
                add["salary_min"] = fields["salary_min"]
                add["salary_max"] = fields.get("salary_max")
            if add:
                sets = ", ".join("%s = ?" % k for k in add)
                con.execute("UPDATE applications SET %s WHERE id = ?" % sets,
                            list(add.values()) + [row["id"]])
                con.commit()
        return None, False


def add_watch(con, label, company_re=None, role_re=None, location_re=None,
              min_salary=None):
    for name, pattern in (("company_re", company_re), ("role_re", role_re),
                          ("location_re", location_re)):
        if pattern:
            try:
                re.compile(pattern, re.I)
            except re.error as e:
                raise JobtrackError("bad regex for %s: %s" % (name, e))
    if not any((company_re, role_re, location_re, min_salary)):
        raise JobtrackError("a watch needs at least one criterion")
    cur = con.execute(
        "INSERT INTO watchlist (label, company_re, role_re, location_re, min_salary)"
        " VALUES (?,?,?,?,?)",
        (label, company_re, role_re, location_re,
         int(min_salary) if min_salary else None))
    con.commit()
    return {"id": cur.lastrowid, "label": label}


def list_watches(con, only_active=True):
    sql = "SELECT * FROM watchlist"
    if only_active:
        sql += " WHERE active = 1"
    return [dict(r) for r in con.execute(sql + " ORDER BY id")]


def remove_watch(con, watch_id):
    cur = con.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,))
    con.commit()
    if not cur.rowcount:
        raise JobtrackError("no watch with id %s" % watch_id)
    return watch_id


def match_watches(con, lead, watches=None):
    """Labels of every active watch this lead satisfies. All set criteria must
    match - an unset criterion is ignored rather than treated as a wildcard."""
    hits = []
    for w in (list_watches(con) if watches is None else watches):
        for field, pattern in (("company", w["company_re"]),
                               ("role", w["role_re"]),
                               ("location", w["location_re"])):
            if not pattern:
                continue
            if not re.search(pattern, str(lead.get(field) or ""), re.I):
                break
        else:
            if w["min_salary"]:
                pay = lead.get("salary_max") or lead.get("salary_min")
                if pay is None or pay < w["min_salary"]:
                    continue
            hits.append(w["label"])
    return hits


def kv_get(con, key, default=None):
    r = con.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
    return r["v"] if r else default


def kv_set(con, key, value):
    con.execute("INSERT INTO kv (k, v) VALUES (?,?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v,"
                " updated_at = datetime('now')", (key, str(value)))
    con.commit()


def leads(con, limit=30, min_score=None):
    """Scored leads first, best first. Unscored fall to the bottom."""
    sql = ("SELECT * FROM app_view WHERE status = 'lead'"
           "%s ORDER BY score IS NULL, score DESC, created_at DESC, id DESC"
           " LIMIT ?")
    if min_score is not None:
        return [dict(r) for r in con.execute(
            sql % " AND score >= ?", (int(min_score), int(limit)))]
    return [dict(r) for r in con.execute(sql % "", (int(limit),))]


def unscored(con, limit=40, force=False):
    """Intake awaiting a score. Leads carrying a next_action are deliberate
    follow-ups rather than poller output, so they are left alone.

    `force` returns every lead instead, for when the rules themselves have
    changed. A score is a verdict under one set of rules, and a lead scored
    under an older set keeps that verdict forever otherwise - which is how a
    $93,809 systems administrator inside the drive radius sat at 7, scored
    before the salary target existed at all.
    """
    return [dict(r) for r in con.execute(
        "SELECT * FROM app_view WHERE status = 'lead'"
        "  AND (score IS NULL OR ?)"
        "  AND next_action_date IS NULL"
        " ORDER BY id DESC LIMIT ?", (1 if force else 0, int(limit)))]


def set_gaps(con, app_id, gaps, blockers):
    con.execute("UPDATE applications SET gaps = ?, blockers = ? WHERE id = ?",
                (gaps or None, blockers or None, app_id))
    con.commit()


def employers_paying(con, floor):
    """Companies with at least one posting at or above a pay level.

    A junior role at one of these is a way in rather than a dead end, which is
    the difference between a dead end and a way in: a lower rung is worth
    taking when the rung above it pays the target.
    """
    rows = con.execute(
        "SELECT DISTINCT lower(trim(company)) FROM applications"
        " WHERE company IS NOT NULL"
        "   AND COALESCE(salary_max, salary_min) >= ?", (int(floor),))
    return {r[0] for r in rows if r[0]}


def set_score(con, app_id, score, reason):
    con.execute("UPDATE applications SET score = ?, score_reason = ?,"
                " scored_at = datetime('now') WHERE id = ?",
                (int(score), reason, app_id))
    con.commit()


# ------------------------------------------------------------------ rendering

def money(lo, hi):
    if not lo and not hi:
        return ""
    f = lambda v: "%dk" % (v // 1000) if v and v >= 1000 else (str(v) if v else "?")
    return "%s-%s" % (f(lo), f(hi))


LIST_COLS = [
    ("ID", lambda r: r["id"]),
    ("COMPANY", lambda r: (r["company"] or "")[:28]),
    ("ROLE", lambda r: (r["role"] or "")[:30]),
    ("STATUS", lambda r: r["status"]),
    ("PAY", lambda r: money(r["salary_min"], r["salary_max"])),
    ("QUIET", lambda r: "%sd" % r["days_quiet"] if r["days_quiet"] is not None else ""),
    ("NEXT", lambda r: r["next_action_date"] or ""),
    ("ACTION", lambda r: (r["next_action"] or "")[:34]),
]


def table(rows, cols=LIST_COLS):
    if not rows:
        return ""
    widths = [len(h) for h, _ in cols]
    body = []
    for r in rows:
        line = ["" if f(r) is None else str(f(r)) for _, f in cols]
        body.append(line)
        for i, v in enumerate(line):
            widths[i] = max(widths[i], len(v))
    out = ["  ".join(h.ljust(widths[i]) for i, (h, _) in enumerate(cols)).rstrip(),
           "  ".join("-" * widths[i] for i in range(len(cols)))]
    for line in body:
        out.append("  ".join(line[i].ljust(widths[i]) for i in range(len(cols))).rstrip())
    return "\n".join(out)


def render_leads(rows):
    """Leads as scannable blocks. A table cannot hold a URL without wrapping,
    and triage needs the link, the pay and the work mode at a glance."""
    out = []
    for r in rows:
        pay = money(r["salary_min"], r["salary_max"]) or "pay not listed"
        facts = [x for x in (r.get("work_mode") or "mode unknown",
                             r.get("location"), pay) if x]
        head = "#%d  %s" % (r["id"], r["company"])
        if r.get("score") is not None:
            head = "[%2d/10] %s" % (r["score"], head)
        out.append(head)
        out.append("    %s" % r["role"])
        out.append("    %s" % "  |  ".join(facts))
        if r.get("source"):
            out.append("    via %s" % r["source"])
        if r.get("score_reason"):
            out.append("    %s" % r["score_reason"])
        if r.get("blockers"):
            out.append("    BLOCKERS: %s" % r["blockers"])
        if r.get("gaps"):
            out.append("    gaps: %s" % r["gaps"])
        if r.get("url"):
            out.append("    %s" % r["url"])
        out.append("")
    return "\n".join(out).rstrip()


def render_detail(row):
    out = ["#%d  %s" % (row["id"], row["company"]),
           "    role       %s" % row["role"],
           "    status     %s" % row["status"]]
    for label, key in (("location", "location"), ("mode", "work_mode"),
                       ("source", "source"), ("url", "url"), ("applied", "applied_on")):
        if row.get(key):
            out.append("    %-10s %s" % (label, row[key]))
    if row.get("salary_min") or row.get("salary_max"):
        out.append("    pay        %s" % money(row["salary_min"], row["salary_max"]))
    if row.get("next_action"):
        out.append("    next       %s  (%s)" % (row["next_action"],
                                                row.get("next_action_date") or "no date"))
    out.append("    quiet      %s day(s)" % row["days_quiet"])
    if row.get("notes"):
        out.append("    notes      %s" % row["notes"])
    if row.get("contacts"):
        out.append("")
        out.append("  contacts")
        for c in row["contacts"]:
            bits = [c["name"]] + [c[k] for k in ("title", "email", "phone") if c.get(k)]
            out.append("    " + "  |  ".join(bits))
    if row.get("events"):
        out.append("")
        out.append("  timeline")
        for e in row["events"]:
            out.append("    %s  %-16s %s" % (e["happened_on"], e["kind"], e["note"] or ""))
    return "\n".join(out)


def render_check(result, days=7):
    lines = []
    if result["due"]:
        lines.append("DUE TODAY")
        for r in result["due"]:
            lines.append("  #%d %s / %s -- %s (due %s)" % (
                r["id"], r["company"], r["role"],
                r["next_action"] or "follow up", r["next_action_date"]))
    if result["stale"]:
        if lines:
            lines.append("")
        lines.append("QUIET %d+ DAYS" % days)
        for r in result["stale"]:
            lines.append("  #%d %s / %s -- %s, no contact in %s days" % (
                r["id"], r["company"], r["role"], r["status"], r["days_quiet"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI

def _cli():
    p = argparse.ArgumentParser(prog="jobtrack")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("-c", "--company", required=True)
    a.add_argument("-r", "--role", required=True)
    a.add_argument("-s", "--source")
    a.add_argument("-u", "--url")
    a.add_argument("-l", "--location")
    a.add_argument("-m", "--work-mode", choices=WORK_MODES)
    a.add_argument("--salary-min", type=int)
    a.add_argument("--salary-max", type=int)
    a.add_argument("--status", choices=STATUSES, default="lead")
    a.add_argument("--applied", dest="applied_on")
    a.add_argument("--next", dest="next_action")
    a.add_argument("--next-date", dest="next_action_date")
    a.add_argument("-n", "--notes")

    s = sub.add_parser("set")
    s.add_argument("id", type=int)
    s.add_argument("--status", choices=STATUSES)
    s.add_argument("--next", dest="next_action")
    s.add_argument("--next-date", dest="next_action_date")
    s.add_argument("--in-days", type=int)
    s.add_argument("--clear-next", action="store_true")
    s.add_argument("--applied", dest="applied_on")
    s.add_argument("-u", "--url")
    s.add_argument("-l", "--location")
    s.add_argument("-m", "--work-mode", choices=WORK_MODES)
    s.add_argument("--salary-min", type=int)
    s.add_argument("--salary-max", type=int)
    s.add_argument("-n", "--notes")

    t = sub.add_parser("touch")
    t.add_argument("id", type=int)
    t.add_argument("-k", "--kind", choices=KINDS, default="note")
    t.add_argument("-n", "--note")
    t.add_argument("-d", "--date", dest="happened_on")
    t.add_argument("--next", dest="next_action")
    t.add_argument("--next-date", dest="next_action_date")
    t.add_argument("--in-days", type=int)
    t.add_argument("--status", choices=STATUSES)

    c = sub.add_parser("contact")
    c.add_argument("id", type=int)
    c.add_argument("name")
    c.add_argument("-t", "--title")
    c.add_argument("-e", "--email")
    c.add_argument("-p", "--phone")
    c.add_argument("-n", "--notes")

    ls = sub.add_parser("list")
    ls.add_argument("--status", default="pipeline")
    ls.add_argument("--json", action="store_true")

    sh = sub.add_parser("show")
    sh.add_argument("id", type=int)

    dd = sub.add_parser("due")
    dd.add_argument("--days", type=int, default=0)

    st = sub.add_parser("stale")
    st.add_argument("--days", type=int, default=7)

    ck = sub.add_parser("check")
    ck.add_argument("--days", type=int, default=7)
    ck.add_argument("-q", "--quiet", action="store_true")

    sub.add_parser("schema")
    return p


def main(argv=None):
    args = _cli().parse_args(argv)
    con = connect()
    kw = {k: v for k, v in vars(args).items() if k not in ("cmd", "json", "quiet")}
    try:
        if args.cmd == "add":
            r = add_application(con, **kw)
            print("added #%d  %s / %s" % (r["id"], r["company"], r["role"]))
        elif args.cmd == "set":
            r = set_fields(con, kw.pop("id"), **kw)
            print("updated #%d" % r["id"])
        elif args.cmd == "touch":
            r = log_touch(con, kw.pop("id"), **kw)
            print("logged %s on #%d" % (args.kind, r["id"]))
        elif args.cmd == "contact":
            add_contact(con, kw.pop("id"), **kw)
            print("contact added")
        elif args.cmd == "list":
            rows = list_applications(con, args.status)
            if args.json:
                print(json.dumps(rows, indent=2))
            elif rows:
                print(table(rows))
                print("\n%d application(s)" % len(rows))
            else:
                print("nothing with status '%s'" % args.status)
        elif args.cmd == "show":
            print(render_detail(get_application(con, args.id)))
        elif args.cmd == "due":
            rows = due(con, args.days)
            print(table(rows) if rows else "nothing due")
        elif args.cmd == "stale":
            rows = stale(con, args.days)
            print(table(rows) if rows else "nothing stale")
        elif args.cmd == "check":
            res = check(con, args.days)
            if not res["due"] and not res["stale"]:
                if not args.quiet:
                    print("job search: nothing needs attention")
                return 0
            print(render_check(res, args.days))
            return 1
        elif args.cmd == "schema":
            print(SCHEMA.strip())
    except JobtrackError as e:
        sys.exit(str(e))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
