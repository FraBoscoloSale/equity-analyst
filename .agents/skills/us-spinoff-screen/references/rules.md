# Screening contract

Tests in `test_spinoff_seeker.py` are the executable rule contract. Change the
relevant test before its implementation. Tests block SEC/model network access
and replace the closes reader with made-up observations.

1. Dates: YYYY-MM-DD; distribution date >= start. Unknown dates stay visible as
   unranked candidates. Proposed dates are not proof that distribution completed.
2. Discovery: scan SEC quarterly/full master-index metadata from the quarter
   preceding the requested start through the run quarter by default. Seed screening
   from that limited carry-in quarter and requested-period Form 10 registrations/amendments, CERT-linked older
   registrations, unresolved registry carry-ins, and period distribution 8-K
   full-text-index links. The distribution start date is unchanged. Search hits
   only route filing reads; they never establish a fact. This lookback covers the
   registration stage preceding a distribution across quarter boundaries; it is
   not a maximum registration-to-distribution interval. Missing original Form 10
   anchors trigger issuer-specific submissions metadata lookups only after all
   initial seeds are screened using bounded filings and period CERT/distribution
   documents, and only for a positive distribution passage attributable to that
   issuer. Identity presence, an amendment/CERT alone or an unrelated 8-K cannot
   trigger recovery. Parent archive links must occur in the positive passage,
   not incidentally elsewhere in the document. Screen recovered older chains
   again before extraction. Do not broaden quarterly scans to decades. Report
   the bounded catalog and any missing anchor coverage. Inspect linked registration
   chains and all later amendments before rejecting a registrant. Read filing HTML
   indexes, directory inventories, primary filings, EX-99 information statements,
   and exhibits explicitly described as separation/distribution evidence. Do not
   download unrelated legal/credit exhibits. Folder/index disagreements leave the
   information-statement status unresolved. Complete this deterministic phase for
   every seed before any model call. Only a plausible business/share distribution
   to shareholders may enter model extraction; information-statement presence
   alone is insufficient. A missing original makes an amendment-only chain
   incomplete for any no-information-statement drop. Only positively screened
   chains can enter missing-anchor recovery or child submissions/history shards
   and requested-period child/parent
   8-Ks. Use the registry's evidenced parent CIK.
3. SEC transport: one serial client, >=0.3 seconds between request starts, HTTPS
   SEC hosts only, SEC_USER_AGENT on every request and redirect hop. Honor
   Retry-After, bounded retries, explicit failures; no parallel SEC workers.
4. Evidence: each field needs literal value, exact quotation, an actual section
   heading, filing accession/form/filer CIK/date, document and index URLs. Dates
   and amounts are normalized only from those literal values. Unsupported,
   conditional and absent facts are blank, never filled from model knowledge.
   A supported later own registration amendment supersedes older observations;
   retain their evidence. Contradictions within the latest supported date, and
   unresolved conflicts without such an amendment, remain blank. Registry retains
   competing observations and missing-field reasons.
5. A positive spinoff finding needs a parent distributing a business to its
   shareholders. Read all collected amendments and later filings before dropping
   a fund, an existing-shares registration, or a Form 10 lacking an information
   statement. A later positive finding supersedes an earlier negative finding.
   An absent information statement is established only by a successfully read
   registration document inventory, not by a failed download or a keyword miss.
6. Stages (one per valid name): registered; listing approved (positive CERT);
   trading (own 10-Q/10-K/Form 4, or completed distribution plus sourced regular
   trading commencement); in its window; past its window. Own periodic/ownership
   forms prove trading, but do not supply a missing distribution date. The window
   is distribution day+1 through distribution day+N inclusive. Past-window takes
   precedence over trading; eligibility still requires valid U.S. spinoff proof.
7. U.S. status requires a filing's explicit U.S. incorporation/jurisdiction, not
   merely an EDGAR listing or a U.S. mailing address.
