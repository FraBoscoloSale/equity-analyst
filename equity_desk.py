"""Seven-step SEC research data pipeline. No model calls or issuer-specific rules."""

import argparse
import json
import math
import re
import sys
import time
from io import BytesIO
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import dotenv_values
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
ANNUAL_FORMS = {"10-K", "10-K/A"}
# These debt concepts exclude the current portion; it is added separately.
CONCEPTS = {
    "revenue": ("Revenue", "duration", [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues", "SalesRevenueNet",
    ]),
    "operating_income": ("Operating income", "duration", ["OperatingIncomeLoss"]),
    "net_income": ("Net income", "duration", ["NetIncomeLoss", "ProfitLoss"]),
    "diluted_eps": ("Diluted EPS", "duration", ["EarningsPerShareDiluted"]),
    "equity": ("Stockholders' equity", "instant", ["StockholdersEquity"]),
    "long_term_debt": ("Long-term debt, noncurrent", "instant", [
        "LongTermDebtNoncurrent", "LongTermDebt",
    ]),
    "current_debt": ("Current portion of long-term debt", "instant", [
        "LongTermDebtCurrent", "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
    ]),
    "cash_from_operations": ("Cash from operations", "duration", [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ]),
    "capex": ("Additions to property and equipment", "duration", [
        "PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ]),
}


class DeskError(Exception):
    """A problem that can be reported without a traceback or credentials."""


class SECClient:
    """All SEC traffic goes through this throttled, identified session."""

    def __init__(self, user_agent):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.last_request = 0.0
        self.cache = {}

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        if urlparse(url).hostname not in {"www.sec.gov", "data.sec.gov", "sec.gov"}:
            raise DeskError(f"Unexpected document host: {url}")
        for attempt in range(4):
            time.sleep(max(0, 0.22 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=(15, 90))
            except requests.RequestException as exc:
                if attempt == 3:
                    raise DeskError(f"SEC request failed for {url}: {type(exc).__name__}") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                retry = response.headers.get("Retry-After", "")
                time.sleep(min(float(retry), 60) if retry.isdigit() else 2 ** (attempt + 1))
                continue
            if not response.ok:
                raise DeskError(f"SEC returned HTTP {response.status_code} for {url}. "
                                "Check connectivity and SEC fair-access restrictions.")
            self.cache[url] = response
            return response
        raise DeskError(f"SEC request failed for {url}")

    def json(self, url):
        try:
            return self.get(url).json()
        except ValueError as exc:
            raise DeskError(f"SEC returned invalid JSON for {url}") from exc

    def text(self, url):
        response = self.get(url)
        # EDGAR HTML is usually UTF-8; don't accept requests' default Latin-1.
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def save_document(client, url, output, stem):
    response = client.get(url)
    if response.content.startswith(b"%PDF"):
        (output / f"{stem}.pdf").write_bytes(response.content)
        try:
            text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages)
        except Exception as exc:
            raise DeskError(f"Could not convert PDF {url}: {type(exc).__name__}") from exc
        if not text.strip():
            raise DeskError(f"PDF {url} has no extractable text (OCR would be required).")
    else:
        html = client.text(url)
        (output / f"{stem}.html").write_text(html, encoding="utf-8")
        text = plain_text(html)
    path = output / f"{stem}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def plain_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(["script", "style", "head", "noscript"]):
        node.decompose()
    for node in list(soup.find_all(lambda tag: tag.name in {"ix:header", "ix:hidden"})):
        node.decompose()
    # Keep table rows together so labels, periods and values remain inspectable.
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if cells:
            row.replace_with("\n" + " | ".join(c.get_text(" ", strip=True) for c in cells) + "\n")
    for node in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "br"]):
        node.insert_before("\n")
        node.insert_after("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text(" ").splitlines()]
    return "\n".join(line for line in lines if line) + "\n"


def rows_from_columns(columns):
    return [{key: values[i] for key, values in columns.items() if i < len(values)}
            for i in range(len(columns.get("accessionNumber", [])))]


def resolve(client, ticker):
    records = client.json("https://www.sec.gov/files/company_tickers.json")
    normalized = ticker.upper().replace(".", "-")
    for company in records.values():
        if company["ticker"].upper().replace(".", "-") == normalized:
            return {"ticker": company["ticker"], "cik": str(company["cik_str"]).zfill(10),
                    "name": company["title"]}
    raise DeskError(f"Ticker {ticker!r} is not in SEC company_tickers.json.")


def filing_urls(company, filing):
    accession = filing["accessionNumber"]
    base = f"https://www.sec.gov/Archives/edgar/data/{int(company['cik'])}/{accession.replace('-', '')}/"
    return base, base + accession + "-index.html"


def index_metadata(client, company, filing):
    base, index_url = filing_urls(company, filing)
    soup = BeautifulSoup(client.text(index_url), "html.parser")
    accepted = None
    for label in soup.select(".infoHead"):
        if label.get_text(strip=True).lower() == "accepted":
            value = label.find_next_sibling(class_="info")
            if value:
                accepted = value.get_text(" ", strip=True)
                break
    if not accepted:
        raise DeskError(f"Could not read the Accepted time from {index_url}")
    documents = []
    for row in soup.select("table.tableFile tr"):
        cells = row.find_all("td")
        if len(cells) >= 4 and cells[2].find("a", href=True):
            href = cells[2].find("a", href=True)["href"]
            # Archive index links normally point to raw documents, but unwrap ix viewers too.
            if "?doc=" in href:
                href = href.split("?doc=", 1)[1].split("&", 1)[0]
            documents.append({"description": cells[1].get_text(" ", strip=True),
                              "type": cells[3].get_text(" ", strip=True),
                              "url": urljoin(base, href)})
    return {**filing, "accepted": accepted, "accepted_source": index_url,
            "documents": documents,
            "document_url": urljoin(base, filing["primaryDocument"])}


def select_filings(client, company, output):
    submissions = client.json(f"https://data.sec.gov/submissions/CIK{company['cik']}.json")
    save_json(output / "submissions.json", submissions)
    history = rows_from_columns(submissions["filings"]["recent"])
    archives = sorted(submissions["filings"].get("files", []),
                      key=lambda item: item.get("filingTo", ""), reverse=True)
    selected, examined = {}, set()
    while True:
        ordered = sorted(history, key=lambda row: (row.get("filingDate", ""),
                                                   row["accessionNumber"]), reverse=True)
        for form in ("10-K", "10-Q"):
            if form not in selected:
                filing = next((row for row in ordered if row.get("form") == form), None)
                if filing:
                    selected[form] = index_metadata(client, company, filing)
        if "8-K" not in selected:
            for filing in ordered:
                if filing.get("form") != "8-K" or filing["accessionNumber"] in examined:
                    continue
                examined.add(filing["accessionNumber"])
                # Metadata is only a prefilter: verify Item 2.02 inside the document.
                if filing.get("items") and not re.search(r"\b2\.02\b", filing["items"]):
                    continue
                base, _ = filing_urls(company, filing)
                document = plain_text(client.text(urljoin(base, filing["primaryDocument"])))
                if re.search(r"\bItem\s*2\s*\.\s*02\b", document, re.I):
                    selected["8-K"] = index_metadata(client, company, filing)
                    break
        if len(selected) == 3 or not archives:
            break
        archive = archives.pop(0)
        older = client.json(f"https://data.sec.gov/submissions/{archive['name']}")
        history.extend(rows_from_columns(older))
    ordered = sorted(history, key=lambda row: (row.get("filingDate", ""), row["accessionNumber"]), reverse=True)
    save_json(output / "filing_history.json", ordered)
    print(f"Loaded {len(history)} filings; full loaded listing saved to filing_history.json.")
    for filing in ordered:
        if filing.get("form") in {"10-K", "10-Q", "8-K"}:
            print(f"  {filing.get('filingDate')} {filing['form']:4} {filing['accessionNumber']} "
                  f"items={filing.get('items', '')}")
    for form in ("10-K", "10-Q", "8-K"):
        if form not in selected:
            print(f"  {form}: NOT FOUND (8-K must report Item 2.02 in its document)")
            continue
        filing = selected[form]
        print(f"Selected {form}: {filing['accessionNumber']} | Accepted: {filing['accepted']} "
              f"(as shown on EDGAR index)\n  {filing['accepted_source']}")
    save_json(output / "filings.json", selected)
    return selected


def download_documents(client, selected, output):
    paths = {}
    for form, filename in (("10-K", "10-K"), ("10-Q", "10-Q"), ("8-K", "8-K")):
        if form not in selected:
            print(f"{form}: unavailable")
            continue
        filing = selected[form]
        paths[form] = save_document(client, filing["document_url"], output, filename)
        print(f"Saved {filename}.txt")
        if form == "8-K":
            exhibits = [doc for doc in filing["documents"]
                        if re.fullmatch(r"EX-99\.0?1", doc["type"], re.I)]
            if not exhibits:
                print("Exhibit 99.1: NOT FOUND on the selected Item 2.02 8-K; release unavailable.")
                continue
            exhibit = exhibits[0]
            paths["release"] = save_document(client, exhibit["url"], output, "earnings_release")
            filing["release_url"] = exhibit["url"]
            print(f"Saved earnings_release.txt (Exhibit 99.1): {exhibit['url']}")
    save_json(output / "filings.json", selected)
    return paths


def annual_records(taxonomy, tag, unit, kind):
    records = taxonomy.get(tag, {}).get("units", {}).get(unit, [])
    valid = []
    for record in records:
        if record.get("form") not in ANNUAL_FORMS or not record.get("end"):
            continue
        if kind == "duration":
            if not record.get("start"):
                continue
            days = (date.fromisoformat(record["end"]) - date.fromisoformat(record["start"])).days
            if not 330 <= days <= 400:
                continue
        elif record.get("start"):
            continue
        valid.append(record)
    return valid


def newest_tag(taxonomy, tags, unit, kind):
    candidates = [(tag, annual_records(taxonomy, tag, unit, kind)) for tag in tags]
    candidates = [(tag, records) for tag, records in candidates if records]
    # Candidate order only breaks ties; obsolete tags cannot outrank newer annual data.
    return max(candidates, key=lambda pair: max(row["end"] for row in pair[1]), default=(None, []))


def latest_record(records):
    return max(records, key=lambda row: (row["end"], row.get("filed", ""), row.get("accn", "")), default=None)


def match_period(records, period, kind):
    if not period:
        return None
    return latest_record([row for row in records if row["end"] == period["end"]
                          and (kind == "instant" or row.get("start") == period.get("start"))])


def fact_detail(record, tag, unit, company):
    if not record:
        return None
    return {**record, "tag": f"us-gaap:{tag}", "unit": unit,
            "filing_url": f"https://www.sec.gov/Archives/edgar/data/{int(company['cik'])}/"
                          f"{record['accn'].replace('-', '')}/{record['accn']}-index.html"}


def extract_facts(raw, company):
    taxonomy = raw.get("facts", {}).get("us-gaap", {})
    if not taxonomy:
        raise DeskError(f"{company['ticker']}: no US-GAAP company facts; IFRS/custom tags are not mapped.")
    monetary_units = {unit for tag in CONCEPTS["revenue"][2]
                      for unit in taxonomy.get(tag, {}).get("units", {}) if re.fullmatch(r"[A-Z]{3}", unit)}
    currency = "USD" if "USD" in monetary_units else next(iter(sorted(monetary_units)), None)
    if not currency:
        raise DeskError(f"{company['ticker']}: no mapped annual revenue currency.")
    revenue_tag, revenues = newest_tag(taxonomy, CONCEPTS["revenue"][2], currency, "duration")
    current = latest_record(revenues)
    if not current:
        raise DeskError(f"{company['ticker']}: no mapped full-year revenue in annual filings.")
    # Use dates, not the fy field on comparative disclosures (which labels the filing's FY).
    previous = latest_record([row for row in revenues if row["end"] < current["start"]
                              and 300 <= (date.fromisoformat(current["end"]) - date.fromisoformat(row["end"])).days <= 430])
    if not previous:
        all_revenues = [row for tag in CONCEPTS["revenue"][2]
                        for row in annual_records(taxonomy, tag, currency, "duration")]
        previous = latest_record([row for row in all_revenues if row["end"] < current["start"]
                                  and 300 <= (date.fromisoformat(current["end"]) - date.fromisoformat(row["end"])).days <= 430])
    result = {"company": company, "currency": currency,
              "fiscal_year": current.get("fy"),
              "current_period": {"start": current["start"], "end": current["end"]},
              "prior_period": {"start": previous["start"], "end": previous["end"]} if previous else None,
              "source_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{company['cik']}.json",
              "concepts": {}}
    for key, (label, kind, tags) in CONCEPTS.items():
        unit = f"{currency}/shares" if key == "diluted_eps" else currency
        tag, records = newest_tag(taxonomy, tags, unit, kind)
        values = {}
        for name, period in (("current", result["current_period"]), ("prior", result["prior_period"])):
            row = match_period(records, period, kind)
            chosen_tag = tag
            # A renamed tag may hold only the current year. Use a predecessor only for
            # the exact requested period, preserving its actual tag in the output.
            if not row and period:
                alternatives = [(candidate, match_period(annual_records(taxonomy, candidate, unit, kind), period, kind))
                                for candidate in tags]
                alternatives = [(candidate, value) for candidate, value in alternatives if value]
                if alternatives:
                    chosen_tag, row = max(alternatives, key=lambda pair: (pair[1]["end"], pair[1].get("filed", "")))
            values[name] = fact_detail(row, chosen_tag, unit, company)
        result["concepts"][key] = {"label": label, "selected_tag": f"us-gaap:{tag}" if tag else None,
                                   "latest_available_end": max((r["end"] for r in records), default=None), **values}
    return result


def show_facts(facts):
    print(f"{facts['company']['ticker']} | FY {facts['fiscal_year']} | {facts['current_period']} "
          f"| prior {facts['prior_period']} | currency {facts['currency']}")
    for concept in facts["concepts"].values():
        print(concept["label"] + ":")
        for period in ("current", "prior"):
            row = concept[period]
            if row:
                print(f"  {period:7}: {row['val']:,.4f} {row['unit']} | {row['tag']} | "
                      f"{row.get('start', 'instant')} to {row['end']} | filing {row['accn']} "
                      f"({row.get('filed')}, {row['form']})")
            else:
                print(f"  {period:7}: N/A (no mapped fact for this revenue fiscal period; "
                      f"preferred tag {concept['selected_tag']})")


def collect_facts(client, company, output, prefix=""):
    raw = client.json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{company['cik']}.json")
    save_json(output / f"{prefix}companyfacts.json", raw)
    facts = extract_facts(raw, company)
    save_json(output / f"{prefix}facts.json", facts)
    return facts


def calculate(facts, price=None, margin_only=False):
    concepts, currency = facts["concepts"], facts["currency"]
    def value(key, period="current"):
        row = concepts[key][period]
        return float(row["val"]) if row else None

    def arithmetic(label, values, formula, compute, percent=False, positive_denominator=None):
        reason = None
        if any(number is None for number in values):
            reason = "missing aligned inputs"
        elif positive_denominator is not None and positive_denominator <= 0:
            reason = "denominator must be positive"
        try:
            number = None if reason else compute(*values)
        except ZeroDivisionError:
            number, reason = None, "zero denominator"
        rendered = formula.format(*[f"{v:,.4f}" if v is not None else "N/A" for v in values])
        print(f"{label}: {rendered} = " + (f"N/A ({reason})" if number is None else
              f"{number * 100:,.2f}%" if percent else f"{number:,.4f}"))
        return {"value": number, "arithmetic": rendered, "reason": reason,
                "unit": "fraction" if percent else "multiple"}

    revenue, operating = value("revenue"), value("operating_income")
    metrics = {"operating_margin": arithmetic("Operating margin", [operating, revenue], "{} / {}",
                                              lambda a, b: a / b, percent=True)}
    if margin_only:
        return metrics
    prior_revenue = value("revenue", "prior")
    metrics["revenue_growth"] = arithmetic("Revenue growth", [revenue, prior_revenue], "({} / {}) - 1",
                                           lambda a, b: a / b - 1, percent=True)
    net, equity, prior_equity = value("net_income"), value("equity"), value("equity", "prior")
    avg_equity = (equity + prior_equity) / 2 if equity is not None and prior_equity is not None else None
    metrics["return_on_average_equity"] = arithmetic("Return on average equity", [net, equity, prior_equity],
        "{} / (({} + {}) / 2)", lambda a, b, c: a / ((b + c) / 2), percent=True, positive_denominator=avg_equity)
    metrics["debt_to_equity"] = arithmetic("Debt to equity (long-term debt including current portion)",
        [value("long_term_debt"), value("current_debt"), equity], "({} + {}) / {}",
        lambda a, b, c: (a + b) / c, positive_denominator=equity)
    metrics["free_cash_flow"] = arithmetic(f"Free cash flow ({currency})", [value("cash_from_operations"), value("capex")],
                                            "{} - {}", lambda a, b: a - b)
    metrics["free_cash_flow"]["unit"] = currency
    eps = value("diluted_eps")
    metrics["pe"] = arithmetic(f"P/E (price in {currency}; latest annual diluted EPS)", [price, eps], "{} / {}",
                               lambda a, b: a / b, positive_denominator=eps)
    return metrics


def release_lines(text):
    """Prefer reported-quarter narrative totals; fall back to quarter-labelled tables."""
    lines = text.splitlines()
    quarter = re.compile(r"\bquarter(?:ly)?\b|\bthree\s+months\b|\bQ[1-4]\b", re.I)
    annual = re.compile(r"(?:twelve|twelve[- ]|12)\s*months\s+ended|year\s+ended|full[- ](?:fiscal[- ]?)?year", re.I)
    future = re.compile(r"outlook|guidance|expects?|forecast|projected|anticipates?|next quarter|will be", re.I)
    revenue = re.compile(r"^(?:(?:the\s+)?(?:company|we)\s+(?:reported\s+)?)?"
                         r"(?:total\s+)?(?:net\s+)?(?:revenues?|sales)\b", re.I)
    eps = re.compile(r"diluted\s+(?:(?:net\s+)?(?:earnings|income|loss)\s+per\s+(?:common\s+)?share|EPS)"
                     r"|(?:earnings|income|loss)\s+per\s+(?:common\s+)?share.{0,30}diluted"
                     r"|\bper\s+diluted\s+(?:common\s+)?share\b", re.I)
    found = {"revenue": [], "diluted_eps": []}
    scope, scope_line = None, None
    for i, line in enumerate(lines):
        if annual.search(line):
            scope, scope_line = "annual", line
        elif quarter.search(line) and not future.search(line):
            scope, scope_line = "quarter", line
        # HTML paragraphs may put quarterly and annual results on the same line.
        # Check explicit quarter statements sentence by sentence, preserving the
        # complete source line for verbatim evidence. Decimal points stay intact.
        quarter_eps_statement = any(
            quarter.search(sentence) and eps.search(sentence)
            and not annual.search(sentence) and not future.search(sentence)
            for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line)
        )
        quarter_scope = scope == "quarter" and not future.search(line)
        if not quarter_scope and not quarter_eps_statement:
            continue
        clean = line.strip(" |\t\u2022\u201c\u201d\"")
        table = "|" in line
        amount = re.search(r"\$\s*\(?\d|\b\d[\d,.]*\s+(?:million|billion|trillion)\b", line, re.I)
        numeric_table = table and re.search(r"\|\s*\$?\s*\(?\d", line)
        if not amount and not numeric_table:
            continue
        revenue_match = revenue.search(clean)
        # Reject segment revenue and percentage-only revenue growth statements.
        is_revenue = quarter_scope and revenue_match and (table or re.match(
            r"\s*(?:was|were|of|totaled|totalled|:|\$|increased\s+to|grew\s+to)", clean[revenue_match.end():], re.I))
        is_eps = quarter_eps_statement or (quarter_scope and (
            eps.search(clean) or (table and re.match(r"diluted\s*\|", clean, re.I)
                                 and re.search(r"earnings per share", " ".join(lines[max(0, i-5):i]), re.I))))
        for key, matches in (("revenue", is_revenue), ("diluted_eps", is_eps)):
            if not matches:
                continue
            context = lines[max(0, i - 4):min(len(lines), i + 2)]
            if scope_line and scope_line not in context:
                context = [scope_line] + context
            found[key].append({"line": i + 1, "text": line, "context": context,
                               "source_kind": "table" if table else "narrative"})
    for key, matches in found.items():
        narratives = [item for item in matches if item["source_kind"] == "narrative"]
        # The opening reported-results statement is more specific than later reconciliations.
        found[key] = narratives[:1] if narratives else matches[:3]
    return found


