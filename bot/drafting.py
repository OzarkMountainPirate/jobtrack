#!/usr/bin/env python3
"""Application drafting.

This is the half that has to sound like you, so it uses the Anthropic API
rather than the local model. Scoring stays local and costs nothing; spend
here scales with applications actually sent, not leads that arrive.

His profile is the whole system prompt and is identical on every call, so it
is marked for caching - the letter text is the only part that varies.
"""

import json
import logging
import os

import anthropic

import profile as prof

log = logging.getLogger("jobtrack.drafting")

MODEL = os.environ.get("DRAFT_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("DRAFT_MAX_TOKENS", "16000"))

KINDS = {
    "letter": "a cover letter",
    "essay": ("an answer to: 'Please explain how your past personal and "
              "professional experience make you a quality candidate for this "
              "position.'"),
    "why": "a short answer to 'why do you want this job'",
    "leaving": ("a reason-for-leaving answer for the most recent employer, "
                "suitable for a short application field"),
    "email": ("a brief email to the hiring contact, to accompany an "
              "application already submitted"),
}

SYSTEM_TEMPLATE = """You draft job application material for one person, in
their voice, from the record below. You are writing as them, in first person.

THE RECORD
%s

HOW TO WRITE
Plain, factual, first person.

Never editorialize. Do not add a sentence explaining why something matters,
why it is impressive, or what it says about them. State what they did and
stop. A line like "which is most of what separates a help desk that scales
from one that drowns" is an opinion put in their mouth, and it reads as
machine-written.

Never invent anything. No dates, employers, device counts, ticket volumes,
certifications or technologies that are not in the record above. If a specific
number would strengthen a sentence and you do not have it, write the sentence
without it, or leave [BRACKETS] to be filled in. A fabricated detail gets
caught in an interview.

Never state or hint at money. The record carries what they earn now, the
floors they apply and the band they are aiming for, because the ranking needs
them. None of it belongs in anything an employer reads. If a posting demands a
salary expectation, leave [SALARY EXPECTATION] and say so on the NEEDS line.
Never mention current pay, and never describe a role as a step up, a raise, or
a chance to grow into something.

When explaining a move between jobs, lead with scope rather than pay.

No greeting-card closings, no "I am excited to", no "passionate about". No
bullet-point resumes restated as prose. Vary sentence length; do not open
consecutive sentences the same way.

Where the record has identity.links, those are public and real: a site, a
repository host. When a posting asks for something one of them demonstrates,
name the URL once, plainly. Bare URL, no adjectives, no "feel free to take a
look", never more than once in a piece, and leave it out entirely when the
posting has nothing to do with it.
%s
Write only the requested text. No preamble, no explanation of your choices, no
sign-off commentary. If you had to leave a bracket, put a single line after the
text starting "NEEDS:" listing what must be supplied."""

# Anything in the profile's writing_rules is pasted in verbatim. This is where
# "do not claim ITIL, I am familiar with it but hold no certification" goes -
# the specific claims you must not let a model make on your behalf.
HOUSE_RULES = """
WHAT NOT TO CLAIM
%s
"""


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=key)


def _system():
    p = prof.load()
    if not p:
        raise RuntimeError("no profile stored - run !profile first")
    rules = p.get("writing_rules") or []
    house = HOUSE_RULES % "\n".join("- %s" % r for r in rules) if rules else ""
    return SYSTEM_TEMPLATE % (json.dumps(p, indent=1, sort_keys=True), house)


def draft(lead, kind="letter", limit=None, extra=None):
    """Returns (text, usage). `lead` is a row from the applications table."""
    if kind not in KINDS:
        raise ValueError("kind must be one of: %s" % ", ".join(KINDS))

    posting = ["Write %s for this posting." % KINDS[kind], "",
               "POSTING",
               "Employer: %s" % (lead.get("company") or "not stated"),
               "Role: %s" % (lead.get("role") or "not stated"),
               "Location: %s" % (lead.get("location") or "not stated"),
               "Work mode: %s" % (lead.get("work_mode") or "not stated")]
    if lead.get("salary_min") or lead.get("salary_max"):
        posting.append("Posted pay: %s-%s" % (lead.get("salary_min"),
                                              lead.get("salary_max")))
    if lead.get("url"):
        posting.append("URL: %s" % lead["url"])
    if lead.get("notes"):
        posting.append("Notes: %s" % lead["notes"])
    if extra:
        posting += ["", "ADDITIONAL CONTEXT FROM HIM", extra]
    if limit:
        posting += ["", "HARD LIMIT: %d characters. Application fields truncate "
                        "silently, so stay under it." % int(limit)]

    client = _client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        # 1h rather than the default 5 minutes: applying is done in sittings,
        # with reading and editing between letters, and the 5 minute window
        # expires in those gaps.
        system=[{"type": "text", "text": _system(),
                 "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[{"role": "user", "content": "\n".join(posting)}],
    )

    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined: %s" % getattr(resp, "stop_details", ""))

    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    u = resp.usage
    usage = {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    # Opus 5 list pricing, for a running sense of spend. A 1h cache write
    # costs about 2x base input; reads are about 0.1x.
    usage["cost"] = round(
        (usage["input"] * 5 + usage["cache_write"] * 10
         + usage["cache_read"] * 0.5 + usage["output"] * 25) / 1_000_000, 4)
    log.info("drafted %s for %s: %d in (%d cached), %d out, ~$%.4f",
             kind, lead.get("company"), usage["input"], usage["cache_read"],
             usage["output"], usage["cost"])
    return text, usage
