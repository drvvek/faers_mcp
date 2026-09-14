# FAERS MCP — assessment and improvement plan

This document records the assessment that shaped the server: a review of two reference
openFDA MCP servers, the defects found in the original single-file implementation, and the
design decisions that followed. The original implementation was derived from a companion
Chrome extension ("AEMS") that computes ROR/PRR from openFDA in the browser; that extension
is referred to throughout as the source of several behaviours, both correct and incorrect.

Evidence was measured against the live openFDA API on 2026-09-05. Every claim marked
**[verified]** has a reproducible curl behind it.

---

## 1. Verdict on the two reference servers

**Augmented-Nature/OpenFDA-MCP-Server — breadth without rigor.**
TypeScript/Node, 10 tools spanning drug + device endpoints. Its FAERS handling is
materially *worse* than yours:

- `drug_name` maps to `patient.drug.medicinalproduct` — the trade name, not the active
  substance — and without `.exact`, so it is tokenized: "ASPIRIN" also matches
  "ASPIRIN 81MG EC". Different semantics, silently.
- `drugcharacterization` appears nowhere. No suspect/concomitant distinction at all.
- No ROR/PRR. No raw search. No sort. No response-size budget.

**Take:** the *idea* of companion endpoints (label, recall, NDC) as a separate, later
server. Take none of its code or field map.

**cyanheads/openfda-mcp-server — rigor without pharmacovigilance.**
TypeScript/Bun on `@cyanheads/mcp-ts-core`, 14 tools over 14+ endpoints, Apache 2.0.
Real engineering discipline:

- Measured ~24 KB serialized page budget, with `page_omitted` + byte size + recovery routes
- Typed `pagination_limit_reached` error at the openFDA skip ceiling of 25,000
- Exponential backoff on 429/5xx; 404 normalized to empty results
- stdio *and* Streamable HTTP
- `openfda_describe_fields` so the model stops inventing field paths
- Zod validation at startup and per request; Vitest suite

But it has zero PV domain logic: no ROR/PRR, no suspect-role handling, and its
`openfda_drug_profile` counts adverse events regardless of drug role.

**Take:** the plumbing, not the domain model.

**Framing:** Augmented-Nature is breadth without rigor. cyanheads is rigor without
pharmacovigilance. You have pharmacovigilance without rigor. The job is to add cyanheads'
rigor to the existing PV logic — not to adopt either server.

**Leave behind:** DataCanvas/DuckDB (pandas is cheaper here), Cloudflare Workers, the
SQLite bulk mirror, Augmented-Nature's field map and license.

---

## 2. Verified findings

### 2.0 BLOCKER — every tool returned "No results found" [verified]

Clauses were joined with `+AND+`, but the query goes through httpx's `params` dict, which
percent-encodes `+` to `%2B`. openFDA received a literal plus, not the AND operator:

```
build_drug_query() as written today : (404, 'No matches found!')
same query, space-joined            : (200, 67027)
```

`build_drug_query()` is itself compound — `(name AND drugcharacterization:1)` — so this
fired on **every tool**, not only the ones taking events. The 404 handler then reported it
to the caller as a polite *"No results found for this query."* The server looked like it
worked and always said there was no data.

It also explains why 2.1 below never surfaced in practice: the query never ran.

Fixed in Phase 1: clauses join with `" AND "`, and `validate_raw_query` rejects `+AND+`
so it cannot be reintroduced through the raw search tool.

### 2.1 CRITICAL — `drugcharacterization:1` does not mean "this drug is the suspect" [verified]

```
ASPIRIN, any role                      547,048
ASPIRIN AND drugcharacterization:1     545,046   (99.6% retained)
ASPIRIN AND drugcharacterization:2     486,912   (89.0% retained)
```

The two filtered counts sum to far more than the unfiltered total. They are matching
independently. openFDA flattens `patient.drug[]`, so the clause means *"this report
contains aspirin somewhere AND contains some suspect drug somewhere"* — not *"aspirin is
the suspect."* Since essentially every ICSR has a suspect drug, the filter is a near-no-op:
it removes 0.4% of aspirin reports, and aspirin is overwhelmingly a concomitant drug.

**Consequence:** `build_drug_query(suspect_only=True)` is the default on all ten tools.
Every count, every demographic breakdown, and the entire ROR/PRR module is computed on an
any-role denominator while being *documented and labelled* as suspect-only. This is the
worst defect in the server — a wrong answer wearing a confident label, which is precisely
the QPPV-facing risk already identified for the SIGNAL banner.

The companion extension already contains the correct pattern: `dashboard.js:265`
`filterForSuspectDrug()` checks that the *same drug object* carries
`drugcharacterization === '1'`. `signal_detection.js:144` uses the broken URL-level
filter. The MCP inherited the wrong one.

