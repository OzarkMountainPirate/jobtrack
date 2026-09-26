#!/usr/bin/env python3
"""Matrix bot over jobtrack.

Phase 1: command driven, no LLM, no external API. Every handler is a thin
wrapper over a jobtrack function. Phase 3 swaps the command parser for tool
calling against the same functions, so nothing below has to be rewritten.

Also runs the daily check on an internal asyncio schedule, which keeps this
to a single container with no host cron and nothing to clean up.
"""

import asyncio
import logging
import re

import aiohttp
import os
import shlex
import sys
from datetime import datetime, timedelta

from nio import AsyncClient, InviteMemberEvent, MatrixRoom, RoomMessageText

import jobtrack as jt
import sources
import mail
import mailingest
import profile as prof
import scoring
import gaps as gapmod
import enrich as enrichmod
import drafting

log = logging.getLogger("jobtrack")

HOMESERVER = os.environ["MATRIX_HOMESERVER"]
USER_ID = os.environ["MATRIX_USER_ID"]
TOKEN = os.environ["MATRIX_TOKEN"]
ROOM_ID = os.environ["MATRIX_ROOM"]
DEVICE_ID = os.environ.get("MATRIX_DEVICE_ID", "JOBTRACKBOT")
PREFIX = os.environ.get("BOT_PREFIX", "!")
STALE_DAYS = int(os.environ.get("STALE_DAYS", "7"))
CHECK_AT = os.environ.get("CHECK_AT", "06:30")
CHECK_DAYS = os.environ.get("CHECK_DAYS", "0,1,2,3,4")  # Mon=0
POLL_EVERY_MIN = int(os.environ.get("POLL_EVERY_MIN", "60"))
MAIL_ENABLED = os.environ.get("GRAPH_CLIENT_ID") and os.environ.get("GRAPH_TENANT_ID")
ALERT_SCORE = int(os.environ.get("ALERT_SCORE", "8"))

HELP = """jobtrack bot

{p}list [status]          your pipeline by default, or all / lead / applied / ...
{p}show <id>              detail and full timeline
{p}add <company> | <role> | key=value ...
{p}set <id> key=value ...
{p}touch <id> <kind> [note]      kind: {kinds}
{p}contact <id> <name> | key=value ...
{p}due [days]             actions due now, or within N days
{p}stale [days]           active and gone quiet
{p}check                  what the morning digest would say
{p}leads [n]              untriaged leads, best scored first
{p}leads good             only leads scoring 7 or better
{p}score                  score any leads not yet scored
{p}score all              re-score every lead, after the rules change
{p}poll                   run the source pollers now
{p}enrich [n]             fetch full postings for leads that came in as teasers
{p}watch                  list watches
{p}watch add <label> | key=value ...    role_re company_re location_re min_salary
{p}watch rm <id>
{p}mail                   check mail now for replies and alerts
{p}mailauth               sign in to Microsoft 365 (one time)
{p}draft <id> [kind] [max]   draft application text; kinds: letter essay why leaving email
{p}profile                your stored profile
{p}profile gaps           fields still needing your input
{p}help

keys for add/set: source url location work_mode salary_min salary_max
                  status applied_on next_action next_action_date in_days notes
keys for contact: title email phone notes

examples
  {p}add Acme Corp | Systems Administrator | status=applied source=greenhouse salary_min=70000 salary_max=90000
  {p}touch 3 email_sent followed up with HR
  {p}set 3 in_days=5 next_action=call if still quiet
  {p}set 3 status=ghosted
  {p}watch add school districts | role_re=(?i)(tech|network|system|comput) company_re=(?i)(school|district)
  {p}watch add Remote 75k+ | role_re=(?i)(sysadmin|help ?desk|network) min_salary=75000"""


INT_KEYS = {"salary_min", "salary_max", "in_days", "min_salary"}


