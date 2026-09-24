# FAERS MCP Server

Wraps the OpenFDA Drug Adverse Event API (`https://api.fda.gov/drug/event.json`) as an MCP
server with 15 pharmacovigilance tools: case search, disproportionality (ROR/PRR), empirical
Bayes signal scores (MGPS/EBGM), bulk screening, and confounder-adjusted versions of all of
them.

## Layout

```
faers/
  server.py       tool definitions and the faers-mcp entry point
  client.py       shared openFDA client: Basic-auth key, retry, ceilings
  query.py        Lucene construction, escaping, the case-sensitivity rules
  projection.py   compact case cards, per-record suspect verification
  stats.py        ROR / PRR / chi-square, Mantel-Haenszel, Breslow-Day
  screen.py       bulk signal screen with the cached global marginal table
  ebgm.py         DuMouchel gamma-Poisson shrinker (MGPS) against scipy
  background.py   the drug x event background table the prior is fitted on
  strata.py       sex / age / year stratifiers and their resolution
  fields.py       field catalogue served by faers_describe_fields
  errors.py       structured {code, reason, recovery} failures
tests/            offline tests on recorded fixtures, plus live openFDA contract tests
```

## Installation

```bash
pip install -e .            # from a clone
pip install -e ".[test]"    # with the test dependencies
```

Or directly from the repository, with no clone or virtualenv:

```bash
uvx --from git+https://github.com/drvvek/faers_mcp faers-mcp --version
```

The `mcp` dependency is pinned to `<2`: mcp 2.x renamed `FastMCP` to `MCPServer`, and an
unpinned `>=1.0` resolves to 2.x on a fresh install.

## API key

Without a key: 1,000 requests/day and a count `limit` ceiling of 999.
With a key: 120,000 requests/day and `limit` up to 1,000.
Free key at https://open.fda.gov/apis/authentication/

The key is read from `OPENFDA_API_KEY` and sent as an HTTP Basic auth header, never in the
query string, so it stays out of URLs, proxy logs and crash traces. The key belongs in the client config or the environment, never in the repository;
`.gitignore` excludes `.env`.

## Running

```bash
faers-mcp                                   # stdio - what MCP clients spawn
faers-mcp --transport http --port 8010      # Streamable HTTP on 127.0.0.1:8010/mcp
python -m faers                             # same as faers-mcp
```

`FAERS_MCP_TRANSPORT`, `FAERS_MCP_HOST` and `FAERS_MCP_PORT` set the defaults. Binding
HTTP to anything other than loopback prints a warning: the transport has no authentication.

## Interface

- **Arguments are flat.** Every tool takes `drug_name`, `events`, ... at the top level of
  the arguments object; nothing is wrapped in a `params` key. Enumerated arguments
  (`role_basis`, `stratify_by`, `mode`, `sort_by`, `category`) are declared as enums in the
  schema, and `date_from`/`date_to` carry a `^\d{8}$` pattern, so invalid values are
  rejected before any API call.
- **Results are structured.** Tools return objects; the server publishes an `outputSchema`
  and sends the result as `structuredContent` with a JSON text fallback.
- **Failures are errors.** A failed call is an MCP error result (`isError: true`) whose
  text is a `{code, reason, recovery}` object. It is never a success payload with
  `ok: false`.
- **Progress.** `faers_signal_screen`, `faers_ebgm` and `faers_warm_cache` report progress
  to clients that support it.

### Timeouts

The first `faers_ebgm` call for a FAERS release builds a background table: 3 + 100 API calls
and a prior fit, about 100 s in total, which exceeds many MCP clients' per-call timeout.
Call **`faers_warm_cache`** first — it does that work on a turn that expects it and reports
whether each cache was already warm — after which `faers_ebgm` takes a few seconds. Year
stratification adds ~47 calls on its first use, likewise cached.

### Prompt

`faers_signal_workup(drug_name, event=None)` returns a fixed sequence of tool calls for a
defensible work-up — counts, screen, crude and stratified ROR/PRR, EBGM with year
stratification, trend, suspect-verified cases — ending with the disclaimer. Clients that
support MCP prompts can offer it directly.

## Deployment options

The choice between them comes down to whether each user spends their own openFDA quota or
all users share one key.