### 2.2 CRITICAL — response size makes `faers_search_cases` unusable [verified]

Ten real empagliflozin ICSRs:

| | bytes |
|---|---|
| smallest record | 8,140 |
| largest record | 367,213 |
| mean record | 72,378 |
| **`limit=10` total** | **723,781 (~181,000 tokens)** |
| compact card x 10 | 3,480 |

The default `limit=10` can return 181k tokens. A compact card (id, dates, serious flags,
suspect drugs, PTs, outcomes) is **208x smaller**. This is the #1 usability bug.

### 2.3 CRITICAL — `faers_get_report` rejects every real ID [verified]

The regex is `^\d{7}-\d{1,2}$`. Real IDs sampled from the API:

```
10084081  10193585  10229378  10269219  10308818
```

Plain 8-digit, no hyphen. The version lives in a separate `safetyreportversion` field.
`safetyreportid:"10084081"` returns 1 report. The current pattern rejects 100% of real IDs
— the tool is dead on arrival.

### 2.4 The original defect list was half-wrong on `.exact` case sensitivity [verified]

```
reactionmeddrapt.exact  "PANCREATITIS" / "Pancreatitis" / "pancreatitis"   52,226 / 52,226 / 52,226
reactionmeddrapt.exact  "ACUTE KIDNEY INJURY" / "Acute kidney injury"     150,299 / 150,299
activesubstancename.exact "EMPAGLIFLOZIN"                                  67,251
activesubstancename.exact "Empagliflozin" / "aspirin"                      no results
```

**MedDRA PT `.exact` is case-INSENSITIVE. Substance name `.exact` is case-SENSITIVE.**

That is the opposite of the assumption in the original defect #1. So:

- Keep `.upper()` on `drug_name` — it is **required**, not optional.
- The `.upper()` on events is **harmless**. Do **not** build the dual-casing / Title-Case
  search strategy — it would double the call count for no gain.
- This is undocumented API behavior. Pin it with a fixture test so a future openFDA change
  surfaces as a test failure rather than silently halved counts.

### 2.5 `faers_time_trend` returned an empty trend for every drug [verified]

Two separate questions here, and they have opposite answers.

*Truncation:* not a problem. `count=receivedate` ignores `limit` for date fields — it
returned 3,944 buckets against `limit=999`, summing to exactly 67,251, the true total. The
histogram is complete.

*Parsing:* broken. openFDA keys date buckets as `time`, not `term`:

```
[{'time': '20150304', 'count': 1}, {'time': '20150415', 'count': 1}, ...]
```

The code read `item.get("term", "")`, matched nothing, and returned
`{"yearly_trend": [], "total_years_with_data": 0}` — an empty answer rather than an error,
for every drug ever queried. Fixed in Phase 1; both key names are now accepted and the
`time` key is pinned by a live contract test.

*(This supersedes an earlier note in this document that called the tool sound. The
completeness finding held; the parsing defect was separate and was found during Phase 1
smoke testing.)*

### 2.6 Confirmed from the original defect list

- **Sequential calls, new client per call.** `faers_disproportionality` does 4 sequential
  awaits, `faers_outcome_breakdown` 7, `faers_case_counts` 5, `faers_demographic_profile` 4
  — each opening its own `httpx.AsyncClient`.
- **Signal rule contradicts its own docstring.** Docstring says "ROR lower CI > 1 OR
  PRR >= 2 with N >= 3"; code is `ror_lower > 1 and a >= 3`.
- **Co-suspect screen is misnamed.** Generalize the rule: *a `count` field returns every
  value present in the matching reports; the `search` clause selects reports, not array
  elements.* So counting `activesubstancename` over a drug-filtered search yields
  co-**reported** substances at any role, never co-suspect.
- **No Lucene escaping.** `drug_name` and event strings are interpolated straight into
  quotes. Escape `+ & | ( ) " : \ / ! ^ ~ * ?`.
- **No date window, no sort, no skip cap** on the case tools.

### 2.7b Caret terms are returned by the count API but rejected by search [verified]

Found while running the first live bulk screen. FAERS stores apostrophes as a caret —
`CROHN^S DISEASE`, `PARKINSON^S DISEASE`, `FOURNIER^S GANGRENE`. The count API returns
those terms happily, but feeding one back into a search fails however it is escaped:

```
.exact:"CROHN^S DISEASE"      -> BAD_REQUEST
.exact:"CROHN\^S DISEASE"     -> BAD_REQUEST
.exact:"CROHN?S DISEASE"      -> BAD_REQUEST
.exact:"CROHNS DISEASE"       -> NOT_FOUND
(non-exact):"CROHN S DISEASE" -> 58,021      <- the only form that works
```

