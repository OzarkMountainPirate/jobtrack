#!/usr/bin/env python3
"""Fetch the full posting for leads whose board only published a teaser.

Some boards return a listing row and little else. Paylocity truncates its
Description field to 110 characters; HireTrue caps descriptionPreview near
340. A teaser is not enough to analyse gaps against or to read pay out of,
which is how a database ends up with a description on every lead and a salary
on none of them.

Both publish the whole posting one request away. This fetches it, stores it,
and takes the pay from it where the posting states one.
"""

import asyncio
import logging
import os
import re

import aiohttp

import jobtrack as jt
import sources as src

log = logging.getLogger("jobtrack.enrich")

PAYLOCITY_ID = re.compile(r"/recruiting/jobs/Details/(\d+)")
HIRETRUE_ID = re.compile(r"/job/([0-9a-fA-F][0-9a-fA-F-]{30,40})")

HT_LIST = ("https://%s/hiretrue/api/ce3/job-board/requisitions"
           "?jobBoardPrimaryKey=%s")
HT_DETAIL = "https://%s/hiretrue/api/ce3/job-board/requisitions/%s"

# The detail page is one template, so its chrome is the same every time: a
# breadcrumb and a JavaScript notice ahead of the posting, an apply footer
# after it.
PAY_MARK = " Description "
PAY_TAIL = re.compile(r"\s+Apply\s+View All Jobs\s+Powered by.*$", re.S)

# The conservative pay reader lives in sources, beside parse_salary, because
# the board adapters need it too.
pay_from = src.pay_from


def ht_pay(detail):
    """HireTrue states pay in its own field, with the period beside it."""
    sal = (detail.get("salary") or "").strip()
    if not sal:
        return None, None
    unit = {"perHour": " per hour", "perWeek": " per week",
            "perMonth": " per month", "perYear": ""}.get(detail.get("salaryType"), "")
    return src.parse_salary(sal + unit)


async def paylocity(session, url):
    """The Paylocity detail page is server rendered, unlike its listing."""
    m = PAYLOCITY_ID.search(url or "")
    if not m:
        return None, (None, None)
    page = "https://recruiting.paylocity.com/recruiting/jobs/Details/%s" % m.group(1)
    async with session.get(page, headers={"User-Agent": src.UA},
                           timeout=src.TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("HTTP %s from paylocity detail" % r.status)
        body = await src._text(r)

    text = src._plain(body, limit=40000) or ""
    text = PAY_TAIL.sub("", text)
    cut = text.find(PAY_MARK)
    if cut != -1:
        text = text[cut + len(PAY_MARK):]
    text = text.strip()[:6000]
    return (text or None), pay_from(text)


async def hiretrue_index(session, host, board_key):
    """externalId -> primaryKey.

    The stored URL carries the external id because that is what the public job
    page uses; the detail endpoint only answers to the primary key.
    """
    data = await src._get_json(session, HT_LIST % (host, board_key))
    return {str(j.get("externalId")): j.get("primaryKey")
            for j in (data if isinstance(data, list) else [])
            if j.get("externalId")}


async def hiretrue(session, url, index):
    m = HIRETRUE_ID.search(url or "")
    if not m:
        return None, (None, None)
    key = index.get(m.group(1))
    if not key:
        # Off the board since the poll that found it - closed or filled.
        return None, (None, None)
    host = url.split("/")[2]
    d = await src._get_json(session, HT_DETAIL % (host, key))
    body = " ".join(str(d.get(k) or "") for k in sorted(d)
                    if k.startswith("description"))
    text = src._plain(body)
    # The structured salary field is usually empty even when the posting
    # states pay in its own first paragraph, so the text is the fallback.
    lo, hi = ht_pay(d)
    if lo is None:
        lo, hi = pay_from(text)
    return text, (lo, hi)


def pending(con, limit=None):
    rows = con.execute(
        "SELECT id, source, url FROM applications"
        " WHERE status = 'lead' AND url IS NOT NULL"
        "   AND (description IS NULL OR description = '')"
        " ORDER BY COALESCE(score, -1) DESC, id").fetchall()
    rows = [r for r in rows if (r["source"] or "") in HANDLERS]
    return rows[:int(limit)] if limit else rows


async def run(limit=None, rescore=True):
    """Fill in descriptions and pay. Returns a one line summary."""
    con = jt.connect()
    try:
        rows = pending(con, limit)
        if not rows:
            return "nothing to enrich"

        filled = priced = failed = 0
        index = None
        async with aiohttp.ClientSession() as session:
            for row in rows:
                try:
                    if row["source"] == "hiretrue":
                        if index is None:
                            index = await hiretrue_index(
                                session, row["url"].split("/")[2],
                                os.environ.get("HIRETRUE_BOARD_KEY", "417"))
                        desc, (lo, hi) = await hiretrue(session, row["url"], index)
                    else:
                        desc, (lo, hi) = await paylocity(session, row["url"])
                except Exception as e:
                    log.warning("enrich #%s failed: %s", row["id"], e)
                    failed += 1
                    continue

                if not desc:
                    continue
                fields = {"description": desc}
                if lo:
                    fields["salary_min"] = lo
                    if hi:
                        fields["salary_max"] = hi
                    priced += 1
                jt.set_fields(con, row["id"], **fields)
                if rescore:
                    # The old score judged a job title with nothing behind it.
                    # Clearing it puts the lead back in front of !score, which
                    # now has a posting to read and can run the gap analysis.
                    con.execute("UPDATE applications SET score = NULL,"
                                " score_reason = NULL WHERE id = ?", (row["id"],))
                    con.commit()
                filled += 1
                await asyncio.sleep(0.3)   # these are small boards

        log.info("enrich: %d filled, %d priced, %d failed", filled, priced, failed)
        out = "enriched %d lead(s), %d with pay" % (filled, priced)
        if failed:
            out += ", %d failed" % failed
        if filled and rescore:
            out += " - run !score to re-rank them"
        return out
    finally:
        con.close()


HANDLERS = {"paylocity": paylocity, "hiretrue": hiretrue}


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(asyncio.run(run(n)))
