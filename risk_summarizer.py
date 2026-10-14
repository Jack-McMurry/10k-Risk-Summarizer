"""
10-K Risk Factor Summarizer
---------------------------
Pulls the latest 10-K for each ticker from SEC EDGAR, extracts the
"Item 1A. Risk Factors" section, and uses the Claude API to turn it into
a structured list of risks (category, severity, one-line summary).
Then it compares risk categories across companies.

Usage:
    python risk_summarizer.py --file companies.csv      # run a whole list
    python risk_summarizer.py CINF FITB NTRS ALL        # or type tickers

companies.csv has two columns: ticker,sector

Outputs (in ./output):
    risks.csv              every risk, one row each
    by_company.csv         category x company matrix (count of risks)
    by_sector.csv          category x sector matrix (% of companies listing it)
    report.md              written comparison, plus any companies that failed
    raw/<TICKER>_risk_factors.txt   the extracted section (for checking)
    cache/<TICKER>.json    saved results, so reruns skip finished companies
"""

import argparse
import json
import os
import re
import sys
import time
import warnings
from functools import lru_cache

import pandas as pd
import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Modern 10-Ks are "inline XBRL": HTML with XML data tags mixed in. Parsing them
# as HTML works fine, so silence the warning BeautifulSoup prints about it.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ---- Settings you should change ---------------------------------------------
# The SEC requires a User-Agent with your name and email or it blocks requests.
SEC_USER_AGENT = "Jack McMurry mcmurrja@miamioh.edu"
MODEL = "claude-haiku-5-5"   # cheap and fast; try "claude-sonnet-5-5" to compare quality
OUTPUT_DIR = "output"
# ------------------------------------------------------------------------------

HEADERS = {"User-Agent": SEC_USER_AGENT}

CATEGORIES = [
    "Market / Interest Rate",
    "Investment / Equity Market",
    "Credit",
    "Liquidity / Capital",
    "Regulatory / Legal",
    "Cybersecurity / Technology",
    "Operational",
    "Catastrophe / Climate",
    "Competition / Strategy",
    "Macroeconomic",
    "Other",
]


# ---------- Step 1: get the 10-K from SEC EDGAR -----------------------------

@lru_cache(maxsize=1)
def sec_ticker_list() -> dict:
    """Download the SEC's ticker-to-CIK list once and reuse it for every company."""
    url = "https://www.sec.gov/files/company_tickers.json"
    return requests.get(url, headers=HEADERS, timeout=30).json()


def ticker_to_cik(ticker: str) -> str:
    """Look up the company's CIK (SEC id) from its ticker."""
    for row in sec_ticker_list().values():
        if row["ticker"].upper() == ticker.upper():
            return str(row["cik_str"]).zfill(10)
    raise ValueError(f"Ticker not found: {ticker}")