This matters because the bulk screen feeds count-API terms straight back into per-term
lookups, so a single Fournier's gangrene row killed the entire run. It also matters for
users, who will type `CROHN'S DISEASE`.

Handled by `term_clause()`: a term containing `^` or `'` drops `.exact` and matches a
tokenised phrase instead. That is slightly over-inclusive because a longer PT containing
the same token run also matches — measured overshoot `CROHN^S DISEASE` +0.08%,
`PARKINSON^S DISEASE` +1.36%. Affected rows carry the flag `approximate_marginal`, and
the screen's audit block lists them.

Separately: one failing term must never sink a whole screen. Per-term lookups are now
individually fault-tolerant.

### 2.7 New — API key is sent in the query string

`faers_request` puts the key in `params["api_key"]`, so it lands in URLs, proxy logs and
crash traces. Your extension already does this correctly: openFDA accepts HTTP Basic auth
with the key as username and an empty password (`dashboard.js:213`). Port that.

---

## 3. The role-basis problem, and the honest resolution

Once 2.1 is accepted, a hard question follows: **can a true suspect-only ROR be computed
through the openFDA API at all?**

- Cell `a` (drug + event) is usually small. It *can* be suspect-verified by fetching those
  reports and filtering client-side, exactly as `dashboard.js` does.
- Cell `b` derives from `a+b` = every report for the drug. For empagliflozin that is 67,251
  records at ~72 KB each — above the 25,000 skip ceiling and ~4.8 GB of transfer. It
  **cannot** be suspect-verified.

So a fully suspect-basis 2x2 is generally **not computable**. That has a direct consequence
for the companion extension:

> **`dashboard.js` computes a biased ROR.** Cell `a` is suspect-verified
> (`filterForSuspectDrug`), but `b = totalDrugSuspectReports - a` comes from the unscoped
> URL-level drug query (`dashboard.js:1015`), so `b` is inflated by every concomitant-only
> report. Mixing a suspect-verified numerator with an any-role marginal biases ROR
> **downward**. The code comments acknowledge the asymmetry; the exported CSV does not.

**Resolution — pick one basis and apply it to all four cells:**

- Default `role_basis: "any"`. All four cells from any-role counts. 4 calls, internally
  consistent, and labelled in the payload as *"any role (suspect + concomitant + interacting)"*.
- Offer suspect verification only on the **case-level** tools, where records are already in
  hand and filtering is free. Return a `suspect_verified` flag per compact card.
- Do **not** offer a mixed-basis ROR. If a caller asks for one, return a typed error
  explaining why it is not computable.

Note the asymmetry that makes this tolerable: cells `c` and `d` involve no drug role at all,
so only `a` and `b` are affected — and holding both at any-role keeps the table internally
consistent.

---

## 4. The bulk signal screen — port it, but not the way the extension does it

The extension's `signal_detection.js` is its most valuable calculation, and the most expensive:
top-500 events (1 call) -> **one count call per event** (500 calls, 300 ms throttle) ->
grand total (1 call). ~502 calls and several minutes. In a chat tool that is unusable and
would burn half a keyless daily quota on one question.

**The DB-wide PT marginals can be fetched in a single call** —
`?count=patient.reaction.reactionmeddrapt.exact&limit=999` returns the global top-999 PTs
with their counts, which is exactly the `acValue` the extension queries one at a time.

Measured for EMPAGLIFLOZIN [verified]:

```
global PT marginals cached in 1 call      999
drug's top PTs                            500
covered by the single global call         444/500 = 88.8%
fallback per-PT calls needed               56
TOTAL                                      58 calls   (vs ~502 today)
```

Persist the global table keyed on `meta.last_updated` and repeat screens for other drugs
cost ~57 calls, dropping to near-zero for the covered 89%. `limit=1000` requires an API key
[verified: keyless requests fail at limit>=1000 with `API_KEY_MISSING`].

This is a ~9x reduction that neither reference server has, and it turns a multi-minute batch
job into a chat-latency tool.

**Also port from `signal_detection.js` (it gets these right):**

- Conditional Haldane-Anscombe: apply the 0.5 correction **only** when a zero cell would
  make ROR/PRR undefined; leave non-zero tables untouched (`signal_detection.js:404`).
  Your MCP currently just errors out on any zero cell.
- Validity flags before computing (`query_failed`, `invalid_cells`) so a failed marginal
  never silently becomes a fabricated ROR (`signal_detection.js:391`).
- The audit-trail metadata block on every export: tool version, generation timestamp, query
  term, date filter, `meta.last_updated`, cell values (`signal_detection.js:698`).

