# jobtrack

A self-hosted job search bot. It watches the places jobs are actually posted,
scores what it finds against a profile you write, tracks every application and
tells you which ones have gone quiet — all from one chat room.

It is one container, one SQLite file, and no account anywhere.

---

## Why this exists

Because the job boards stopped working.

You search for "systems administrator", and you get a page of results sorted by
who paid. Half of them are staffing agencies reposting the same role. A quarter
are ghost listings for a position that was filled in March, or that never
existed and is there to collect résumés. The pay is not listed. You apply
through a form that re-types your résumé badly into eleven fields, and then
nothing happens — not a rejection, just silence, forever, and no way to tell a
dead application from a slow one.

Meanwhile the jobs you actually want are sitting on the employer's own site,
posted the day before, with the salary printed on them. Nobody is between you
and that page except the fact that there are two hundred such pages and you
cannot read them all every morning.

This is the program that reads them for you.

It has a second purpose, which turned out to matter more. When you are
applying to thirty places, the thing that sinks you is not finding the jobs —
it is losing track of the ones you already found. Which one did you follow up
on. Who said they would call. Which of these has been silent for eleven days
and needs a nudge today. A spreadsheet decays within a week. This does not,
because it is the same program that found the job in the first place.

There is no scraping of Indeed or LinkedIn here, deliberately. Those have terms
of service, they ban accounts, and they are the problem rather than the
solution. Everything this reads is a public API, a public feed, or a public
careers page that the employer wants you to read.

---

## What it does

**Finds.** Fifteen adapters against real hiring systems — Greenhouse, Lever,
Ashby, Workable, Workday, BambooHR, Paylocity, NeoGov, AppliTrack, HireTrue,
three remote boards, WordPress classifieds — plus a page-change watcher for
career pages that publish nothing at all. You list the employers you care
about; it checks them all, hourly, forever.

**Judges.** Every new posting is scored 0–10 against your profile by a local
model, and the score is explained in one sentence. Anything above a threshold
is announced immediately. The rest sort themselves.

**Reads.** Where a posting is only a teaser, it fetches the full text. Where a
posting does not state pay but the employer publishes a pay scale, it reads the
scale. It reports what a posting requires that you do not have, and separates
"a gap" from "a wall".

**Tracks.** Every application, every contact, every touch, with a quiet clock
on each and a weekday digest of what is overdue. It watches your mailbox and
files replies against the right application on its own.

**Writes.** Cover letters and application essays in your voice, from your
record, with a hard rule against inventing anything.

---

## How it works

```
  employer boards ──┐
  remote boards   ──┤
  ATS APIs        ──┼─► sources.py ──► SQLite ──► scoring.py ──► Matrix room
  career pages    ──┤     (poll)       jobtrack.db   (local model)      ▲
  your mailbox    ──┘                      │                            │
                                           └──► drafting.py ────────────┘
                                                (frontier API)
```

One design decision runs through all of it:

> **The model judges facts. Code applies rules.**

A 3B model is good at "is this posting the kind of work described in this
ladder" and unreliable at "is 40 miles within an hour's drive" or "is $15.43 an
hour more than I earn now". So the model reads the posting and reports what it
says; code decides what that means. Geography, salary, seniority and hard
requirements are all arithmetic and regular expressions, not inference. This is
why the scores are stable and why you can argue with them.

The second decision: **local model for volume, frontier model for voice.**
Scoring runs on Ollama, costs nothing and runs once per lead. Drafting calls a
paid API, costs a few cents, and only runs when you ask. Cover letters are the
only place the quality difference is worth paying for.

---

## Requirements

- Docker and Docker Compose
- A Matrix account for the bot and a private room. Any homeserver; you do not
  have to run one.