def parse_kv(tokens):
    """key=value tokens into a dict. Bare words append to the previous value,
    so next_action=call if still quiet works without quoting."""
    out, last = {}, None
    for tok in tokens:
        if "=" in tok and not tok.startswith("="):
            k, v = tok.split("=", 1)
            k = k.strip().lower().replace("-", "_")
            out[k] = v
            last = k
        elif last:
            out[last] += " " + tok
        else:
            raise jt.JobtrackError("expected key=value, got: %s" % tok)
    for k in list(out):
        if k in INT_KEYS:
            try:
                out[k] = int(str(out[k]).replace(",", "").replace("$", ""))
            except ValueError:
                raise jt.JobtrackError("%s must be a number" % k)
    return out


def cmd_list(con, args):
    rows = jt.list_applications(con, args.strip() or "pipeline")
    if not rows:
        return "nothing with that status"
    return jt.table(rows) + "\n\n%d application(s)" % len(rows)


def cmd_show(con, args):
    return jt.render_detail(jt.get_application(con, int(args.strip())))


def cmd_add(con, args):
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise jt.JobtrackError("need: add <company> | <role> | key=value ...")
    kw = parse_kv(shlex.split(parts[2])) if len(parts) > 2 and parts[2] else {}
    r = jt.add_application(con, parts[0], parts[1], **kw)
    return "added #%d  %s / %s  [%s]" % (r["id"], r["company"], r["role"], r["status"])


def cmd_set(con, args):
    toks = args.split()
    if not toks:
        raise jt.JobtrackError("need: set <id> key=value ...")
    app_id = int(toks[0])
    kw = parse_kv(toks[1:])
    clear = kw.pop("clear_next", None) is not None
    r = jt.set_fields(con, app_id, clear_next=clear, **kw)
    return "updated #%d  %s  [%s]  next: %s %s" % (
        r["id"], r["company"], r["status"],
        r["next_action"] or "none", r["next_action_date"] or "")


def cmd_touch(con, args):
    toks = args.split(None, 2)
    if len(toks) < 2:
        raise jt.JobtrackError("need: touch <id> <kind> [note]")
    r = jt.log_touch(con, int(toks[0]), kind=toks[1],
                     note=toks[2] if len(toks) > 2 else None)
    return "logged %s on #%d  %s" % (toks[1], r["id"], r["company"])


def cmd_contact(con, args):
    parts = [p.strip() for p in args.split("|")]
    toks = parts[0].split(None, 1)
    if len(toks) < 2:
        raise jt.JobtrackError("need: contact <id> <name> | key=value ...")
    kw = parse_kv(shlex.split(parts[1])) if len(parts) > 1 and parts[1] else {}
    jt.add_contact(con, int(toks[0]), toks[1], **kw)
    return "contact added to #%s" % toks[0]


def cmd_due(con, args):
    rows = jt.due(con, int(args.strip() or 0))
    return jt.table(rows) if rows else "nothing due"


def cmd_stale(con, args):
    rows = jt.stale(con, int(args.strip() or STALE_DAYS))
    return jt.table(rows) if rows else "nothing stale"


def cmd_check(con, args):
    res = jt.check(con, STALE_DAYS)
    if not res["due"] and not res["stale"]:
        return "nothing needs attention"
    return jt.render_check(res, STALE_DAYS)


def cmd_leads(con, args):
    args = args.strip().lower()
    if args in ("good", "best", "top"):
        rows = jt.leads(con, 30, min_score=7)
        if not rows:
            return "nothing scoring 7 or better yet"
    else:
        rows = jt.leads(con, int(args or 30))
    if not rows:
        return "no untriaged leads"
    return (jt.render_leads(rows)
            + "\n\n%d lead(s). %sset <id> status=applied once you act on one."
            % (len(rows), PREFIX))


