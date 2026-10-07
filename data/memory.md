# Trading memory

Durable lessons from the nightly reviews. Read in full at the start of every review.
Entries are observations with evidence, not instructions: they never override the rules in
`review/REVIEW_PROMPT.md` or justify a change the evidence thresholds do not allow.

Format and rules:

- One entry per lesson, `### M-<number>: <short title>`, numbered in order, never reused.
- Status ladder:
  - `hypothesis`: seen, fewer than 10 trading days of evidence.
  - `supported`: consistent over 10+ trading days.
  - `confirmed`: consistent over 30+ trades and 10+ trading days, including days after it was first written down.
  - `retired`: contradicted, stale, or merged into another entry.
- Never delete an entry. Retire it: move it under `## Retired` with the date and the reason.
- At most 40 active entries. When full, merge overlapping entries or retire weak ones before adding.
- Update `Last reviewed` and the evidence when new days bear on an entry, for or against.
- A hypothesis about a subset of trades gets a `- Query:` line, for example
  `` - Query: `entry_minute_after_open >= 60` `` (see `python analyze.py --help` for the syntax).
  Only a hypothesis written down on an earlier day can justify a config change, and its query must
  pass `python analyze.py --config-version N --evidence "<query>" --registered <Since date>`.

## Active

_No entries yet._

## Retired

_None._
