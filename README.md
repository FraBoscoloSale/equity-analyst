# SEC equity research desk

Python 3.10+ desk implementing the seven data steps, then four isolated model roles.
Company identities come from SEC data; the code contains no issuer-specific rules.

Install and run from this directory:

```powershell
python -m pip install -r requirements.txt
python equity_desk.py MSFT --peers ORCL ADBE --price 420
```

The price is an input example, not a fetched quote. Supply a price in the company's
reporting currency. Peers may be separated by spaces or commas. Without `--price`,
P/E prints N/A. Without `--peers`, step 7 reports that it was skipped.

Put the following in the `.env` beside the script (or copy `.env.example`):

```dotenv
SEC_USER_AGENT="Your Name your.email@example.com"
API_KEY="your Purdue GenAI Studio key"
```

The program reads `API_KEY` directly from that file. It sends the key only as the
Purdue API bearer credential; requests saved in the run folder contain no credentials.
SEC requests use `SEC_USER_AGENT`, throttling, and bounded retries. Existing `.env`
contents are preserved. No new dependencies are required for the role layer.

To run just one numbered step:

```powershell
python equity_desk.py MSFT --step 4
python equity_desk.py MSFT --step 5 --price 420
python equity_desk.py MSFT --step 7 --peers ORCL ADBE
```

Single-step commands run only the selected data step and do not require `API_KEY`.
Prerequisite data is fetched quietly for a single step. Each invocation creates a
new `research_data/<company>_<local-date>_<time>/` folder. The run metadata records
the timezone. Existing runs are preserved.

## Seven steps

1. Resolve the input ticker using `company_tickers.json`.
2. Read submissions, list 10-K/10-Q/8-K filings, and choose the latest unamended
   10-K, 10-Q, and 8-K whose primary document contains Item 2.02. Metadata items
   are only a prefilter. Read **Accepted** directly from each EDGAR index page,
   preserving its displayed clock without converting the submissions timestamp.
   Search historical submissions pages when the recent filings do not suffice.
3. Save the primary filings and the selected 8-K's Exhibit 99.1, their source
   HTML (or PDF), and plain text. Also save the 8-K cover document for provenance.
   PDF conversion uses text extraction; image-only PDFs require OCR and report an error.
4. Fetch company facts and print nine concepts for revenue's latest full fiscal
   year, with the previous fiscal year beside each. Every nonmissing value carries
   its exact tag, unit, dates, accession, filing date, and source URL in `facts.json`.
5. Compute growth, operating margin, return on average equity, long-term debt
   including its current portion divided by equity, operating cash flow less cash
   additions to property and equipment, and price divided by annual diluted EPS.
   Print the numeric arithmetic and save it in `metrics.json`.
6. Print verbatim quarter revenue and diluted EPS evidence from Exhibit 99.1,
   including context and GAAP/non-GAAP labels. Prefer opening narrative totals;
   fall back to quarter-labelled tables. The deterministic matcher can miss unusual
   wording; missing evidence is reported explicitly and the complete text is retained.
7. Save and print each peer's same nine concepts and annual operating margin
   (`OperatingIncomeLoss / total revenue`). Each peer uses its own latest fiscal
   year, with dates shown; this is not a synchronized calendar-year comparison.

## Financial selection and limits

Candidate tags are compared by their newest available annual period end, rather
than taking the first tag that exists. When revenue candidates have the same
latest annual period, `Revenues` takes precedence over contract revenue and net
sales because those narrower disclosures can exclude other revenue sources.
The exact-period prior-year fallback uses the same semantic preference. A newer
contract-revenue tag still supersedes an obsolete `Revenues` tag; values are never
ranked by size. Annual duration facts must cover 330–400
days and come from 10-K/10-K/A filings. Balance-sheet facts must be instants on
the revenue fiscal year-end. Current and comparative duration facts must match
revenue's exact start/end dates. Comparative `fy` fields describe the filing's
fiscal year, so they are not used to match the prior year. The latest filed
disclosure wins within a period (including amendments/restatements). A predecessor
tag may supply a missing period only when its dates match exactly; its actual tag
is retained. No metric silently substitutes a different fiscal year.

The tag map covers standard US-GAAP concepts. IFRS and company-specific extension
tags are not mapped. Missing tags or periods print N/A; missing current debt is
never assumed zero. The debt ratio excludes short-term borrowings unrelated to
the current portion of long-term debt and does not mix in combined debt/lease tags.
Negative/zero equity denominators and nonpositive EPS produce N/A. Revenue and
margin growth with a zero denominator also produce N/A. Cash capital expenditure
uses the reported positive payment amount without silently taking an absolute value.
Fiscal transition years outside the annual duration window are unsupported.

