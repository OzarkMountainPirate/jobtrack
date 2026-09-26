#!/usr/bin/env python3
"""What a posting demands that you do not have.

Same split as scoring: the local model reads the posting and reports what it
*states* - a facts task it does reliably - and code decides what those facts
mean for you, from the blockers you declare in profile.json:

    a programming language you do not write
    a hard years-in-a-technology minimum you cannot meet
    a certification listed as required
    a bachelor's degree listed as required

Everything found is reported as a gap. Only blockers move the score, so you
see the whole picture and are penalised only for walls.

Nothing here knows any particular person. What you write, what you hold and
what counts as a wall all come from the profile.
"""

import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("jobtrack.gaps")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("GAPS_MODEL", os.environ.get("SCORING_MODEL", "phi4-mini:latest"))
TIMEOUT = aiohttp.ClientTimeout(total=180)

# Languages that mean software engineering. Whether any of these is a wall
# depends on the profile: anything listed under blockers.writes is scripting
# you actually do, and is removed from this set at analysis time.
DEV_LANGS = {"go", "golang", "java", "c#", "csharp", "c++", "cpp", "rust",
             "ruby", "scala", "kotlin", "php", "elixir", "erlang", "swift",
             "typescript", "javascript", "node", "nodejs", ".net", "dotnet",
             "perl", "r", "matlab", "cobol", "objective-c"}


def _blockers(profile):
    """(writes, holds, clearance_blocks) from the profile's blockers section."""
    b = (profile or {}).get("blockers") or {}
    writes = {str(x).lower() for x in (b.get("writes") or [])}
    holds = {str(x).lower() for x in (b.get("certifications_held") or [])}
    return writes, holds, bool(b.get("clearance_is_a_blocker"))


EXTRACT = """Read the job posting and report only what it explicitly states as
REQUIRED. Reply with JSON only, no commentary:

{"languages": [], "certifications": [], "degree_required": false,
 "clearance_required": false, "years": [], "technologies": []}

languages        programming languages the posting requires writing
certifications   certifications the posting lists as required
degree_required  true only if a bachelor's degree or higher is required
clearance_required  true if a US security clearance is required
years            objects of the form {"tech": NAME, "years": NUMBER}, one for
                 each explicit minimum number of years in a named technology
technologies     the main tools and platforms named

RULES
Anything the posting calls preferred, desired, nice to have, a plus, or a
bonus is NOT required - leave it out of languages, certifications and years,
and put it in technologies instead.

"OR EQUIVALENT" MEANS NOT REQUIRED. Job postings routinely write "degree or
equivalent experience", "degree and/or certifications", or "X, Y, or
equivalent". Any requirement offering an alternative, an "and/or", or an
equivalence escape is satisfiable by experience - set degree_required false
and leave those certifications out. Only report a certification or degree
when the posting demands it with no alternative offered.

Copy names from the posting text. Never output a name that does not appear in
the posting. If the posting states no minimum years, "years" must be [].

Report only what the text says. Do not infer and do not add what a role like
this usually wants. Empty lists are the correct answer when nothing is
stated."""