def latest_10k_url(cik: str) -> tuple[str, str]:
    """Return (document URL, filing date) of the most recent 10-K."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    recent = requests.get(url, headers=HEADERS, timeout=30).json()["filings"]["recent"]
    for form, acc, doc, date in zip(
        recent["form"], recent["accessionNumber"], recent["primaryDocument"], recent["filingDate"]
    ):
        if form == "10-K":
            acc_nodash = acc.replace("-", "")
            return (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{doc}",
                date,
            )
    raise ValueError(f"No 10-K found for CIK {cik}")


def download_text(url: str) -> str:
    """Download the 10-K HTML and convert it to plain text."""
    html = requests.get(url, headers=HEADERS, timeout=60).text
    text = BeautifulSoup(html, "lxml").get_text(" ")
    return re.sub(r"\s+", " ", text)


# ---------- Step 2: pull out Item 1A. Risk Factors --------------------------

# Where Risk Factors ends: Item 1B is the most reliable marker, because risk
# factors often MENTION "Item 1C. Cybersecurity" or "Item 2" in passing.
END_PRIMARY = r"item\s*1b\.?\s*[-–—:]?\s*unresolved|unresolved\s+staff\s+comments"
END_FALLBACK = r"item\s*1c\.?\s*[-–—:]?\s*cybersecurity|item\s*2\.?\s*[-–—:]?\s*properties"


def longest_section(text: str, start_pattern: str, flags=re.I) -> str:
    """Try every start match and keep the longest stretch up to the next end marker."""
    best = ""
    for m in re.finditer(start_pattern, text, flags):
        rest = text[m.end():]
        end = re.search(END_PRIMARY, rest, re.I) or re.search(END_FALLBACK, rest, re.I)
        if end and end.start() > len(best):
            best = rest[: end.start()]
    return best


def extract_risk_factors(text: str) -> str:
    """
    Find Item 1A. Risk Factors. "Risk Factors" shows up three ways in a 10-K:
      table of contents:  "Item 1A. Risk Factors 30 Item 1B..."   (a page number follows)
      cross-reference:    "see Item 1A, Risk Factors, for..."     (a comma or lowercase word follows)
      the real heading:   "Risk Factors Our business involves..." (a capitalized word follows)
    So we try patterns from strictest to loosest and stop at the first that works.
    """
    patterns = [
        # 1. "Item 1A. Risk Factors" followed by a sentence (most filings: FITB, SCHW)
        (r"item\s*1a\.?\s*[-–—:]?\s*risk\s+factors\s+(?=[A-Z])", re.I),
        # 2. Just "Risk Factors" followed by a sentence (filings like CINF: "I TEM 1 A ." headings)
        (r"(?:Risk Factors|RISK FACTORS)\s+(?=[A-Z])", 0),
        # 3. Last resort: any "Item 1A Risk Factors" (filings like JPM, TROW whose heading
        #    isn't followed by a capitalized sentence); longest-section logic picks the real one
        (r"item\s*1a\.?\s*[-–—:]?\s*risk\s+factors", re.I),
    ]
    best = ""
    for pattern, flags in patterns:
        best = longest_section(text, pattern, flags)
        if len(best.split()) >= 1000:
            return best

    if re.search(r"item\s*1a.{0,300}incorporated.{0,60}by\s+reference", text, re.I | re.S):
        raise ValueError(
            "Risk Factors are incorporated by reference from the Annual Report exhibit, "
            "not written in the 10-K itself (not supported yet)"
        )
    raise ValueError(
        f"Could not find the Risk Factors section (longest match was {len(best.split())} words). "
        f"Check raw/<TICKER>_FULL_TEXT.txt"
    )


# ---------- Step 3: summarize with Claude -----------------------------------

client = Anthropic()  # reads your ANTHROPIC_API_KEY environment variable


def response_text(response) -> str:
    """
    A response is a list of content blocks. Some models return a "thinking"
    block (their reasoning) before the answer, so keep only the text blocks.
    """
    if response.stop_reason == "max_tokens":
        print("WARNING: response was cut off; raise max_tokens")

    return "".join(block.text for block in response.content if block.type == "text")

SUMMARY_PROMPT = """You are a financial analyst. Below is the "Risk Factors" section of {company}'s 10-K.

Identify the 10 most material risks. Return ONLY a JSON list, no other text, where each item has:
- "risk": a short name (max 8 words)
- "category": one of {categories}
- "severity": 1-5 (5 = could seriously hurt earnings or solvency)
- "summary": one plain-English sentence a non-expert could understand

Risk Factors section:
<section>
{section}
</section>"""


def summarize_risks(company: str, section: str) -> list[dict]:
    response = client.messages.create(
        model=MODEL,
        max_tokens=6000,
        messages=[{
            "role": "user",
            "content": SUMMARY_PROMPT.format(
                company=company, categories=CATEGORIES, section=section[:300_000]
            ),
        }],
    )
    raw = response_text(response)
    raw = raw[raw.find("["): raw.rfind("]") + 1]  # strip anything around the JSON
    return json.loads(raw)


COMPARE_PROMPT = """Here are the top risks extracted from the latest 10-K filings of {n} companies, grouped by sector:

{table}