### 1. Git URL + `uvx` (per-user install)

Each user adds the following to their MCP client config, with their own API key:

```json
{
  "mcpServers": {
    "faers": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/drvvek/faers_mcp", "faers-mcp"],
      "env": { "OPENFDA_API_KEY": "<key>" }
    }
  }
}
```

There is nothing to clone and no virtualenv to manage; updates are picked up on the next
launch. A private repository works the same way provided the user's machine already has
GitHub credentials (`gh auth`, a credential manager, or an SSH key) — `uvx` does not
prompt. Caches build per machine under `~/.faers_mcp_cache`.

### 2. Shared HTTP endpoint

A single process serves the tools over Streamable HTTP. The openFDA key belongs on that
**server process**, not in each client's config. The server reads `OPENFDA_API_KEY` from
the environment; it does not load a `.env` file.

Git Bash:

```bash
OPENFDA_API_KEY=<key> python -m faers --transport http --host 127.0.0.1 --port 8010
```

cmd:

```bat
set OPENFDA_API_KEY=<key>
python -m faers --transport http --host 127.0.0.1 --port 8010
```

PowerShell:

```powershell
$env:OPENFDA_API_KEY="<key>"
python -m faers --transport http --host 127.0.0.1 --port 8010
```

Use the same interpreter that has the package installed. Bind to loopback or a private
interface. Then every client points at the URL only:

```json
{ "mcpServers": { "faers": { "url": "http://127.0.0.1:8010/mcp" } } }
```

No per-user install, and the expensive caches — the EBGM background table, fitted priors,
stratified marginals — are built once and shared. The trade-offs: all users draw on one
key's 120,000/day quota, the endpoint has no authentication of its own, and it is a single
point of failure. It should be bound to a private interface or placed behind an
authenticating proxy, and never exposed to the public internet.

| | per-user quota | install effort | shared caches | auth |
|---|---|---|---|---|
| git + uvx | yes | none | no | n/a |
| shared HTTP | no — one key | none | yes | none built in |

### Local install

From a clone:

```bash
pip install -e .
```

Point the desktop client at that interpreter with an **absolute path**. Bare `faers-mcp` only works if the client's spawn PATH includes the Scripts directory; on Windows it often does not.

```json
{
  "mcpServers": {
    "faers": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python314\\python.exe",
      "args": ["-m", "faers"],
      "env": { "OPENFDA_API_KEY": "<key>" }
    }
  }
}
```

`Python314` in the example is whatever interpreter you ran `pip install -e .` with — change the folder to match (3.10+).

That block belongs in the client's `mcpServers` config (on Claude Desktop: `%APPDATA%\Claude\claude_desktop_config.json`). Fully quit and reopen the client after editing.

This is not the same as Settings → Connectors → Local command, which runs in a remote sandbox and cannot see a Windows install. Do not use Connectors for a local stdio server. A hosted HTTP URL is only needed for Connectors → Remote.

## Tools

| Tool | Description |
|------|-------------|
| `faers_search_cases` | Case reports as compact triage cards (`full=true` for raw ICSRs) |
| `faers_case_counts` | Total / serious / fatal counts |
| `faers_disproportionality` | 2x2 table, ROR, PRR, chi-square, named criteria; optional Mantel-Haenszel adjustment |
| `faers_count_by_field` | Aggregate by any FAERS field (GROUP BY equivalent) |
| `faers_top_events` | Top MedDRA PTs for a drug |
| `faers_get_report` | Full ICSR by Safety Report ID |
| `faers_demographic_profile` | Sex, age (coded group and onset-age bands, each with coverage), reporter, country |
| `faers_outcome_breakdown` | Reaction outcomes + seriousness criteria |
| `faers_time_trend` | Yearly reporting trend |
| `faers_coreported_drugs` | Substances co-reported with an index drug |
| `faers_signal_screen` | Bulk ROR/PRR screen: a drug against all its events, or vice versa |
| `faers_ebgm` | Empirical Bayes signal scores (EBGM, EB05/EB95) via MGPS; optional stratification |
| `faers_raw_search` | Arbitrary Lucene query passthrough, validated |
| `faers_describe_fields` | Catalogue of searchable field paths, stratifiers and their traps |
| `faers_warm_cache` | Build the EBGM background and fit the prior ahead of time; report cache state |