**Port from `dashboard.js`:** the date-window discipline — `withDate()` applies the range to
**every** query including the grand total N (`dashboard.js:36`), so the 2x2 stays internally
consistent. The original MCP had no date window at all; once one exists, a date filter must apply
to all four cells or none.

---

## 5. Signal rule — replace the banner with named criteria

Drop `signal_detected: bool` and the warning/checkmark banner. Report each criterion
separately under its own name:

| Criterion | Rule |
|---|---|
| `ema_ror` | ROR lower 95% CI > 1 **and** a >= 3 |
| `evans_prr` | PRR >= 2 **and** chi-square >= 4 **and** a >= 3 |

Return `criteria_met: {ema_ror: bool, evans_prr: bool}` plus all raw metrics, and always
carry the disclaimer: *openFDA counts are not de-duplicated and do not match FAERS Public
Dashboard case counts; these are reporting-rate comparisons, not incidence, and cannot
support causal inference.* Put it in every count and disproportionality payload.

---

## 6. TypeScript vs Python

**Not actually TS advantages:**

- *Streamable HTTP.* FastMCP Python has it today: `mcp.run(transport="streamable-http")`.
- *Deployment.* `uvx faers-mcp` and `npx faers-mcp` are both one-command, no manual venv.
  Roughly a tie; npx has broader familiarity, uvx is newer but works.

**Real TS advantages:**

- Zod -> JSON Schema is the reference path, and the TS SDK is the most-exercised one.
- One npm artifact, no Python-version skew on colleagues' machines — a real support burden
  if the server is distributed to other users.
- Edge/serverless deploy is possible (Python needs a container).
- cyanheads' patterns become copy-paste rather than translation.

**Real Python advantages — and for *this* server they dominate:**

- scipy/statsmodels provide chi-square with continuity correction, exact CIs, and above all
  **EBGM/MGPS** (a 5-parameter gamma-Poisson mixture MLE needing digamma/lgamma plus an
  optimizer) and **BCPNN IC** with proper shrinkage. In TS the special functions are hand-rolled —
  which is exactly where wrong numbers enter a regulated deliverable.
- pandas for the staging sketched in Phase 4. cyanheads reached for DuckDB specifically
  because JS has no dataframe.
- 876 working lines already exist.

**Decision rule:**

- Statistical ceiling is ROR / PRR / chi-square / IC — all closed-form -> **TS is fine.**
- EBGM, MGPS, or any shrinkage estimator on the roadmap -> **stay Python.**

Your Phase 4 lists chi-square and IC, both closed-form. So TS is viable.

**Recommendation: fix in Python first, then port — not the reverse.**

Every defect in section 2 is a domain-logic defect, not a language defect. Porting first
means porting the bugs into a language where it is not yet possible to tell whether a
number changed because of a fix or because of a translation error. Do Phase 0-1 in Python, write the
fixtures as language-agnostic JSON (recorded openFDA responses + expected outputs), and
those fixtures become the port's acceptance suite. That is the thing that makes a port safe,
and it is the only cheap way to prove the TS version computes identical numbers.

Cost either way: fix-then-port ~= 4 days + 3 days. Port-then-fix ~= 3 days + 5 days of
harder debugging. Your call — but the fixtures are the deciding artifact, so build them
before the language choice is revisited.

---

## 7. Phased plan

### Phase 0 — lock the contract (half day)

One page, committed to the repo: role basis and why suspect-only is not computable
(section 3); substance uppercased / PT case-free (2.4); compact vs full case; the named
signal criteria (section 5); the non-deduplication disclaimer. Every count and
disproportionality payload carries that disclaimer.

### Phase 1 — correctness (2 days)

1. `OpenFdaClient`: one shared client, Basic-auth key (2.7), timeout, retry with backoff on
   429/5xx, 404 -> empty, skip ceiling 25,000 as a typed error.
2. `asyncio.gather` on every multi-count tool (4 -> 1 round trip on disproportionality,
   7 -> 1 on outcome breakdown).
3. **Fix the role basis** (2.1, section 3) — the single highest-value change. Default
   `role_basis="any"`, honest labels, no mixed-basis ROR.
4. Lucene escaping; keep `.upper()` on drug, drop the PT casing worry.
5. Relax `safetyreportid` to `^\d{6,10}(-\d{1,2})?$` and search both forms (2.3).
6. Compact case projection by default with `full: bool` and `fields_omitted` (2.2); full
   records only in `faers_get_report`.
7. Rename the co-suspect tool to co-**reported**, or filter the count field (2.6).
8. Replace the signal banner with named criteria (section 5); add conditional
   Haldane-Anscombe and the validity flags from `signal_detection.js`.