Write a comparison (under 500 words) for an analyst:
1. Which risks are shared across nearly all companies?
2. How do the sectors differ from each other?
3. Within each sector, which companies stand out, and why?
4. Which 3 companies look most exposed overall, and why?"""


def compare_companies(df: pd.DataFrame) -> str:
    table = df.sort_values(["sector", "ticker"]).to_csv(index=False)
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": COMPARE_PROMPT.format(n=df["ticker"].nunique(), table=table)}],
    )
    return response_text(response)


# ---------- Run it -----------------------------------------------------------

def process_company(ticker: str) -> dict:
    """Run steps 1-4 for one company. Results are cached so a rerun skips it."""
    cache_file = f"{OUTPUT_DIR}/cache/{ticker}.json"
    if os.path.exists(cache_file):
        print(f"{ticker}: using saved results")
        with open(cache_file) as f:
            return json.load(f)

    cik = ticker_to_cik(ticker)
    url, filed = latest_10k_url(cik)
    text = download_text(url)
    with open(f"{OUTPUT_DIR}/raw/{ticker}_FULL_TEXT.txt", "w") as f:
        f.write(text)  # the whole 10-K as plain text, for debugging the extractor
    section = extract_risk_factors(text)
    words = len(section.split())
    with open(f"{OUTPUT_DIR}/raw/{ticker}_risk_factors.txt", "w") as f:
        f.write(section)
    result = {"ticker": ticker, "filed": filed, "url": url, "words": words,
              "risks": summarize_risks(ticker, section)}
    with open(cache_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"{ticker}: 10-K filed {filed}, {words:,} words of risk factors")
    time.sleep(0.5)  # the SEC allows 10 requests a second; stay well under
    return result


def load_companies(args) -> dict:
    """Return {ticker: sector} from --file or from tickers typed on the command line."""
    if args.file:
        df = pd.read_csv(args.file)
        return dict(zip(df["ticker"].str.upper().str.strip(), df["sector"].str.strip()))
    return {t.upper(): "Unspecified" for t in args.tickers}


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize and compare 10-K risk factors.")
    parser.add_argument("tickers", nargs="*", help="tickers, e.g. CINF FITB")
    parser.add_argument("--file", help="CSV with columns ticker,sector")
    args = parser.parse_args()
    companies = load_companies(args)
    if not companies:
        parser.error("give tickers or --file companies.csv")

    for d in ("raw", "cache"):
        os.makedirs(f"{OUTPUT_DIR}/{d}", exist_ok=True)
    start = time.time()
    rows, failed = [], {}

    for i, (ticker, sector) in enumerate(companies.items(), 1):
        print(f"[{i}/{len(companies)}] ", end="")
        try:
            result = process_company(ticker)
        except Exception as e:  # one bad filing shouldn't stop the whole run
            print(f"{ticker}: FAILED - {e}")
            failed[ticker] = str(e)
            continue
        for r in result["risks"]:
            rows.append({"ticker": ticker, "sector": sector, "filed": result["filed"],
                         "words_in_section": result["words"], **{k: r.get(k) for k in ("risk", "category", "severity", "summary")}})

    if not rows:
        print("Every company failed; see the errors above.")
        return

    df = pd.DataFrame(rows)
    df.to_csv(f"{OUTPUT_DIR}/risks.csv", index=False)

    by_company = df.pivot_table(index="category", columns="ticker", values="risk", aggfunc="count", fill_value=0)
    by_company.to_csv(f"{OUTPUT_DIR}/by_company.csv")

    # % of companies in each sector that list at least one risk in the category
    listed = df.drop_duplicates(["ticker", "category"])
    per_sector = df.groupby("sector")["ticker"].nunique()
    by_sector = (listed.groupby(["category", "sector"])["ticker"].nunique()
                 .unstack(fill_value=0).div(per_sector).mul(100).round(0))
    by_sector.to_csv(f"{OUTPUT_DIR}/by_sector.csv")

    report = compare_companies(df[["sector", "ticker", "risk", "category", "severity", "summary"]])
    total_words = df.drop_duplicates("ticker")["words_in_section"].sum()
    elapsed = time.time() - start
    done = df["ticker"].nunique()

    with open(f"{OUTPUT_DIR}/report.md", "w") as f:
        f.write(f"# 10-K Risk Comparison: {done} companies\n\n")
        f.write(f"Processed {total_words:,} words of risk disclosures in {elapsed:.0f} seconds.\n\n")
        f.write("## Analysis\n\n" + report + "\n\n")
        f.write("## % of companies in each sector listing the risk category\n\n" + by_sector.to_markdown() + "\n\n")
        if failed:
            f.write("## Companies that failed\n\n")
            for t, err in failed.items():
                f.write(f"- {t}: {err}\n")

    print(f"\nDone: {done} companies in {elapsed:.0f}s, {total_words:,} words of risk factors.")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}. Rerun after fixing; finished companies are skipped.")
    print(f"See {OUTPUT_DIR}/report.md")


if __name__ == "__main__":
    main()
