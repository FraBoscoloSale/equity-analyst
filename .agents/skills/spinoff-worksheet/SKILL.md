---
name: spinoff-worksheet
description: Complete the Lab 07 spinoff worksheet for one ticker in an existing saved agent-team run, using the project's worksheet rules, Investment Advisor persona, filing evidence, closes reader, and one judgment call.
---

Run from the project root. The rule source is `spinoff_worksheet.txt`; the persona
is `roles/investment_advisor.txt`. Read both without rewriting them. The executable
is `spinoff_worksheet.py`, and its fictional-data tests are
`test_spinoff_worksheet.py`. The existing agent team and seeker are not run.

When the user authorizes a real worksheet, take one existing run directory:

```powershell
python -B spinoff_worksheet.py "research_data/SAVED_RUN_FOLDER"
```

The run's `run.json`, `facts.json`, `filings.json`, and original saved filing
HTML/text establish its ticker, CIK and sources. Do not use generated analyst
reports as filing facts. The default registry is `spinoff_runs/registry.json`;
`--registry PATH` selects another existing registry explicitly. Missing or
ambiguous identity/source evidence is not a company-name guess.

A registry match routes the existing SEC transport and filing-inventory reader
to the evidenced Form 10, Information Statement, and registry-cited filings.
New source snapshots and metadata are saved beneath this run's unique
`worksheet_sources/` subdirectory. No registry match means the spinoff-only
fields are `not applicable`; finish the ordinary-company worksheet. Registration
or an Information Statement alone does not prove a completed business spinoff.

The worksheet headings, required returns, owner-cash-flow formula, client size,
and conviction multiple come from the worksheet file each time. Unsupported or
ambiguous rule changes fail explicitly. Code prints one fact per line with
filing/accession, section, date and statement classification. Missing facts are
`not known`. A searched section with no established item is distinguished from
an unavailable source; neither establishes a zero amount.

Owner cash flow uses after-tax earnings plus depreciation minus capital spending
from a coherent annual period, currency and statement basis. Do not annualize a
quarter, substitute combined depreciation/amortization for depreciation, mix
dimensional facts or issuers, or compare company-level currency values directly
with a share price. Per-share values require an evidenced outstanding share count;
its date is disclosed. Historical periods remain historical even if filed after
the distribution. Pro forma figures stay labeled. No missing operand is estimated.

Only non-derivative Form 4 code-P acquisitions qualify as open-market purchases.
Preserve their transaction section and filing evidence. Partial coverage and
unreconciled amendments remain unknown rather than producing a zero purchase count.

The existing reader resolves the exact export Instrument. Its separate read-only
volume API preserves missing/conflicting volumes, and its currency API reads only
an explicit Currency column. Raw price/volume histories never enter the model.
Price comparison requires comparable evidenced currencies. Missing prices produce
`not computed` for comparison/view. Liquidity uses the saved annual period start
through worksheet date, discloses its observation interval, and never backfills
missing volume. The worksheet supplies no participation threshold: print the
purchase/median-dollar-volume comparison and mark execution reasonableness
`not known` rather than inventing a trading-capacity rule.

Exactly one direct judgment request uses the unmodified persona with the existing
configured model and Purdue endpoint. Give it only filing-supported facts,
Python-calculated value range and price comparison. It supplies qualitative
cheapness/forced-selling reasoning and next-filing evidence that would change the
view, referencing supplied fact IDs. No calculator, team, retry, manager, verifier,
or second model call is part of this skill. Reject numeric/calculation-bearing or
unsupported judgment output rather than asking the model to repair it.

Code adds liquidity after judgment, computes the final view and HIGH/LOW exactly
from the worksheet's conviction rule, and lists all unknown required facts and
coverage gaps. Outputs are uniquely named `spinoff_worksheet_<timestamp>_<id>.md`
and a corresponding evidence JSON beside the saved run. Existing worksheets,
saved artifacts, rules, persona, registry, environment and Excel export stay intact.

Offline validation (no SEC, model provider or real workbook):

```powershell
python -B -m unittest -v test_spinoff_worksheet test_closes_reader
```