9. Fixture tests: one known drug-event pair, an empty result, a 404, a 429, plus a
   **regression pin on the `.exact` case behavior** in 2.4.

### Phase 2 — plumbing and reach (1.5 days)

- Add to search/count tools: `date_from`, `date_to` (applied to *all* cells or none),
  `sort` (`receivedate:desc`), `role_basis`, and an optional `raw_filter` clause ANDed onto
  the preset query.
- **`faers_raw_search`** — the raw-query escape hatch. Takes a raw Lucene string plus
  optional `count`, `limit`, `skip`, `sort`. Validates quote/paren balance, enforces the
  limit and skip caps, returns a compact projection by default with `full: bool`.
- **`faers_describe_fields`** — static catalog of the documented field paths, so
  the model stops inventing them.
- Return `effective_query` on **every** tool.
- Structured errors: `{reason, recovery}`, never a bare `"error"` string.

### Phase 3 — the bulk screen (1 day)

`faers_signal_screen` implementing section 4: global PT marginal table cached on
`meta.last_updated`, per-PT fallback for the ~11% miss, conditional Haldane, named criteria,
validity flags, audit metadata. This is the tool that makes the server worth more than the
extension.

### Phase 4 — run it like a server (half day)

`mcp.run()` stdio for local MCP clients; `mcp.run(transport="streamable-http",
host="127.0.0.1", port=8010)` for HTTP/remote. README with both configs.
**Do not host publicly with a personal key.**

### Phase 5 — decide on TS

If porting: fixtures from Phase 1 are the acceptance suite. Target the official TS SDK with
Zod schemas; borrow cyanheads' page-budget and typed-error shapes; skip the framework,
DuckDB, and the mirror. ~3 days with fixtures in hand.

### Phase 6 — optional

Label / recall / shortage tools in a **second** server. pandas staging for "give me 200
compact cards grouped by country." chi-square and IC alongside ROR/PRR, each labelled an
openFDA-count approximation.

---

## 8. Target tool surface (13)

| Tool | Change |
|---|---|
| `faers_search_cases` | compact projection, date window, sort, skip cap, `role_basis`, `suspect_verified` per card |
| `faers_case_counts` | gathered, role-labelled, disclaimer |
| `faers_disproportionality` | consistent role basis, named criteria, conditional Haldane, no banner |
| `faers_count_by_field` | note report-scoped count semantics |
| `faers_top_events` | unchanged apart from labelling |
| `faers_get_report` | fixed ID pattern — full record lives here and only here |
| `faers_demographic_profile` | gathered |
| `faers_outcome_breakdown` | gathered (7 -> 1) |
| `faers_time_trend` | date buckets keyed `time`, not `term` — trend was always empty |
| `faers_coreported_drugs` | renamed from `drug_interaction_screen` |
| `faers_signal_screen` | **new** — section 4 bulk screen |
| `faers_raw_search` | **new** — raw Lucene escape hatch |
| `faers_describe_fields` | **new** — static field catalog |

---

## 9. Fix order, by cost of being wrong

0. `+AND+` joining (2.0) — **every tool returned nothing**
1. Role basis (2.1) — wrong numbers, confidently labelled
2. Response size (2.2) — tool unusable at its own default
3. `safetyreportid` (2.3) — tool 100% broken
4. `time_trend` parsing (2.5) — silently empty answers
5. Signal banner (section 5) — QPPV-facing risk
6. Shared client + gather (2.6) — quota and latency
7. Everything else

---

## 10. Phase 1 status — complete

All nine Phase 1 items are implemented and tested. 81 offline tests and 9 live contract
tests pass.

```
faers/client.py      shared client, Basic auth, retry, 404->empty, skip/limit ceilings
faers/query.py       space joining, escaping, case rules, date clause, raw validation
faers/projection.py  compact cards, per-record suspect verification
faers/stats.py       conditional Haldane, validity flags, named criteria, chi-square
faers/errors.py      structured {code, reason, recovery}
faers/server.py      ten tools rewritten
tests/               81 offline + 9 live contract tests, recorded fixtures
```

Verified end to end against the live API (EMPAGLIFLOZIN x PANCREATITIS):

```
cells      a=575  b=66,676  c=51,651  d=20,573,788
ROR        3.435  (95% CI 3.163 - 3.731)
PRR        3.414  (95% CI 3.146 - 3.705)
chi-square 973.2
criteria   ema_ror=true  evans_prr=true    flags: none
trend      12 years, summing to 575 - matches cell a exactly
3 compact cards  1,444 bytes   |   1 full record  97,785 bytes
```