Source files, selected facts, calculations, release evidence, peer data, loaded
filing history, and run metadata are saved together. The submissions recent block
is loaded first; the saved filing history covers the blocks actually searched,
not necessarily the issuer's entire lifetime. Generated data and bytecode are
ignored by Git; the existing `.env` is unchanged.

The requested design reference is Francis & Zhang (2026),
[Can AI match professional analysts? Evidence from a multi-agent system](https://doi.org/10.1016/j.frl.2026.110127).
The publisher and SSRN blocked full-text access during implementation, so Figure 1
was not independently inspected. The user's explicit four-role responsibilities
and seven-step specification govern this implementation.
SEC endpoints and fair-access handling follow the
[SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
and [developer resources](https://www.sec.gov/about/developer-resources).

## Four roles after the seven steps

Every call uses Purdue's documented OpenAI-compatible endpoint
`https://genai.rcac.purdue.edu/api/chat/completions`, model `gpt-oss:120b`, and
the vLLM `response_format` JSON schema interface. See
[Purdue's API documentation](https://docs.rcac.purdue.edu/services/genai/api/) and
[hosted model documentation](https://docs.rcac.purdue.edu/services/genai/models/).
The program prints each role's name as it runs. Each role gets its own system
instructions and a fresh JSON brief with exactly `task`, `context`, and
`expected_output`; conversations and filesystem access are never shared.

1. **Strategy Manager** makes delegation briefs. The orchestrator enforces which
   evidence can enter each role's context, regardless of the generated brief.
2. **Research Analyst** receives the annual Business and MD&A sections and the
   earnings release, with original text line numbers and filing metadata. It
   covers business model, competition, catalysts and recent developments. Every
   claim selects a supplied text evidence ID; Python attaches the full verbatim
   source line and its line number. Section extraction chooses the longest
   Item span to avoid table-of-contents entries; unusual headings can remain missing
   and must be disclosed. Original source text is retained for inspection.
3. **Financial Analyst** receives the saved step 4 facts, step 5 calculations,
   step 6 quarter evidence and step 7 peers through an ID catalog, plus definition
   excerpts and the release. It distinguishes definitions such as total revenue
   versus net sales and GAAP versus adjusted EPS. Financial numbers are rendered
   by Python from cited evidence IDs with accession, tag, unit, exact period,
   and arithmetic. The user-supplied price is explicitly identified as an input,
   with the run time, rather than as a filing number or fetched quote. Additional
   ratios use a bounded calculator request round: whitelisted operations on known
   fact IDs, with unit/period checks; no model-supplied code or numerical operands.
4. **Investment Advisor** receives only the two analysts' outputs and their cited
   facts/calculations, never the filings or uncited financial catalog. It supplies
   a thesis, Buy/Hold/Sell, and one deciding calculator evidence ID; Python renders
   that number with its inputs and arithmetic.
5. **Strategy Manager** reviews presence, coverage, grounding and consistency of
   Overview; Market Positioning and Growth Outlook; Valuation Insights; Future
   Developments and Strategic Recommendations; and Investment Recommendation.
   It returns failing parts to their owners in a single correction pass, refreshes
   the advisor when upstream analysis changes, and checks again before compiling.

Each specialist block contains only `text` and `evidence_ids`. Its JSON schema
enumerates the IDs actually supplied to that call. Specialists cannot submit
`source`, `line`, `quote`, filing metadata, or replacement evidence payloads.
Unknown IDs are also rejected locally. The advisor's schema is restricted to
records cited in the two analysts' outputs, including their calculator inputs.
Unsupported analyst blocks are removed from the Advisor's input projection;
the raw outputs remain available to the manager for review and one correction.
The Advisor separately cites `thesis_evidence_ids` and `reason_evidence_ids`;
the deciding number and named supporting metrics must be available in that scope.

`desk_evidence.py` constructs the shared catalog from saved files, without network
access. Text record IDs include a source-content fingerprint and original line
number; source IDs include the filing form, accession and saved filename. The same
saved source produces the same IDs. MD&A remains part of its actual annual filing;
it is not a separate model-created source. Text, XBRL facts, user inputs, calculator
results, missing inputs and quarter-evidence bundles have distinct record kinds.
Step 6 bundles verify the saved match against the release, then include actual
source lines with date/column/unit context. A stale match fails explicitly.

Prose uses qualitative language; full quotations, facts and calculations are
attached by Python from canonical records. XBRL evidence requires accession, tag
and period, without an invented filing line. Price comes from `run.json`'s input
record, with the time it was supplied, without implying a market observation time.
Calculator evidence retains its arithmetic and references to every input record.
Missing inputs remain N/A. No `CLI` or `metrics` pseudo-document is needed.

`claim_validation.py` adds deterministic claim contracts. Peer comparisons are
Python records computed from the subject and peer values, with higher/lower/tied
groups, missing/incompatible observations and the original fiscal-period scope.
Models may select their exact statement or use neutral prose and let Python print
it. They cannot author rankings or comparative metric assertions. Segment claims
must reproduce an explicit filing sentence identifying segments, so category or
channel lists cannot be relabelled. Causal and acceleration language must reproduce
an actual supplied filing sentence; positive contribution or growth alone does not
support a stronger causal or acceleration claim. These are conservative extractive
checks, not a general natural-language entailment engine.

Source identifiers (`10-Q`, including Unicode hyphens, accessions, supplied IDs,
tags and periods) are metadata, not model-computed financial numbers. Other financial
numerals must be rendered from deterministic records or exact source text. Ratio
validation checks input provenance and reproduces arithmetic using unrounded source
values, accounting for the four-decimal display arithmetic in Step 5. It does not
require a separate narrative sentence in a filing to describe the computed ratio.

`limitations` is a list of supplied inventory limitation IDs, not arbitrary prose.
Python renders the limitation and its extraction scope. Required gaps (including
market share, quarterly operating cash flow and missing requested inputs) must be
selected by the responsible role. This never asserts that the issuer omitted a
disclosure from its filings. Available quarter evidence cannot be called missing.

Before each manager review, Python revalidates each role against the exact evidence
snapshot supplied to it, including attached record integrity. The manager receives
the citation audit, permitted IDs, canonical records and their source metadata.
Mechanical failures override a model's passing review. Interpretation,
alternative-definition coverage and consistency also
depend on the manager's model review and merit human review. Unsupported/missing
source evidence is never repaired with remembered model knowledge.
Reviewer findings use explicit issue codes. Requests for separate ratio narrative,
integrated limitation placement or additional deciding metrics contradict the
contract; they are retained as `policy_exclusions` rather than treated as failures.
Actual claim, provenance and consistency failures remain blocking. The review brief
deduplicates records into one catalog, preserving all analysis and evidence.

The report is saved as `report.md` inside that run's folder, and its absolute path
is printed. The first line lists filings read and their accessions, including the
XBRL filings behind the subject and peers. The second line contains the run time
and timezone. Requests, responses, specialist outputs and both manager reviews are
saved alongside it for inspection. `evidence_catalog.json` preserves the canonical
records; `role_evidence_scopes.json` records permitted IDs, and individual request
artifacts preserve the exact records supplied on every call. If a part still fails after its single revision,
the compiled report retains its generated analysis unchanged for the Lab 03 human
audit. The section and each block are marked **HUMAN AUDIT REQUIRED**, with the full
section findings displayed and preserved in `review_final.json`. Recommendation,
thesis and deciding-number reason are also retained and flagged when their section
fails. Flags apply to the failed section's blocks; they do not assert every claim
is wrong. Deterministic evidence is displayed separately, without revising model
prose. Unknown, unsupplied or altered evidence is explicitly labelled unvalidated;
no source is invented or silently repaired. Missing peers or price are disclosed; calculations
with unavailable operands stay N/A. Inputs exceeding the desk's conservative
context budget fail explicitly without silently truncating source evidence.

Purdue documents JSON `null` as a possible rate-limit response; null and retryable
HTTP/network failures receive bounded backoff. The desk checks response completion
and JSON structure and reports errors without printing the API key.

Run offline regression tests:

```powershell
python -m unittest -v
```

The citation regressions use the issuer-neutral excerpts in
`tests/fixtures/lab03_citation_failures.json` to reproduce the failed Lab 03 run's
Unicode-hyphen substitutions, ellipsis quotations, competition-heading line
mistake, invented `mda`/`CLI`/`metrics` references and out-of-range release line.
They also cover advisor isolation, tampered provenance/arithmetic, missing inputs,
stale quarter evidence, and the unchanged single correction pass. Tests run offline.
`tests/fixtures/lab03_claim_failures.json` adds the second run's false peer-margin
comparison, segment relabelling, causal/trend inferences and reviewer false positives.

To audit an existing run without any API calls or changes to its artifacts:

```powershell
python audit_saved_run.py "research_data/your-run-folder" --output research_data/offline_validation/audit.json
```

The audit uses the exact saved briefs, checks original records against reconstructed
canonical evidence, validates arithmetic and claims, and records old review findings
that contradict the current contract. It does not silently upgrade old free-form
limitations or missing Advisor support fields to the new schema.
