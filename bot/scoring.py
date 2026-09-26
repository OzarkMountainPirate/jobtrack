#!/usr/bin/env python3
"""Lead scoring against your profile.

Split deliberately: the model judges *fit* - is this your kind of work, at a
sensible level - and code applies the *rules* - is it reachable, what does it
pay against what you earn now. A small local model is good at the first and
unreliable at the second, and geography should not be probabilistic.

Nothing about any particular person is compiled into this file. The ladder of
role families, the towns you would drive to, the fields you are moving into,
what you earn and what you are aiming for all come out of profile.json. If
this file mentions a job title, it is a universal convention - "senior",
"junior" - and not somebody's career.

Runs against Ollama. Nothing here calls a paid API; drafting is where that
becomes worth it.
"""

import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("jobtrack.scoring")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("SCORING_MODEL", "phi4-mini:latest")
TIMEOUT = aiohttp.ClientTimeout(total=120)

REMOTE = re.compile(r"(?i)\b(remote|anywhere|work from home|wfh|telecommut)\b")

# Title conventions, not anybody's career. Every employer uses these the same
# way, so they stay in code.
SENIOR = re.compile(
    r"(?i)(\bsenior\b|\bsr\.?\b|\bprincipal\b|\bstaff\b|\blead\b|"
    r"\bmanager\b|\bdirector\b|\bhead of\b|\barchitect\b|\bii+\b|\b[23]\b)")

JUNIOR = re.compile(
    r"(?i)(\bjunior\b|\bjr\.?\b|\bassociate\b|\bentry[- ]?level\b|"
    r"\bapprentice\b|\btrainee\b|\bi\b|\b1\b)")

# Guardrails that took several rounds of wrong answers to arrive at. They are
# about how models misread job postings in general, so they ship as they are.
RULES = """IF A TITLE IS NOT CLEARLY ON THIS LADDER, SCORE IT 4 AT MOST. Do not
give an unfamiliar title the benefit of the doubt because a well known company
posted it.

JUDGE THE ROLE, NOT THE EMPLOYER. Every employer also posts sales, finance,
customer service and operations jobs. Working at a company in the candidate's
industry does not make a job one they can do. If the day to day work is not
the work described on the ladder, it is not their field.

INTERNSHIPS AND ENTRY-LEVEL PROGRAMS score 3 or below whatever the field,
unless the ladder says otherwise.

SENIORITY. For work the candidate has actually done, any level is fine - score
the family, not the prestige. For a field they are moving into, seniority is
handled separately after you score, so judge the family here and ignore it."""


def _alternation(words):
    """A word-boundary regex over a list, or None when the list is empty."""
    terms = [re.escape(str(w).strip()) for w in (words or []) if str(w).strip()]
    if not terms:
        return None
    return re.compile(r"(?i)\b(%s)\b" % "|".join(terms))


def build_system(profile):
    """The scoring prompt, assembled from the profile.

    The ladder is the whole point: it is the candidate's own order of
    preference, and the model's job is to place a posting on it rather than to
    have opinions about careers.
    """
    profile = profile or {}
    search = profile.get("search") or {}
    ladder = search.get("ladder") or []
    if not ladder:
        raise RuntimeError("profile.json has no search.ladder - see the README")

    out = ['You score job postings for one candidate. Reply with JSON only:',
           '{"score": <0-10>, "reason": "<one short sentence>"}',
           "", "THE CANDIDATE", (search.get("summary") or "").strip(), "",
           "SCORE THE ROLE FAMILY. This is the candidate's own order of",
           "preference, most wanted first. Ignore location entirely - it is",
           "applied separately.", ""]

    for rung in sorted(ladder, key=lambda r: -int(r.get("score", 0))):
        text = " ".join(str(rung.get("families") or "").split())
        note = " ".join(str(rung.get("note") or "").split())
        out.append("%2d  %s%s" % (int(rung.get("score", 0)), text,
                                  (" " + note) if note else ""))

    if search.get("not_my_field"):
        out += ["", " 0  Not their field: %s"
                % " ".join(str(search["not_my_field"]).split())]

    out += ["", RULES]

    examples = search.get("examples") or {}
    if examples:
        out += ["", "EXAMPLES"]
        for title, score in examples.items():
            out.append('"%s" -> %s' % (title, score))
    return "\n".join(out)


class Criteria:
    """Everything the scorer needs, read from the profile once per run."""

    def __init__(self, profile):
        profile = profile or {}
        pref = profile.get("preferences") or {}
        search = profile.get("search") or {}
        floors = pref.get("salary_floor") or {}

        self.system = build_system(profile)
        self.commute = _alternation(pref.get("commute_towns"))
        self.pivot = _alternation(search.get("pivot_into"))
        self.target_min = _int(pref.get("target_salary", {}).get("min"), 0)
        self.baseline = _int(pref.get("current_annual"), 0)
        self.relocation_floor = _int(floors.get("relocation"), 0)
        self.remote_bonus = _int(search.get("remote_bonus"), 0)
        self.min_fit_for_remote = _int(search.get("remote_bonus_needs_fit"), 5)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def locality(lead, criteria):
    """reachable | remote | distant - decided in code, not by the model."""
    loc = str(lead.get("location") or "")
    mode = str(lead.get("work_mode") or "")
    if REMOTE.search(mode) or REMOTE.search(loc):
        return "remote"
    if criteria.commute and criteria.commute.search(loc):
        return "reachable"
    return "distant"