def cmd_draft(con, args):
    toks = args.split()
    if not toks:
        return ("need: %sdraft <id> [letter|essay|why|leaving|email] [maxchars]"
                % PREFIX)
    row = jt.get_application(con, int(toks[0]))
    kind = "letter"
    limit = None
    for tok in toks[1:]:
        if tok.isdigit():
            limit = int(tok)
        elif tok.lower() in drafting.KINDS:
            kind = tok.lower()
        else:
            return "unknown option %r. kinds: %s" % (tok, ", ".join(drafting.KINDS))
    try:
        text, usage = drafting.draft(row, kind=kind, limit=limit)
    except Exception as e:
        return "draft failed: %s" % str(e)[:200]
    head = "%s for #%d %s - %s" % (kind, row["id"], row["company"], row["role"])
    foot = "%d chars | %d in (%d cached), %d out | ~$%.4f" % (
        len(text), usage["input"], usage["cache_read"], usage["output"],
        usage["cost"])
    return "%s\n\n%s\n\n%s" % (head, text, foot)


def cmd_profile(con, args):
    p = prof.load()
    if not p:
        return "no profile stored at %s" % prof.path()
    if args.strip().lower().startswith("gap"):
        g = prof.gaps(p)
        if not g:
            return "profile is complete - nothing marked for review"
        out = ["%d field(s) need your input:" % len(g), ""]
        out += ["  %s" % f for f in g]
        out.append("")
        out.append("Edit %s on the host, then %sprofile gaps to re-check."
                   % (prof.path(), PREFIX))
        return "\n".join(out)
    return prof.summary(p)


def cmd_watch(con, args):
    args = args.strip()
    if not args or args.split()[0] == "list":
        rows = jt.list_watches(con)
        if not rows:
            return "no watches. add one with: %swatch add <label> | role_re=..." % PREFIX
        out = []
        for w in rows:
            bits = [f"{k.replace('_re','')}~{w[k]}" for k in
                    ("company_re", "role_re", "location_re") if w[k]]
            if w["min_salary"]:
                bits.append("min_salary>=%d" % w["min_salary"])
            out.append("#%d  %s\n      %s" % (w["id"], w["label"], "  ".join(bits)))
        return "\n".join(out)

    verb, _, rest = args.partition(" ")
    verb = verb.lower()
    if verb in ("rm", "remove", "del", "delete"):
        return "removed watch #%s" % jt.remove_watch(con, int(rest.strip()))
    if verb != "add":
        raise jt.JobtrackError("watch: use add, rm, or list")

    parts = [x.strip() for x in rest.split("|")]
    if len(parts) < 2 or not parts[0]:
        raise jt.JobtrackError("need: watch add <label> | key=value ...")
    kw = parse_kv(shlex.split(parts[1])) if parts[1] else {}
    r = jt.add_watch(con, parts[0], **kw)
    return "watch #%d added: %s" % (r["id"], r["label"])


COMMANDS = {
    "list": cmd_list, "ls": cmd_list,
    "show": cmd_show,
    "add": cmd_add,
    "set": cmd_set,
    "touch": cmd_touch,
    "contact": cmd_contact,
    "due": cmd_due,
    "stale": cmd_stale,
    "check": cmd_check,
    "leads": cmd_leads,
    "watch": cmd_watch,
    "profile": cmd_profile,
    "draft": cmd_draft,
}


RICH = {"leads", "show", "draft"}


def dispatch(body):
    body = body.strip()
    if not body.startswith(PREFIX):
        return None
    parts = body[len(PREFIX):].strip().split(None, 1)
    if not parts:
        return None
    name, args = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    if name in ("help", "h", "?"):
        return HELP.format(p=PREFIX, kinds=" ".join(jt.KINDS)), True
    fn = COMMANDS.get(name)
    if not fn:
        return "unknown command '%s'. try %shelp" % (name, PREFIX), True
    code = name not in RICH
    con = jt.connect()
    try:
        return fn(con, args), code
    except jt.JobtrackError as e:
        return "error: %s" % e, True
    except (ValueError, IndexError):
        return "could not parse that. try %shelp" % PREFIX, True
    except Exception:
        log.exception("handler blew up on: %s", body)
        return "something broke, check the container logs", True
    finally:
        con.close()