async def extract(session, description):
    body = {"model": MODEL, "format": "json", "stream": False,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": EXTRACT},
                         {"role": "user", "content": description[:6000]}]}
    async with session.post("%s/api/chat" % OLLAMA_URL, json=body,
                            timeout=TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError("ollama %s" % r.status)
        data = json.loads(await r.text())
    return json.loads(data["message"]["content"])


def _known(profile):
    """Every skill string in the profile, lowercased."""
    out = set()
    for group in (profile.get("skills") or {}).values():
        for s in group:
            out.add(s.lower())
            out.update(re.split(r"[^a-z0-9+#.]+", s.lower()))
    for c in profile.get("certifications") or []:
        out.add(c.lower())
    for job in profile.get("employment") or []:
        for h in job.get("highlights") or []:
            out.update(re.split(r"[^a-z0-9+#.]+", h.lower()))
    return {x for x in out if x}


# "degree or equivalent experience", "certifications ... or equivalent",
# "and/or" - all mean the requirement is satisfiable by experience. The model
# cannot hold this reliably at 3B, and it is a fixed phrasing, so it is checked
# in code against the posting text instead.
SOFTENER = re.compile(
    r"(?i)(or equivalent|and\s*/\s*or|equivalent (experience|combination|"
    r"work experience|practical experience)|or comparable|or relevant "
    r"experience)")


def _is_soft(description, term):
    """True when `term` appears near an equivalence escape in the posting."""
    if not description:
        return False
    for m in re.finditer(re.escape(term), description, re.I):
        window = description[max(0, m.start() - 160):m.end() + 160]
        if SOFTENER.search(window):
            return True
    return False


def analyse(found, profile, description=None, role=None):
    """Returns (gaps, blockers) as lists of short human-readable strings."""
    known = _known(profile)
    writes, holds, clearance_blocks = _blockers(profile)
    dev_langs = DEV_LANGS - writes
    gaps, blockers = [], []

    # A language named in the title is the job's identity, not a line in a
    # requirements list, and it is there whether or not the posting body is
    # long enough to extract anything from. A "Golang Kubernetes Engineer"
    # posting arrived as a 565 character stub: the model found nothing to
    # object to and it scored near the top of the list, for a role built on a
    # language the candidate does not write.
    for word in re.split(r"[^a-z0-9+#.]+", (role or "").lower()):
        if word in dev_langs and word not in known:
            label = "writes %s" % word
            if label not in blockers:
                blockers.append(label)

    for lang in found.get("languages") or []:
        l = str(lang).strip().lower()
        if not l or l in writes or l in known:
            continue
        if l in DEV_LANGS:
            blockers.append("writes %s" % lang)
        else:
            gaps.append(str(lang))

    for cert in found.get("certifications") or []:
        c = str(cert).strip()
        if not c:
            continue
        cl = c.lower()
        if any(h in cl for h in holds) or cl in known:
            continue
        # Models often list a clearance under certifications. It is handled
        # below, as a gap or a blocker depending on the profile.
        if re.search(r"(?i)clearance|secret|ts/sci|polygraph", cl):
            continue
        if _is_soft(description, c):
            gaps.append("cert: %s (or equivalent)" % c)
            continue
        blockers.append("cert: %s" % c)

    if found.get("degree_required"):
        if any(_is_soft(description, w) for w in ("degree", "Bachelor", "BS ", "B.S.")):
            gaps.append("degree (or equivalent experience)")
        else:
            blockers.append("bachelor's degree")

    for item in found.get("years") or []:
        try:
            tech, yrs = str(item.get("tech", "")).strip(), int(item.get("years") or 0)
        except (AttributeError, ValueError, TypeError):
            continue
        if not tech or yrs < 3:
            continue
        # Postings name a requirement as a family - "MDM (Jamf / Intune /
        # Workspace ONE)" - so an exact string never matches a single skill he
        # holds. Any overlapping token means he has some of it: a gap in depth,
        # not a wall.
        tokens = {w for w in re.split(r"[^a-z0-9+#.]+", tech.lower())
                  if len(w) > 2}
        if tech.lower() in known or (tokens & known):
            gaps.append("%dy %s stated" % (yrs, tech))
        else:
            blockers.append("%dy %s" % (yrs, tech))

    for tech in found.get("technologies") or []:
        t = str(tech).strip()
        if t and t.lower() not in known and len(t) > 2:
            gaps.append(t)

    if found.get("clearance_required"):
        # Whether this is a wall is a personal call: plenty of employers
        # sponsor, so it is a gap unless the profile says otherwise.
        if clearance_blocks:
            blockers.append("security clearance")
        else:
            gaps.append("clearance (sponsorship)")

    seen = set()
    gaps = [g for g in gaps if not (g.lower() in seen or seen.add(g.lower()))]
    return gaps[:10], blockers[:6]


def penalty(blockers):
    """Only declared blockers move the score, three points each."""
    return -3 * len(blockers) if blockers else 0