The first card returned is a good illustration of finding 2.1: the report matches the
empagliflozin query, but its suspect drugs are TIRZEPATIDE and CHOLECALCIFEROL —
empagliflozin is concomitant on that report, and `suspect_verified` correctly reports
`false`. Under the old `suspect_only=True` default that record was counted as a suspect
case.

---

## 11. Phase 2 status — complete

`date_from` / `date_to` / `raw_filter` are accepted by every query-shaped tool, applied
uniformly to **all four marginals including the grand total N** — so a filter turns the
2x2 into a genuine stratified analysis rather than an inconsistent one. A half-open window
is rejected rather than silently widened.

New tools:

* **`faers_raw_search`** — arbitrary Lucene passthrough, validated for balanced quotes and
  parentheses and for the `+AND+` mistake before anything is sent. Compact projection and
  the skip/limit ceilings still apply, and `drug_name` optionally suspect-verifies each
  returned record.
* **`faers_describe_fields`** — static catalogue of field paths in five categories, each
  carrying the trap that produces a plausible-but-wrong answer: `drugcharacterization`
  filtering the report rather than the drug, the split `.exact` case sensitivity, and the
  `time`-keyed date histogram.

Every tool returns `effective_query`, and all failures are `{code, reason, recovery}`.

---

## 12. Phase 3 status — complete

`faers_signal_screen` runs in both directions (`mode="drug"` screens a drug against its
events; `mode="event"` screens an event against its drugs), with `min_cases`,
`signals_only`, `sort_by`, `return_n` and a date window.

**Measured live, screening 200 events for EMPAGLIFLOZIN:**

```
cold run (empty cache)   13 API calls   9 fallback   13.3 s
warm run, another drug   10 API calls   7 fallback    3.9 s
extension equivalent    ~202 calls at a 300 ms throttle, ~60 s floor
```

The global marginal table is cached on `meta.last_updated`, so a FAERS refresh invalidates
it automatically and a date window keys a separate table.

Top rows are the textbook SGLT2-inhibitor signals, which is the outcome that matters:

| event | n | ROR | ROR lo95 | PRR | chi2 |
|---|---|---|---|---|---|
| EUGLYCAEMIC DIABETIC KETOACIDOSIS | 2,049 | 343.8 | 322.8 | 333.4 | 325,407 |
| FOURNIER^S GANGRENE | 1,102 | 265.9 | 245.3 | 261.6 | 154,407 |
| KETOACIDOSIS | 1,519 | 81.3 | 76.8 | 79.5 | 93,550 |
| DIABETIC KETOACIDOSIS | 3,902 | 64.2 | 62.0 | 60.6 | 191,118 |
| NECROTISING FASCIITIS | 253 | 24.2 | 21.3 | 24.1 | 5,189 |

Euglycaemic DKA and Fournier's gangrene are the two labelled SGLT2i risks, the latter the
subject of the FDA's 2018 safety communication. Necrotising fasciitis and fungal infection
follow as the related genital mycotic signals.

Ported from `signal_detection.js`: conditional Haldane-Anscombe, pre-computation validity
flags, and the audit metadata block (tool version, generation time, query term, date
window, `meta.last_updated`, N, call counts, method statement).

---

## 13. Phase 6 status — EBGM implemented

### Library choice: none of the three candidates at runtime

* **vigipy** — GPL-3.0 (copyleft: linking it makes the whole server GPL-3), hard pins
  `numpy<2` / `pandas==2.2.2` / `scipy==1.13.1` / `statsmodels==0.14.2`, and is not on PyPI.
* **rpy2 + openEBGM** — needs a full R runtime, painful on Windows, heavy to distribute.
* **scipy directly** — BSD, no pins, ~250 lines. Chosen.

`faers/ebgm.py` implements DuMouchel's model: a two-component gamma mixture prior, a
negative-binomial mixture marginal likelihood, an MLE over the five parameters, and the
posterior gamma mixture whose geometric mean is EBGM. EB05/EB95 come from inverting the
posterior mixture CDF, which has no closed form.

**Zero-truncated likelihood** is the default, because openFDA reports only co-occurring
pairs and fitting the untruncated form to such data biases the prior. A test demonstrates
that bias rather than asserting it.

### Validation

Without an external oracle the only honest check is recovering known parameters from
simulated data. Fitting 20,000 synthetic cells drawn from a known prior:

| parameter | true | fitted | error |
|---|---|---|---|
| a1 | 2.000 | 2.018 | 0.9% |
| b1 | 2.000 | 2.006 | 0.3% |
| a2 | 4.000 | 4.132 | 3.3% |
| b2 | 0.500 | 0.512 | 2.4% |
| P | 0.850 | 0.858 | 0.9% |