8. Prices: call existing daily_closes only in Python. Use the filing-established
   regular-way ticker and start date; reject when-issued identifiers. First price
   is the first official regular-way close strictly after distribution, and must
   match the sourced regular-way commencement date. If that cannot be established,
   no price/rank. Price/date pairs always print together. A first-week observation
   is the fifth exported regular-way daily close, within the first seven calendar
   days from commencement. Missing/gapped observations are a stated export
   completeness limitation; the export is assumed to contain its official daily
   sessions, never backfilled. Freeze first-week results until five observations
   and the week are available. No raw histories or vendor data enter any model.
9. Factor definitions: read name, direction, source/chapter, weight and N from the
   existing factor file each run. No implicit factors/weights/window defaults.
   Unsupported factors/directions fail explicitly. Current operational measures:
   small relative to parent = child assets / parent assets, with equal explicit
   dates and currencies; first-week performance = fifth close / first close - 1;
   insider buying/alignment = gross dollar value of code-P, acquisition-A Form 4
   transactions since distribution. Grants/sales are not buying; this measure
   does not establish qualitative management alignment. Missing Form 4 coverage
   does not mean zero buying. Do not sum amended or duplicate transactions.
10. Points: the factor file has no numerical scoring thresholds. Use transparent
    cross-sectional ordinal points among fully evidenced eligible names only:
    weight * number of peers strictly worse / (rankable count - 1). Ties receive
    equal points. A sole eligible name receives zero comparative points. Sum
    factor points; highest sum ranks first, CIK breaks ties. Missing any required
    ranking field/measurement means unranked, never a zero substituted for data.
11. Report: first line run date/time, price date(s), EDGAR index date(s). Index
    date is SEC Last-Modified/header evidence, never inferred from run time.
    Include all five stages, factor values/points/source/weight/direction, missing
    data and coverage limitations, and dropped names/reasons/filing evidence.
    Save unique run-time-stamped reports and a cumulative registry, preserving
    factual observations and all prior reports. Last line exactly
    `screens, not recommendations`.

SEC access reference: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
The SEC documents a maximum of 10 requests/second; the seeker uses fewer than 4.

Runtime behavior (screening rules above remain unchanged): print flushed index,
candidate, filing, document/chunk, model-call, and cache counts. Append operational
events to `spinoff_runs/logs/`. Distinguish historical identities, period screening
seeds, rejected/unresolved names, and the reduced plausible model queue. Explicit
fund/existing-share declarations and complete inventories lacking an information
statement are handled without model extraction. Ambiguous registrations stay
unresolved without model work; positive later filings can rescue them. Parent-filed
period distribution 8-K index links preserve older carry-ins without crawling the
entire historical registrant universe. Full-text index pagination is exhausted,
and saturated intervals are split; a failed search is an explicit coverage gap.
Keyword absence is never evidence of a negative. See the SEC full-text search
reference: https://www.sec.gov/edgar/search/efts-faq.html

Reuse successful extractions by exact input text, target, form, model, endpoint,
and prompt hash in `spinoff_runs/extraction_cache/`. Revalidate quotes against the
current document and attach its current filing/section provenance on every reuse.
Failed or incomplete responses are not cached. A killable model worker has a
60-second wall-clock deadline (`--extraction-timeout` overrides it), with wait
updates every 10 seconds. A failed chunk defers the rest of its document; three
consecutive failures defer further model work for that run. Every such gap remains
explicit and prevents affected names from ranking. A subsequent run retries
deferred work and reuses completed work; it does not narrow the date range.

Extraction routing: process registration versions newest first. Locate relevant
sections/passages for each registry fact, spinoff validity, stage, exclusion and
factor measurement using source headings and terms. Parse only unambiguous
explicit registry labels deterministically, subject to the same quote/section
and conditional-date validation. Send selected passages and requested fields/
finding kinds only; do not chunk the whole information statement. Context-size
bounds limit each passage's size, not the number of model calls. Earlier versions
use lower-priority sections only for items not established by a higher-priority
section; same-priority contradictions still go through evidence validation. They
fill missing facts or establish history; do not request already supported newer
fields again. Reuse unchanged passage results when their previously requested
scope covers the new request, reattaching and revalidating source citations.
Validate quotations against both passage and original document. Report selected
passage counts and source-character counts without logging source text or secrets.
