#!/usr/bin/env python3
"""Read a district's published pay scale.

Districts are required to publish what they pay and it is public, but it lives
on the district website as a PDF rather than inside the posting - which is why
every district lead arrived with a null salary.

The documents are laid out the same way: a heading naming the job family, then
a grid of steps and lanes. That is enough to take a range from, and a range is
what the ranking needs. It is not enough to pick a single number from, because
which lane someone lands in depends on credentials and negotiation, so nothing
here tries to.
"""

import io
import logging
import re

log = logging.getLogger("jobtrack.payscale")

HEADING = re.compile(r"^(.{0,80}?)\s*(?:salary|pay|wage)\s+schedule\s*$", re.I | re.M)
MONEY = re.compile(r"\b\d{2,3},\d{3}(?:\.\d{2})?\b")
# Support staff grids are published as hourly rates, with no comma to spot
# them by. Districts do the same for some technology rungs.
RATE = re.compile(r"(?<![\d,.])\d{1,2}\.\d{2}(?![\d,])")
HOURS_PER_YEAR = 2080
NOISE = re.compile(r"(?i)\b(19|20)\d{2}\s*[-–]\s*(19|20)?\d{2}\b")   # "2026-2027"

# Words that say which family a schedule covers, mapped to what a posting
# would call itself. Extend FAMILIES for the job families you apply to.
FAMILIES = {
    "technology": ("technology", "technolog", "computer", "network", "it", "information"),
    "administrator": ("administrator", "administrative", "director", "coordinator"),
    "secretarial": ("secretarial", "secretary", "clerical", "registrar"),
    "maintenance": ("maintenance", "custodial", "custodian"),
}


def text_from_pdf(data):
    """PDF bytes to text. Returns None when the file cannot be read."""
    try:
        from pypdf import PdfReader
    except ImportError:                     # pragma: no cover - image without it
        log.warning("pypdf is not installed; pay scales will not be read")
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        log.warning("could not read pay scale pdf: %s", e)
        return None


def schedules(text):
    """{heading: (min, max)} for every salary schedule in the document."""
    if not text:
        return {}
    text = text.replace("​", "").replace(" ", " ").replace("\xa0", " ")
    marks = list(HEADING.finditer(text))
    out = {}
    for n, m in enumerate(marks):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -–")
        if not name:
            continue
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        body = NOISE.sub(" ", text[m.end():end])
        nums = [float(x.replace(",", "")) for x in MONEY.findall(body)]
        nums = [x for x in nums if 15000 <= x <= 400000]
        if len(nums) < 4:
            rates = [float(x) for x in RATE.findall(body)]
            rates = [x for x in rates if 10 <= x <= 99]
            nums = [x * HOURS_PER_YEAR for x in rates]
        if len(nums) < 4:            # a grid, not a stray figure in prose
            continue
        out[name] = (int(min(nums)), int(max(nums)))
    return out


def family(label):
    """Which job family a schedule heading or a posting belongs to."""
    low = (label or "").lower()
    for name, words in FAMILIES.items():
        for w in words:
            # Short words need both boundaries: "it" would otherwise match
            # "itinerant" and file it as a technology schedule.
            edge = r"\b%s\b" if len(w) <= 3 else r"\b%s"
            if re.search(edge % re.escape(w), low):
                return name
    return None


def for_role(text, role, category=None):
    """(min, max, heading) for the schedule covering this role, or (None,)*3.

    Matching is by job family rather than title, because a schedule is named
    for the family - "Technology Salary Schedule" - and never for the job.
    """
    want = family(role) or family(category)
    if not want:
        return None, None, None
    for heading, (lo, hi) in schedules(text).items():
        if family(heading) == want:
            return lo, hi, heading
    return None, None, None
