#!/usr/bin/env python3
"""Your professional profile.

Both halves of phase 3 read from here: scoring compares a posting against it,
drafting writes from it. Kept as one JSON document on the data volume rather
than SQL tables, because it is read whole, edited by hand, and never queried
relationally.

It holds whatever you put in it, which for most people includes reference
contacts, salary history and things said in confidence - so it lives on the
data volume, mode 600, and never in the repository.
"""

import json
import os

REQUIRED_TOP = ("identity", "preferences", "employment")


def path():
    return os.environ.get("PROFILE_FILE", "/data/profile.json")


def load():
    p = path()
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def save(profile):
    p = path()
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, "w") as f:
        json.dump(profile, f, indent=2)
    os.chmod(p, 0o600)
    return p


def gaps(profile):
    """Fields still carrying a REVIEW marker or left empty.

    Anything uncertain is flagged rather than invented, so a draft never states
    a date or a number that nobody verified.
    """
    found = []

    def walk(node, trail):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, trail + [str(k)])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, trail + ["[%d]" % i])
        elif isinstance(node, str):
            # A marker, not merely the word: writing_rules legitimately
            # mentions REVIEW while describing the convention.
            if node.strip() == "" or node.strip().upper().startswith("REVIEW"):
                found.append(".".join(trail))

    walk(profile or {}, [])
    return found


def summary(profile):
    """Compact plain-text profile for a model prompt or a quick look."""
    if not profile:
        return "no profile stored"
    idn = profile.get("identity") or {}
    pref = profile.get("preferences") or {}
    out = ["%s - %s" % (idn.get("name", "?"), idn.get("location", "?"))]
    if idn.get("headline"):
        out.append(idn["headline"])
    out.append("")

    sal = pref.get("salary_floor") or {}
    out.append("Wants: %s" % ", ".join(
        "%s %s" % (k, ("$%s" % f"{v:,}") if isinstance(v, int) and v else "no floor")
        for k, v in sal.items()))
    if pref.get("commute_from"):
        out.append("Commute base: %s (max %s min)" %
                   (pref["commute_from"], pref.get("max_commute_minutes", "?")))
    if pref.get("work_modes"):
        out.append("Work modes: %s" % ", ".join(pref["work_modes"]))
    out.append("")

    if profile.get("certifications"):
        out.append("Certifications: %s" % ", ".join(profile["certifications"]))
    for group, items in (profile.get("skills") or {}).items():
        out.append("%s: %s" % (group, ", ".join(items)))
    out.append("")

    out.append("Experience:")
    for job in profile.get("employment") or []:
        out.append("  %s - %s (%s to %s)" % (
            job.get("title", "?"), job.get("employer", "?"),
            job.get("start", "?"), job.get("end", "?")))
        for b in (job.get("highlights") or [])[:3]:
            out.append("      %s" % b)
    return "\n".join(out)