def realism(role, criteria):
    """How plausible an application is, given what the candidate has held.

    Returns (adjustment, note). Fields they have worked in are unaffected -
    they can apply at any level. A field they are moving into is only a
    realistic application at the entry rung, however well the work fits.
    """
    title = role or ""
    if not criteria.pivot or not criteria.pivot.search(title):
        return 0, None
    if JUNIOR.search(title):
        return 1, "entry rung of a move across - realistic"
    if SENIOR.search(title):
        return -5, "senior title in a field they have not held"
    return -2, "field they are moving into, no seniority stated"


def pay_delta(pay, target_min, baseline):
    """What a stated salary does to a lead, measured against both ends.

    The target is what you want; the baseline is what you earn now. Only the
    second one can push a lead down, and only when the job would not actually
    be a raise. A target is an aspiration, not a filter - set a floor in
    salary_floor if you want a hard one.
    """
    if not pay:
        return 0, None
    if target_min and pay >= target_min:
        return 2, "pays the target"
    if target_min and pay >= target_min - 10000:
        return 1, "close to the target"
    if not baseline:
        return 0, None
    if pay >= baseline + 10000:
        return 0, "well above current pay"
    if pay >= baseline:
        return -1, "barely above current pay"
    return -2, "less than current pay"


def apply_rules(fit, lead, where, criteria, ladder=None):
    """Your constraints, applied deterministically.

    Remote is a positive, not merely the absence of a penalty, if you set
    search.remote_bonus - but it is gated on fit, because remote does not
    rescue a job outside your field, it amplifies one inside it.

    Pay moves a lead up three ways: the job pays the target, its range reaches
    it, or the employer pays it in a role above this one. `ladder` is the set
    of employers already seen posting at or above the target, which both adds
    a point and stops the relocation floor burying a way in.
    """
    score, notes = fit, []
    pay = lead.get("salary_max") or lead.get("salary_min")
    bottom = lead.get("salary_min") or lead.get("salary_max")
    company = (lead.get("company") or "").strip().lower()
    way_in = bool(ladder and company and company in ladder)

    adj, note = pay_delta(pay, criteria.target_min, criteria.baseline)
    if pay and criteria.target_min and pay >= criteria.target_min \
            and (bottom or 0) < criteria.target_min:
        note = "range reaches the target"
    score += adj
    if note:
        notes.append(note)
    if not pay and way_in:
        score += 1
        notes.append("employer posts at the target higher up")
    elif pay and criteria.target_min and pay < criteria.target_min and way_in:
        notes.append("under the target, but a way in at an employer that pays it")

    if where == "remote":
        if criteria.remote_bonus:
            if fit >= criteria.min_fit_for_remote:
                score += criteria.remote_bonus
                notes.append("remote")
            else:
                notes.append("remote, but not their field")
        else:
            notes.append("remote")
    elif where == "distant":
        score -= 4
        notes.append("needs relocation")
        # Moving costs money, so relocation is the one case that still carries
        # a floor of its own.
        if pay and criteria.relocation_floor \
                and pay < criteria.relocation_floor and not way_in:
            score -= 2
            notes.append("below the relocation floor")
    else:
        notes.append("within driving distance")

    return max(0, min(10, score)), notes


async def score_lead(session, lead, criteria, ladder=None):
    """Returns (score, reason). Raises on transport failure so callers can log."""
    prompt = "Title: %s\nCompany: %s\nLocation: %s\nWork mode: %s" % (
        lead.get("role"), lead.get("company"),
        lead.get("location") or "not stated", lead.get("work_mode") or "not stated")
    body = {"model": MODEL, "format": "json", "stream": False,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": criteria.system},
                         {"role": "user", "content": prompt}]}
    async with session.post("%s/api/chat" % OLLAMA_URL, json=body,
                            timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("ollama %s" % r.status)
        data = json.loads(await r.text())

    try:
        verdict = json.loads(data["message"]["content"])
        fit = int(verdict.get("score"))
        why = str(verdict.get("reason") or "").strip()
    except (KeyError, ValueError, TypeError):
        raise RuntimeError("unparseable model reply: %s" % str(data)[:120])

    where = locality(lead, criteria)
    score, notes = apply_rules(fit, lead, where, criteria, ladder=ladder)
    adj, note = realism(lead.get("role"), criteria)
    if adj:
        score = max(0, min(10, score + adj))
        notes.append(note)
    reason = why
    if notes:
        reason = "%s (%s)" % (why, "; ".join(notes))
    return score, reason[:300]


async def health(session):
    try:
        async with session.get("%s/api/version" % OLLAMA_URL, timeout=TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False