Prior component means recover to within 1%. A first attempt used components with means
1.67 and 1.50 and recovered them badly — a two-gamma mixture is weakly identified when the
components overlap, so that was a bad test rather than a bad fit. The suite now uses
separated components and additionally pins the ordering convention, since the mixture is
exchangeable.

### The result that justifies the work

EBGM only differs from ROR where evidence is thin, so both cases are worth stating.

**Heavily reported drug — the methods agree.** Empagliflozin's top 200 events (all
n >= 201): EBGM retains 98-100% of the raw ratio and the top-10 rankings by ROR lower CI
and by EB05 are identical.

**Sparsely reported drug — the methods diverge sharply.** Teplizumab, 29 total reports:

| event | n | expected | RRR | ROR lo95 | EBGM | EB05 |
|---|---|---|---|---|---|---|
| HEPATIC CYTOLYSIS | 2 | 0.017 | 118.3 | **30.2** | 11.0 | **1.5** |
| BLOOD POTASSIUM INCREASED | 2 | 0.022 | 89.1 | 22.7 | 10.1 | 1.5 |
| DEEP VEIN THROMBOSIS | 2 | 0.090 | 22.1 | 5.6 | 5.0 | 1.1 |

A lower confidence bound of 30 on two cases is exactly the false alarm a QPPV-facing tool
must not produce. EB05 of 1.5 sits below the conventional threshold of 2 and declines to
flag it. The whole ranking reorders: EBGM promotes LYMPHOPENIA (n=9) to first and drops
TEMPERATURE REGULATION DISORDER and CHEILITIS (both n=3) out of the top six. Teplizumab's
two labelled warnings are lymphopenia and EBV reactivation, which EBGM ranks 1 and 2.

### Background table

`faers/background.py` assembles the drug x event table the prior is fitted on: 3 +
`background_drugs` calls, cached on `meta.last_updated` alongside the fitted
hyperparameters. Default 100 drugs gives 74,470 observed cells; cold build ~105 calls and
~100 s, subsequent calls ~6 calls and ~8 s.

One correction made during live testing: the background's event universe is the globally
most-reported terms, which **excludes** drug-specific signals. Euglycaemic DKA and
Fournier's gangrene — the two most important empagliflozin findings — were being dropped
as `event_marginal_unavailable`. Missing marginals are now fetched on demand and flagged
`marginal_fetched_on_demand`.

### Declared limitations, carried in every payload

* **Unstratified.** FDA's MGPS stratifies by age, sex and report year; doing so here would
  multiply the API calls by the number of strata. These values will not reproduce FDA's
  published EBGMs.
* Expected counts use any-role marginals (section 2.1).
* The prior is fitted on a truncated background, not all of FAERS.
* openFDA counts are not de-duplicated.

`numpy` and `scipy` are imported defensively, so the other 13 tools run without them and
`faers_ebgm` returns a structured error instead of breaking the server.


---

## 14. Phase 7 status — stratification

Both the frequentist and Bayesian statistics are now confounder-adjustable.

### Field choice was decided by measured coverage

```
patient.patientsex          87.9% of reports
patient.patientonsetage     55.3% (unit = years)
patient.patientagegroup     18.4%   <- rejected
receivedate                  ~100%
```

`patientagegroup` is the field the schema suggests for age, and it would have
discarded 82% of the database. Age bands are derived from `patientonsetage` with a
unit-of-years filter instead.

### ROR / PRR — Mantel-Haenszel

`faers_disproportionality` gains `stratify_by` (`sex`, `age`, `year`, `age_sex`). It pools
stratum-specific 2x2 tables with MH weights — Robins-Breslow-Greenland variance for the
odds ratio, Greenland-Robins for the proportional reporting ratio — and reports crude and
adjusted together with a **Breslow-Day** test of homogeneity.

The chi-square tail is written out rather than taken from scipy, so the non-EBGM tools stay
dependency-free; it matches `scipy.stats.chi2.sf` to 1e-9 across the tested range.

Cost is four calls per outer band, not four per stratum: openFDA can count *on* the
stratifying field, so one call returns every stratum at once. Age and year are counted on
their underlying numeric/date field and binned locally.

Live results:

| analysis | crude ROR | adjusted | change | Breslow-Day |
|---|---|---|---|---|
| EMPAGLIFLOZIN x VULVOVAGINAL CANDIDIASIS, by sex | 9.12 | 12.23 | +34.1% | p=0.81, homogeneous |
| EMPAGLIFLOZIN x DIABETIC KETOACIDOSIS, by sex | 64.24 | 57.33 | -10.8% | p~0, heterogeneous |
| ATORVASTATIN x RHABDOMYOLYSIS, by age | 6.27 | 4.89 | -22.1% | p~0, heterogeneous |

