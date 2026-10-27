# SEC equity-research data desk

Python 3.10+ data pipeline implementing the requested seven-step research workflow.
There are no model calls or issuer-specific rules. Company identities come from SEC data.

Install and run from this directory:

```powershell
python -m pip install -r requirements.txt
python equity_desk.py MSFT --peers ORCL ADBE --price 420
```

The price is an input example, not a fetched quote. Supply a price in the company's
reporting currency. Peers may be separated by spaces or commas. Without `--price`,
P/E prints N/A. Without `--peers`, step 7 reports that it was skipped.

Put `SEC_USER_AGENT="Your Name your.email@example.com"` in the `.env` beside the
script. The program reads that file directly and sends this header on all SEC
requests, with throttling and bounded retries. No API key is needed.

To run just one numbered step:

```powershell
python equity_desk.py MSFT --step 4
python equity_desk.py MSFT --step 5 --price 420
python equity_desk.py MSFT --step 7 --peers ORCL ADBE
```

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
than taking the first tag that exists. Annual duration facts must cover 330–400
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
The publication record was located, but Section 3.1's full text was inaccessible
during implementation. The explicit seven-step user specification governs this
implementation; no claim is made that the inaccessible section was independently verified.
SEC endpoints and fair-access handling follow the
[SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
and [developer resources](https://www.sec.gov/about/developer-resources).

Run offline regression tests:

```powershell
python -m unittest -v
```