- [Ollama](https://ollama.com) somewhere you can reach, with a small instruct
  model pulled. `phi4-mini` is what this was tuned against and is plenty — it
  scores a lead in about two seconds on a six-year-old GPU.
- *Optional:* an Anthropic API key, for `!draft` only.
- *Optional:* a Microsoft 365 mailbox and an Entra app registration, for reply
  tracking.

---

## Install

```bash
git clone https://github.com/YOURNAME/jobtrack.git
cd jobtrack
cp .env.example .env            # fill in Matrix and Ollama
mkdir -p data
cp bot/profile.example.json data/profile.json
cp bot/sources.example.json data/sources.json
chmod 600 data/profile.json
```

Edit all three. Then:

```bash
docker compose build
docker compose run --rm bot jobtrack schema     # creates data/jobtrack.db
docker compose up -d
docker compose logs -f bot                      # "listening in <room>"
```

Say `!help` in the room.

**Two things that will waste an hour if you miss them.** `MATRIX_ROOM` must be
the room's *internal ID* — the `!abcdef:server` string in Room Settings →
Advanced, not the room name. The bot logs whatever you give it without
validating, so a wrong value looks perfectly healthy and silently does nothing.
And the room must be **unencrypted**; this uses matrix-nio without E2EE.

---

## Configure

Three files. Only the first one is really about you.

### `data/profile.json` — who you are and what you want

This is the heart of it. The scoring prompt, the gap analysis and every cover
letter are built from this file, so the more honest and specific it is, the
better all three get. `bot/profile.example.json` is a fully worked example.

The part that does the most work is **`search.ladder`** — your own order of
preference, most wanted first:

```json
"ladder": [
  { "score": 9, "families": "Systems administrator, infrastructure engineer, IT manager.",
    "note": "Work already done - any level is fine." },
  { "score": 8, "families": "Cloud operations, platform engineering, DevOps, SRE.",
    "note": "The move being made. Junior and associate titles welcome." },
  { "score": 6, "families": "Service desk and desktop support at any tier." }
]
```

Be blunt here. "I would take this but I do not want it" is more useful than a
diplomatic ranking, because the whole point is to sort thirty postings without
reading them. Pair it with `not_my_field`, which is the list that stops a
technology company's sales job scoring like a technology job.

Other fields that change behaviour:

| Field | What it does |
|---|---|
| `preferences.current_annual` | What you earn now. **The only thing that can lower a score.** |
| `preferences.target_salary.min` | What you are aiming for. Only ever *lifts* a lead. |
| `preferences.commute_towns` | Places you would drive to. Anything else is "distant". |
| `preferences.salary_floor.relocation` | The one hard floor: moving costs money. |
| `search.pivot_into` | Fields you are moving into but have not held. Senior titles in these get marked unrealistic. |
| `search.remote_bonus` | Points added for remote, gated on the role being in your field at all. |
| `blockers.writes` | Languages you genuinely write. Everything else in `DEV_LANGS` becomes a wall. |
| `blockers.certifications_held` | Certifications you hold, so they stop showing up as gaps. |
| `writing_rules` | Claims a model must never make on your behalf. |

The profile also holds references, salary history and anything else you put in
it, so it lives on the data volume at mode 600 and is never committed. `.gitignore`
covers `data/`.

### `data/sources.json` — where to look

One entry per source. `bot/sources.example.json` shows every adapter with the
URL you pull its token out of. The whole list is in
[Sources](#sources-and-how-to-point-them-at-an-employer) below.

`role_filter` is a regex the job **title** must match. The default shipped in
`sources.py` is an IT example — replace it with your own field, and remember
that **`data/sources.json` overrides the code.** Editing `DEFAULT_ROLE_FILTER`
in Python does nothing once that file exists. That one has caught people out.

For a single employer whose every opening interests you, set `role_filter:
null` on that source and use `location_filter` instead.

### `.env` — everything else

Commented in `.env.example`. The digest time, the alert threshold, where Ollama
lives, and the optional API keys.

---

## Using it

```
!list                      your pipeline
!leads                     untriaged leads, best scored first
!leads good                only 7 and above
!show 23                   one application, with its whole timeline
!add Acme Corp | Systems Administrator | status=applied source=greenhouse
!set 23 status=interviewing
!set 23 in_days=5 next_action=call if still quiet
!touch 23 email_sent followed up with HR
!contact 23 Jane Smith | email=jane@acme.com title=Recruiter
!due 7                     what is due in the next week
!stale                     applied, and gone quiet
!check                     what tomorrow's digest would say
!poll                      run every source now
!enrich                    fetch full postings for leads that arrived as teasers
!score                     score anything new
!score all                 re-score everything, after you change the rules
!watch add Remote 75k+ | role_re=(?i)(sysadmin|network) min_salary=75000
!draft 23                  a cover letter
!draft 23 essay 1500       an application essay, under 1500 characters
!mailauth                  sign in to Microsoft 365, once
!profile gaps              fields in your profile still needing input
```

The one habit that makes it work: **when you apply, say so.** `!set 23
status=applied` starts the quiet clock. Everything else — the digest, the
follow-up nudges, the response-rate numbers — is downstream of that one command
being honest.

Every weekday morning it posts what is due and what has gone quiet. If nothing
needs attention it says nothing at all, which is the point.

---

## Sources, and how to point them at an employer

| kind | What it is | What you need |
|---|---|---|
| `greenhouse` | Greenhouse board | slug from `job-boards.greenhouse.io/SLUG` |
| `lever` | Lever board | slug from `jobs.lever.co/SLUG` |
| `ashby` | Ashby board | slug from `jobs.ashbyhq.com/SLUG` |
| `workable` | Workable board | slug from `apply.workable.com/SLUG` |
| `bamboohr` | BambooHR board | subdomain from `SLUG.bamboohr.com/careers` |
| `workday` | Workday | tenant + site from `TENANT.wdN.myworkdayjobs.com/en-US/SITE` |
| `paylocity` | Paylocity recruiting | the UUID in the board URL |
| `neogov` | governmentjobs.com | agency slug from `governmentjobs.com/careers/AGENCY` |
| `applitrack` | Frontline/AppliTrack — school districts | the board URL as you see it |
| `hiretrue` | HireTrue "ce3" — state governments | host + board key from the URL |
| `acadp` | WordPress classifieds boards | the listings page URL |
| `remotive` `remoteok` `weworkremotely` | remote boards | a search phrase |
| `changedetection` | anything with no feed at all | a tag in changedetection.io |

A few of these are worth knowing about because they behave badly:

**AppliTrack** (most US school districts) links you to an embedded view that
renders a category summary — "Maintenance (3), Transportation (2)" — and no job
titles at all. The *same* view with `&all=1` renders every posting in full. If
you have ever wondered why district jobs are invisible to every job board, that
is why. This adapter uses `all=1`.

**Paylocity** truncates its description field to exactly 110 characters in the
listing. **HireTrue** caps at around 340. Both publish the whole posting one
request away, which is what `!enrich` fetches.

**Workday** pagination returns `total` on the first page only, and its
`searchText` is fuzzy enough that "information technology" returns surgical
technologists. Filter on title, not on the search.

**changedetection.io** is the fallback for pages with no feed. It tells you a
page changed, which by itself is useless — so this diffs the last two
snapshots, throws away timestamp lines, and only speaks up if what was *added*
reads like a job title in your field. Without that you get "PAGE CHANGED" every
morning from a page that reprints the clock.

---

## How scoring works

Three things happen to every lead, in order.

**1. The model places it on your ladder.** It sees the title, company, location
and work mode — not the description, deliberately, because descriptions are
marketing and the title is the job. It returns a score and one sentence.

**2. Code applies your rules.** Geography: remote, within your commute towns, or
distant. Pay, measured against both ends — the target only ever lifts a lead,
and the only thing that lowers one is failing to beat what you earn now. This
matters more than it sounds: an aspirational target used as a filter will bury
every job that is a large raise but not the dream, which are exactly the ones
worth applying to.

There is also a **ladder rule**: if an employer has been seen posting at or
above your target anywhere in the database, its junior roles get a point and
stop being penalised. A lower rung at a place that pays well is a way in, not a
dead end.

**3. Reality check.** For fields in `search.pivot_into` — work you want but have
never been paid to do — a senior title is not a realistic application however
well the work fits, and is scored accordingly. An entry-level title in the same
field gets a point *added*. This is the difference between a list of jobs you
want and a list of jobs you might get.

Change any of this and run **`!score all`**. A score is a verdict under one set
of rules; `!score` alone only looks at leads with no score, so rule changes stay
invisible on everything already ranked.

### Gap analysis

Where a posting has a description, a second pass asks the model what it
explicitly *requires*, and code decides what that means. Everything found is
reported. Only four things are walls, each costing three points:

- a programming language you do not write (from `blockers.writes`)
- a hard years-in-a-technology minimum
- a certification listed as required
- a bachelor's degree listed as required

Two refinements that took real postings to find. **"Or equivalent" is handled in
code**, not by the model — postings constantly write "degree and/or
certifications (Cisco, A+, or equivalent)", which is satisfiable by experience,
and reading it as four hard requirements will bury a job you are perfectly
qualified for. And **a language in the job title is a requirement** whether or
not the description mentions it: "Golang Kubernetes Engineer" with a
two-sentence description has no extractable requirements and is still a Go job.

---

## Reading pay

Worth its own section, because this is where a job tracker quietly lies to you.
A wrong salary is worse than no salary, since the ranking believes it.

`pay_from()` only accepts a figure when the words around it are about pay, or
when it is written as a range. Then it works out the period from what is
*attached* to the figure, and annualizes. Things it has been wrong about, all on
real postings, all now in `tests/test_pay.py`:

| Posting says | Naive reading | Correct |
|---|---|---|
| `(100% In-Office) Salary: $45,000` | $45,000–$100,000 | $45,000 |
| `Pay Range: Starting at $15.43` | $15,430 a year | $32,094 |
| `$106.54 / day`, 179-day calendar | $106,540 | $19,070 |
| `8.0 hrs / day. … Starting at $17.12` | daily rate, $3,252 | $35,609 |
| `$38,470.00 - $42,798.00 Annually` | nothing found | $38,470–$42,798 |
| `$5,000 sign-on bonus` | $5,000 salary | nothing |

Public employers are required to publish a pay scale even when the posting does
not state pay. `payscale.py` reads the PDF, finds the schedules by heading, and
matches one to a posting by **job family** — never by title, because that is how
the documents are organised. It records only the *floor* as the lead's pay: the
top of those grids is thirty years of steps, and filing it as the job's salary
would tell the ranking that an entry-level opening pays your target.

---

## Mail tracking

Optional, and it earns its keep. With a Microsoft Graph app registration and
`!mailauth` once, the bot reads your inbox and files replies against the right
application by contact address, then sender domain, then company name.

It will move an application forward on its own — a rejection to `rejected`, an
interview invitation to `interviewing` — but **never backwards**, and never off
`accepted`. A status you set by hand is a decision; an email is only news. (This
rule exists because a signed offer letter arriving two days after an acceptance
was recorded demoted the row from `accepted` back to `offer`.)

It reads the **inbox**, not the mailbox. `/me/messages` spans every folder
including Drafts, and Outlook writes a new message id on every autosave — so one
reply you are composing can arrive as six replies *from* the employer, each with
an empty sender, each looking like fresh activity on a dead application.

---

## Drafting

`!draft 23` writes a cover letter from your profile and the posting.
`!draft 23 essay 1500` answers a long-form application question under a
character limit. Kinds: `letter`, `essay`, `why`, `leaving`, `email`.

The whole profile is the system prompt, cached for an hour, so a sitting of ten
letters costs cents rather than dollars. The prompt's rules are worth reading in
`drafting.py` before you use it in anger — in particular it is told never to
invent a number, to leave `[BRACKETS]` and a `NEEDS:` line when it wants one it
does not have, and never to mention money in any form.

Put anything a model must not claim on your behalf in `writing_rules`.

Treat every draft as a first draft. The point is editing rather than composing.

---

## Teardown

```bash
make destroy        # container, image, network
rm -rf ./data       # and the database, profile and tokens with it
```

Nothing is installed on the host. No systemd units, no cron — the scheduler is
an asyncio timer inside the bot process. Back `./data` up before you remove it
if there is any chance you will want the history.

---

## A note on what this is not

It does not apply to jobs for you. Auto-apply tools exist; they are how you get
a hundred applications and no interviews, and how you get your account banned.
This finds the job, tells you honestly whether you have a shot, drafts the
letter, and then gets out of the way.

It also will not make a bad market good. What it will do is make sure that when
the right posting appears on a page nobody reads, at 6am on a Tuesday, you know
about it that morning — and that the one you applied to three weeks ago does not
quietly disappear.

---

## License

Copyright (C) 2026 Carl Alcott

Released under the [GNU General Public License v3.0 or later](LICENSE).