## Date windows and filters

Every query-shaped tool accepts `date_from` / `date_to` (YYYYMMDD, both or neither) and a
`raw_filter` Lucene clause. On `faers_disproportionality` and `faers_signal_screen` these
are applied to **all four marginals including the grand total N**, so the table stays
internally consistent — which is what makes `raw_filter="patient.patientsex:2"` a genuine
restricted analysis rather than a broken one.

## Bulk screening

`faers_signal_screen` computes ROR, PRR and chi-square for every event reported with a
drug (or every drug reported with an event). The database-wide marginals come from a
single cached count call rather than one call per term:

```
screening 200 events, cold cache   13 API calls, 13.3 s
same, warm cache, another drug     10 API calls,  3.9 s
one-call-per-term equivalent      ~202 calls, ~60 s floor
```

The cache is keyed on openFDA's own `meta.last_updated`, so a FAERS refresh invalidates it.
Set `FAERS_CACHE_DIR` to relocate it; it defaults to `~/.faers_mcp_cache`.

## Stratification

Crude ROR/PRR/EBGM compare a drug against the whole database, so anything predicting both
exposure and reporting confounds them. Pass `stratify_by` to adjust:

| stratifier | strata | field | coverage |
|---|---|---|---|
| `sex` | male, female | `patient.patientsex` | 87.9% |
| `age` | 0-17, 18-64, 65+ | `patient.patientonsetage` (years) | 55.3% |
| `year` | calendar year, measured from the data | `receivedate` | ~100% |
| `age_sex` | age band x sex | both | lower |

`patientagegroup` is not used for age: it is populated on only 18.4% of reports. Age
bands are derived from `patientonsetage` instead.

`faers_disproportionality` pools stratum-specific tables by **Mantel-Haenszel** (Robins-
Breslow-Greenland variance for ROR, Greenland-Robins for PRR) and reports crude and
adjusted side by side with a **Breslow-Day** homogeneity test:

```
ATORVASTATIN x RHABDOMYOLYSIS, by age        coverage 55.3%
  crude ROR    6.273      adjusted ROR  4.888   (-22.1%, material)
  Breslow-Day  p = 0.0  -> heterogeneous
      0-17   a=11    ROR 16.95
      18-64  a=963   ROR  5.58
      65+    a=1538  ROR  4.51
```

Statins are prescribed predominantly to older patients, who also report more
rhabdomyolysis; adjustment removes that confounding. A small Breslow-Day p indicates that
the stratum-specific estimates differ, in which case the per-stratum rows are more
informative than the pooled value.

Cost is four calls per outer band, because openFDA can count *on* the stratifying field and
the bins are formed locally. Reports missing the field cannot enter any stratum, so every
stratified result states its coverage.

`faers_ebgm` accepts all four. Stratification enters MGPS only through the expected count —
`E = sum_k (n_drug,k * n_event,k) / n_k` — and the prior is refitted on those expectations.

Year strata are not knowable in advance, so they are measured from the data and the
negligible tail is pruned: FAERS spans 38 calendar years, but 1986–2003 hold **385 reports
between them (0.002%)** while costing two calls each to stratify. `max_strata` (default 30)
caps the number of strata; a narrower `date_from`/`date_to` window reduces it.

Year is the only stratifier with full coverage (`receivedate` is present on every report).
On empagliflozin it produces the largest correction:

| event | obs | EBGM crude | EBGM by sex | EBGM by year |
|---|---|---|---|---|
| FOURNIER^S GANGRENE | 1,102 | 140.0 | 124.3 | **87.0** |
| DIABETIC KETOACIDOSIS | 3,902 | 50.7 | 52.9 | 52.6 |
| URINARY TRACT INFECTION | 2,024 | 3.70 | 4.36 | 3.76 |
| NAUSEA | 3,595 | 1.42 | 1.66 | 1.55 |

Empagliflozin was approved in 2014 and Fournier's gangrene reporting spiked after FDA's
2018 safety communication, so both are concentrated in the same recent years. Adjusting for
report year removes that stimulated-reporting effect and drops EBGM by 38%.