def show_release(paths, output):
    if "release" not in paths:
        print("N/A: selected 8-K has no convertible Exhibit 99.1 release.")
        return
    text = Path(paths["release"]).read_text(encoding="utf-8")
    matches = release_lines(text)
    for key, label in (("revenue", "Quarter total revenue"), ("diluted_eps", "Quarter diluted EPS")):
        print(f"{label} - verbatim reported-quarter evidence (retain GAAP/non-GAAP labels):")
        if not matches[key]:
            print("  No reliable quarter-labelled match; inspect earnings_release.txt.")
        for item in matches[key]:
            print(f"  [line {item['line']}] {item['text']}")
            for line in item["context"]:
                if line != item["text"]:
                    print("    " + line)
    save_json(output / "release_lines.json", matches)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", help="SEC ticker")
    parser.add_argument("--peers", nargs="*", default=[], help="Peer tickers, separated by spaces or commas")
    parser.add_argument("--price", type=float, help="Share price in the reporting currency for annual P/E")
    parser.add_argument("--step", type=int, choices=range(1, 8), help="Print/run just this step; load prerequisites quietly")
    args = parser.parse_args(argv)
    if args.price is not None and (not math.isfinite(args.price) or args.price <= 0):
        parser.error("--price must be a finite positive number")
    args.peers = list(dict.fromkeys(t.strip().upper() for entry in args.peers for t in entry.split(",") if t.strip()))
    return args