URL_RE = re.compile(r"(https?://[^\s<]+)")
# Category names AppliTrack uses. Adjust for the field you are in.
IT_CATEGORY_RE = r"(?i)(technolog|computer|information system|network|\\bit\\b)"
ROLE_RE = re.compile(sources.DEFAULT_ROLE_FILTER)


def _html(text, code=True):
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if code:
        return "<pre><code>%s</code></pre>" % esc
    # Links have to be clickable for triage to be one click instead of a
    # copy-paste, and Element does not linkify inside <pre>.
    linked = URL_RE.sub(r'<a href="\1">\1</a>', esc)
    return linked.replace("\n", "<br/>")


async def send(client, text, code=True):
    await client.room_send(
        room_id=ROOM_ID,
        message_type="m.room.message",
        content={
            "msgtype": "m.text",
            "body": text,
            "format": "org.matrix.custom.html",
            "formatted_body": _html(text, code),
        },
    )


def seconds_until_next_check():
    hh, mm = (int(x) for x in CHECK_AT.split(":"))
    wanted = {int(d) for d in CHECK_DAYS.split(",") if d.strip() != ""}
    now = datetime.now()
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in wanted:
            return (candidate - now).total_seconds()
        candidate += timedelta(days=1)
    return 24 * 3600


def postings_from_change(change):
    """The added lines of a page change that read like a job posting.

    changedetection tells us a page moved, not what opened. The added lines
    are the part that is new, so anything in them matching the role filter is
    treated as a posting and ingested like any other lead - which is what puts
    it through scoring, gap analysis, watches and the usual notification.

    Each posting keeps only the lines beneath its own title. Handing every one
    of them the whole diff would feed another job's requirements to the gap
    analysis.
    """
    added = change.get("_added") or []
    marks = []
    for i, line in enumerate(added):
        title = line.strip(" \t*-\u2022")
        if sources.looks_like_title(title) and (ROLE_RE.search(title)
                                        or re.search(IT_CATEGORY_RE, title)):
            marks.append((i, title))

    seen, out = set(), []
    for n, (i, title) in enumerate(marks):
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        end = marks[n + 1][0] if n + 1 < len(marks) else len(added)
        out.append({
            "company": change.get("company"),
            "role": title,
            "location": change.get("location"),
            "url": change.get("url"),
            "source": "changedetection",
            "work_mode": change.get("work_mode"),
            "description": " ".join(added[i:end])[:6000] or None,
            "_expanded": True,
        })
    return out[:5]


async def run_poll(client, announce_empty=False):
    """Poll every source, ingest new leads, ping immediately on watch hits."""
    con = jt.connect()
    try:
        found, errors = await sources.poll_all()
        watches = jt.list_watches(con)
        new, alerts, page_changes = 0, [], []
        queue = list(found)
        while queue:
            lead = queue.pop(0)
            # changedetection reports "this page changed", not a posting.
            # What actually changed gets expanded into postings and re-queued;
            # the bookkeeping stops the same change re-firing tomorrow.
            if lead.get("source") == "changedetection" and not lead.get("_expanded"):
                key = "cd_seen:%s" % lead.get("_uuid")
                if jt.kv_get(con, key) == str(lead.get("_changed_at")):
                    continue
                jt.kv_set(con, key, lead.get("_changed_at"))
                postings = postings_from_change(lead)
                if not postings:
                    # Real change, nothing that reads as a tech job. Worth a
                    # line, not worth reading the page.
                    page_changes.append(lead)
                    continue
                queue.extend(postings)
                continue
            row, is_new = jt.ingest_lead(con, **{
                k: v for k, v in lead.items() if not k.startswith("_")})
            if not is_new:
                continue
            new += 1
            # One alert per posting, not per matching watch - overlapping watches
            # (a district in a watched county) would otherwise ping twice.
            hits = jt.match_watches(con, lead, watches)
            if hits:
                alerts.append((hits, row))
    finally:
        con.close()

    for c in page_changes:
        # A page moved and nothing on it reads as a job you want. An alert
        # you check and find nothing in is an alert you learn to ignore.
        log.info("page changed with nothing tech: %s (%s)",
                 c.get("company"), c.get("url"))

    for labels, row in alerts:
        await send(client, "WATCH HIT - %s\n\n%s" % (
            ", ".join(labels), jt.render_leads([row])), code=False)

    if errors:
        log.warning("poll errors: %s", "; ".join(errors))
    log.info("poll: %d seen, %d new, %d alerts, %d page changes",
             len(found), new, len(alerts), len(page_changes))
    if announce_empty or new or page_changes:
        return "poll: %d seen, %d new, %d watch hit(s), %d quiet page change(s)%s" % (
            len(found), new, len(alerts), len(page_changes),
            ("\nerrors: " + "; ".join(errors)) if errors else "")
    return None