The atorvastatin row is the clearest: statins go to older patients, who report more
rhabdomyolysis, and the age-specific ORs fall monotonically (16.95 / 5.58 / 4.51). The
Breslow-Day result is the useful part — where it is significant, the pooled number is
hiding a real difference and the per-stratum rows should be read instead.

### EBGM — stratified expected counts

Stratification enters DuMouchel's model only through the expectation:

    E_ij = sum_k (n_i.k * n_.jk) / n_..k

so the observed counts are untouched and the prior is simply refitted on the stratified
pairs. That costs 2K+1 extra calls (one substance count and one event count inside each
stratum, plus the stratum sizes), cached separately from the crude marginals — a prior
fitted against crude E is not valid for stratified E, so it gets its own cache key.

Sex-adjusting empagliflozin moved the prior means from 1.48/4.42 to 1.81/5.47 and the
expected counts with it: urinary tract infection E fell 545.5 -> 463.9 (EBGM 3.70 -> 4.36)
because the drug skews female and so does the event.

`year` is refused for EBGM with a structured error: its strata are not enumerable before
counting, and stratified E needs each stratum's whole marginal distribution.

### A silent wrong answer found while testing this

The background's cell rows are filtered to its event universe (the globally commonest
terms). An explicitly requested event outside that universe was therefore absent from the
row, and the tool reported `observed: 0` — "no cases" for a pair that had 38. Requested
events missing from the row are now measured directly. Both tools now agree exactly:
EMPAGLIFLOZIN x VULVOVAGINAL CANDIDIASIS gives 38 from `faers_ebgm` and a=38 from
`faers_disproportionality`.

### Report-year stratification

The gap left open above is closed: `year` now works on `faers_ebgm` as well as
`faers_disproportionality`, so all three of FDA's MGPS stratification variables are
available.

Year is the only stratifier whose strata are not fixed in advance, so `resolve_strata`
measures them. The measured distribution justifies pruning:

```
distinct years in FAERS   38
1986-2003                 385 reports total   (0.002%)
2004-2026                 20,692,305 reports  (99.998%)
```

Stratifying on all 38 would cost 76 calls to gain 385 reports. Years holding less than 0.1%
of the window's reports are dropped and named in the payload. Two further details:

* The resolving call is a `count=receivedate` histogram, which already measures every
  stratum's size — so it doubles as the stratum-size measurement rather than being repeated.
* `max_strata` (default 30) refuses to fragment the data past the point of estimating
  anything from it, and the error points at `date_from`/`date_to` as the way to narrow.

Cost for year on EBGM is 1 resolve + 2 per kept stratum (~47 calls for the full 2004-2026
range), cached like every other background artefact. Mantel-Haenszel is unaffected: it
needs only counts, so year has always cost it four calls.

Measured live on empagliflozin, comparing all three stratifiers:

| | crude | by sex | by year |
|---|---|---|---|
| strata | — | 2 (static) | 23 (measured, 2004-2026) |
| coverage of database | 100% | 87.3% | **100%** |
| calls | 4 | 12 | 55 |
| prior means | 1.48 / 4.42 | 1.81 / 5.47 | 2.00 / 27.17 |

| event | obs | E crude | E sex | E year | EBGM crude | EBGM sex | **EBGM year** |
|---|---|---|---|---|---|---|---|
| FOURNIER^S GANGRENE | 1,102 | 7.78 | 8.78 | 12.65 | 140.0 | 124.3 | **87.0** |
| DIABETIC KETOACIDOSIS | 3,902 | 76.89 | 73.74 | 74.11 | 50.7 | 52.9 | **52.6** |
| URINARY TRACT INFECTION | 2,024 | 545.5 | 463.9 | 538.2 | 3.70 | 4.36 | **3.76** |
| NAUSEA | 3,595 | 2530.3 | 2166.9 | 2320.8 | 1.42 | 1.66 | **1.55** |

Fournier's gangrene is the row that matters: EBGM falls from 140 to 87, a 38% reduction,
the largest adjustment any stratifier produces here. Empagliflozin was approved in 2014 and
Fournier's reporting spiked after FDA's 2018 safety communication, so both the drug's
reports and the event's reports are concentrated in the same recent years. Its expected
count rises 7.78 -> 12.65 once that era effect is accounted for. This is precisely the
notoriety/stimulated-reporting bias report-year stratification exists to remove, and it is
why FDA's MGPS stratifies on it.

Year is also the only stratifier that costs no coverage: `receivedate` is present on every
report, so the adjusted estimate describes the same population as the crude one - unlike
sex (87.3%) or age (55.3%).