## EBGM / MGPS

`faers_ebgm` fits DuMouchel's Gamma-Poisson Shrinker and reports EBGM with an EB05/EB95
credibility interval. EB05 is the conventional screening statistic; EB05 > 2 is the usual
threshold.

Shrinkage is what distinguishes EBGM from ROR and PRR, which treat two cases against an
expectation of 0.02 as a strong signal; EBGM discounts a ratio in proportion to how little
evidence supports it. Teplizumab (29 reports in total) illustrates the difference:

| event | n | expected | RRR | ROR lo95 | EBGM | EB05 |
|---|---|---|---|---|---|---|
| HEPATIC CYTOLYSIS | 2 | 0.017 | 118.3 | **30.2** | 11.0 | **1.5** |
| BLOOD POTASSIUM INCREASED | 2 | 0.022 | 89.1 | 22.7 | 10.1 | 1.5 |
| DEEP VEIN THROMBOSIS | 2 | 0.090 | 22.1 | 5.6 | 5.0 | 1.1 |

An ROR lower confidence bound of 30 on two cases is a false alarm; the EB05 of 1.5 sits
below the screening threshold. On a heavily-reported drug the two agree closely — for empagliflozin's
top 200 events (all n >= 201) EBGM retains 98-100% of the raw ratio and the rankings match.

The prior is fitted across a drug x event background table, not the single pair, so the
first call for a FAERS release builds and caches it (3 + `background_drugs` calls; ~100 s
for the default 100 drugs / 74,000 cells). Later calls take about 6.

**Caveats, repeated in every payload:** expected counts use any-role marginals, the prior
comes from a truncated background, and the likelihood is zero-truncated because openFDA
reports only co-occurring pairs. Unstratified by default — pass `stratify_by` for sex, age
or report year. These values will not reproduce FDA's published EBGMs.

## Reading the output

**Role basis.** openFDA cannot scope a count to one drug's role within a report: the
clause `drugcharacterization:1` filters the *report*, not the matched drug. Counts are
therefore any-role (suspect, concomitant or interacting), and are labelled as such in
`role_basis_note`.
`faers_search_cases` accepts `role_basis="suspect_verified"`, which checks each returned
record individually — the one place the distinction is computable.

**Screening criteria.** There is no single "signal detected" verdict. `criteria_met`
reports each convention separately:

| Criterion | Rule |
|---|---|
| `ema_ror` | ROR lower 95% CI > 1 and a >= 3 |
| `evans_prr` | PRR >= 2 and chi-square >= 4 and a >= 3 |

**Counts are not de-duplicated.** They do not match FAERS Public Dashboard case counts.
These are reporting-rate comparisons, not incidence, and cannot support causal inference.
Every count payload carries this disclaimer.

**Approximate terms.** FAERS stores apostrophes as a caret (`CROHN^S DISEASE`), and
openFDA rejects that character in a search however it is escaped. Such terms fall back to a
tokenised phrase match, which is slightly over-inclusive (measured +0.08% to +1.36%). Rows
affected carry the flag `approximate_marginal`.

**Response size.** ICSRs average ~72 KB; ten raw records measured 724 KB. Case tools
return compact cards by default and declare what was dropped in `fields_omitted`.

## Tests

```bash
python -m pytest
```

CI runs the offline suite on Python 3.10–3.12 for every push and pull request. The live
contract suite runs on pushes only, never gates a PR, and needs `OPENFDA_API_KEY` as a
repository secret to stay under the keyless quota.

Offline tests run against recorded fixtures. Contract tests that hit the live API — they
pin undocumented openFDA behaviour such as `.exact` case sensitivity and the `time` key on
date histograms — are deselected by default:

```bash
python -m pytest -m live
```

## Example queries

- *"What are the top adverse events for empagliflozin in FAERS?"*
- *"Calculate ROR and PRR for metformin and lactic acidosis, adjusted for age"*
- *"Give me a demographic profile of levetiracetam rhabdomyolysis cases"*
- *"Show the yearly trend of pancreatitis reports with sitagliptin"*
- *"Find atorvastatin myopathy cases where atorvastatin is the suspect drug"*
- *"EBGM for empagliflozin's top events, stratified by report year"*