async def run_scoring(client, announce_empty=False, force=False):
    """Score anything new. High scorers are announced; the rest just sort."""
    con = jt.connect()
    try:
        pending = jt.unscored(con, limit=500 if force else 40, force=force)
        if not pending:
            return "nothing new to score" if announce_empty else None
        scored, failed, notable = 0, 0, []
        stored = prof.load()
        try:
            criteria = scoring.Criteria(stored)
        except RuntimeError as e:
            return "cannot score: %s" % e
        # Computed once: an employer seen paying the target anywhere lifts its
        # junior roles, so this has to reflect every row, not just this batch.
        ladder = jt.employers_paying(con, criteria.target_min) \
            if criteria.target_min else set()
        async with aiohttp.ClientSession() as session:
            if not await scoring.health(session):
                return "scoring model unreachable at %s" % scoring.OLLAMA_URL
            for lead in pending:
                try:
                    s, why = await scoring.score_lead(
                        session, lead, criteria, ladder=ladder)
                except Exception as e:
                    failed += 1
                    log.warning("scoring failed for #%s: %s", lead["id"], e)
                    continue
                # Extraction needs the posting text, which only some sources
                # carry, but the title is always there and the analysis reads
                # it too - so this runs either way.
                desc = lead.get("description")
                try:
                    found = await gapmod.extract(session, desc) if desc else {}
                    g, b = gapmod.analyse(found, prof.load() or {}, desc,
                                          role=lead.get("role"))
                    jt.set_gaps(con, lead["id"], ", ".join(g), ", ".join(b))
                    adj = gapmod.penalty(b)
                    if adj:
                        s = max(0, min(10, s + adj))
                        why = "%s (blocked: %s)" % (why, ", ".join(b))
                except Exception as e:
                    log.warning("gap analysis failed for #%s: %s",
                                lead["id"], e)
                jt.set_score(con, lead["id"], s, why)
                scored += 1
                if s >= ALERT_SCORE:
                    notable.append(jt._row(con, lead["id"]))
        log.info("scored %d, %d failed, %d notable", scored, failed, len(notable))
    finally:
        con.close()

    if notable:
        await send(client, "Worth a look\n\n" + jt.render_leads(notable), code=False)
    if announce_empty or scored:
        return "scored %d lead(s)%s" % (scored, ", %d failed" % failed if failed else "")
    return None


async def run_mail(client, announce_empty=False):
    """Read recent mail, log employer replies, surface alerts."""
    if not MAIL_ENABLED:
        return "mail is not configured (GRAPH_CLIENT_ID / GRAPH_TENANT_ID)"
    con = jt.connect()
    try:
        async with aiohttp.ClientSession() as session:
            msgs = await mail.fetch_messages(session, top=50)
        summary = mailingest.process(con, msgs)
    except mail.MailError as e:
        return "mail error: %s" % e
    finally:
        con.close()

    body = mailingest.render(summary)
    log.info("mail: %d tracked, %d alerts, %d unmatched",
             len(summary["tracked"]), len(summary["alerts"]),
             len(summary["unmatched"]))
    if summary["tracked"]:
        await send(client, "Mail update\n\n" + body, code=False)
        return None
    if announce_empty:
        return body or "no new mail to act on"
    return None