def main(argv=None):
    args = parse_args(argv)
    # Read this folder's file explicitly; no API key is needed or read.
    user_agent = dotenv_values(ROOT / ".env").get("SEC_USER_AGENT", "")
    if not user_agent or not user_agent.strip() or "\n" in user_agent or "\r" in user_agent:
        raise DeskError("Set a valid, single-line SEC_USER_AGENT in the .env beside this script.")
    client = SECClient(user_agent.strip())
    state = {}
    descriptions = {
        1: "Resolve ticker to CIK", 2: "List filings and select 10-K, 10-Q and Item 2.02 8-K",
        3: "Download filings and Exhibit 99.1 as plain text", 4: "Aligned annual XBRL facts and prior year",
        5: "Calculate financial metrics", 6: "Quarter revenue and diluted EPS from the release",
        7: "Peer facts and operating margins",
    }

    def company():
        if "company" not in state:
            state["company"] = resolve(client, args.ticker)
        return state["company"]

    def output():
        if "output" not in state:
            name = re.sub(r"[^\w.-]+", "_", company()["name"]).strip("_.")[:100] or company()["ticker"]
            stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S_%f")
            path = ROOT / "research_data" / f"{name}_{stamp}"
            path.mkdir(parents=True, exist_ok=False)
            state["output"] = path
            save_json(path / "run.json", {"company": company(), "run_time": datetime.now().astimezone().isoformat(),
                                         "step": args.step, "peers": args.peers, "price": args.price})
        return state["output"]

    def filings():
        if "filings" not in state:
            state["filings"] = select_filings(client, company(), output())
        return state["filings"]

    def documents():
        if "documents" not in state:
            state["documents"] = download_documents(client, filings(), output())
        return state["documents"]

    def facts():
        if "facts" not in state:
            state["facts"] = collect_facts(client, company(), output())
        return state["facts"]

    # A selected step still needs its data, but emits only its own numbered heading/output.
    from contextlib import redirect_stdout
    from io import StringIO
    for step in ([args.step] if args.step else range(1, 8)):
        print(f"\nStep {step}/7: {descriptions[step]}", flush=True)
        if args.step:
            with redirect_stdout(StringIO()):
                company()
                if step == 3:
                    filings()
                elif step in {4, 5}:
                    facts()
                elif step == 6:
                    documents()
        if step == 1:
            item = company()
            print(f"{item['ticker']} -> CIK {item['cik']} | {item['name']}")
            save_json(output() / "company.json", item)
        elif step == 2:
            filings()
        elif step == 3:
            documents()
        elif step == 4:
            show_facts(facts())
        elif step == 5:
            save_json(output() / "metrics.json", calculate(facts(), args.price))
        elif step == 6:
            show_release(documents(), output())
        elif step == 7:
            peers = {}
            if not args.peers:
                print("No --peers supplied; skipped.")
            for ticker in args.peers:
                print(f"\nPeer {ticker}")
                try:
                    peer = resolve(client, ticker)
                    peer_facts = collect_facts(client, peer, output(), prefix=f"{peer['ticker']}_")
                    show_facts(peer_facts)
                    metrics = calculate(peer_facts, margin_only=True)
                    peers[ticker] = {"facts": peer_facts, "metrics": metrics}
                except DeskError as exc:
                    print(f"Unavailable: {exc}")
                    peers[ticker] = {"error": str(exc)}
            save_json(output() / "peers.json", peers)
    print(f"\nSaved research data: {output()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (DeskError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