async def run_mailauth(client):
    """Device code sign-in, driven from the room."""
    if not MAIL_ENABLED:
        return "set GRAPH_CLIENT_ID and GRAPH_TENANT_ID in .env first"
    try:
        async with aiohttp.ClientSession() as session:
            code = await mail.start_device_code(session)
            await send(client, "Go to %s and enter code:  %s\n\nWaiting..." % (
                code.get("verification_uri"), code.get("user_code")), code=False)
            await asyncio.to_thread(lambda: None)
            await mail.poll_device_code(session, code["device_code"],
                                        interval=int(code.get("interval", 5)))
        return "signed in - mail is now being checked each poll"
    except mail.MailError as e:
        return "sign-in failed: %s" % e


async def poller(client):
    while True:
        try:
            await run_poll(client)
        except Exception:
            log.exception("poll cycle failed")
        try:
            await run_scoring(client)
        except Exception:
            log.exception("scoring cycle failed")
        if MAIL_ENABLED:
            try:
                await run_mail(client)
            except Exception:
                log.exception("mail cycle failed")
        await asyncio.sleep(POLL_EVERY_MIN * 60)


async def scheduler(client):
    while True:
        await asyncio.sleep(seconds_until_next_check())
        try:
            con = jt.connect()
            res = jt.check(con, STALE_DAYS)
            con.close()
            if res["due"] or res["stale"]:
                await send(client, "Job search check\n\n" + jt.render_check(res, STALE_DAYS))
            else:
                log.info("daily check clear, staying quiet")
        except Exception:
            log.exception("scheduled check failed")


async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    client = AsyncClient(HOMESERVER, USER_ID)
    client.access_token = TOKEN
    client.device_id = DEVICE_ID
    client.user_id = USER_ID

    started = datetime.now().timestamp() * 1000

    async def on_message(room: MatrixRoom, event: RoomMessageText):
        if room.room_id != ROOM_ID or event.sender == USER_ID:
            return
        if event.server_timestamp < started:   # ignore backlog on restart
            return
        body = event.body.strip()
        low = body.lower().strip()
        if low in (PREFIX + "poll", PREFIX + "poll "):
            await send(client, "polling...")
            await send(client, await run_poll(client, announce_empty=True))
            return
        if low == PREFIX + "enrich" or low.startswith(PREFIX + "enrich "):
            rest = body[len(PREFIX) + len("enrich"):].strip()
            try:
                count = int(rest) if rest else None
            except ValueError:
                await send(client, "usage: %senrich [how many]" % PREFIX)
                return
            await send(client, "fetching postings...")
            await send(client, await enrichmod.run(count))
            return
        if low in (PREFIX + "score", PREFIX + "score all", PREFIX + "rescore"):
            again = low != PREFIX + "score"
            await send(client, "re-scoring everything..." if again else "scoring...")
            reply = await run_scoring(client, announce_empty=True, force=again)
            if reply:
                await send(client, reply)
            return
        if low == PREFIX + "mail":
            await send(client, "checking mail...")
            reply = await run_mail(client, announce_empty=True)
            if reply:
                await send(client, reply)
            return
        if low == PREFIX + "mailauth":
            reply = await run_mailauth(client)
            if reply:
                await send(client, reply)
            return
        result = dispatch(body)
        if result and result[0]:
            await send(client, result[0], code=result[1])

    async def on_invite(room: MatrixRoom, event: InviteMemberEvent):
        if room.room_id == ROOM_ID:
            await client.join(room.room_id)

    client.add_event_callback(on_message, RoomMessageText)
    client.add_event_callback(on_invite, InviteMemberEvent)

    jt.connect().close()          # create the db and schema on boot
    asyncio.create_task(scheduler(client))
    if POLL_EVERY_MIN > 0:
        asyncio.create_task(poller(client))
    log.info("listening in %s as %s", ROOM_ID, USER_ID)
    await client.sync_forever(timeout=30000, full_state=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
