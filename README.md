# 10-K Risk Factor Summarizer

Pulls the latest 10-K filings from SEC EDGAR, it then extracts **Item 1A. Risk Factors**, and uses the Claude API to rank each company's top 10 risks. It ranks them into a fixed set of categories, then compares companies and sectors side by side.

**First full run (Oct 2026):** 20 financial companies across insurance, banking, and asset management · 269,455 words of risk disclosures · about 3.5 minutes end to end.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key"
python3 risk_summarizer.py --file companies.csv   # full list (ticker,sector)
python3 risk_summarizer.py CINF FITB              # or a quick test
```

Failed filings are skipped and listed in the report; finished companies are cached, so a rerun only redoes the failures.

## How it works

1. **Find the filing.** It converts each ticker to its SEC CIK and pulls the most recent 10-K from the EDGAR submissions API.
2. **Extract Item 1A.** It then strips the HTML and finds the Risk Factors section with regex rules tried from strictest to loosest, ending at Item 1B (Unresolved Staff Comments).
3. **Summarize.** It sends each section to Claude with a prompt that returns structured JSON: the 10 most material risks, each with a category from a fixed list, a 1–5 severity, and a plain-English summary.
4. **Compare.** Lastly, it builds company and sector matrices with pandas, and a second Claude call writes a cross-company comparison.

## Output (`output/`)

| File | What it is |
|---|---|
| `report.md` | Written comparison across sectors and companies, sector matrix, and any failures |
| `risks.csv` | Every extracted risk: ticker, category, severity (1–5), summary |
| `by_company.csv` | Count of risks per category per company |
| `by_sector.csv` | % of companies in each sector listing each risk category |
| `raw/<TICKER>_risk_factors.txt` | The raw extracted section, for checking the model's work |

## Results

### Accuracy check (Cincinnati Financial)

I read CINF's Item 1A myself, wrote my own top 5 risks, then compared them with the model's output.

- **Coverage:** 2 of my top 5 matched exactly and the other 3 were a partial match; the severity agreed on 3 of 5.
- **Facts:** Both figures the model cited were in the filing (58.5% of the portfolio in bonds and $632M modeled loss from a 1-in-100-year hurricane). However, the summary did drop the condition "net of reinsurance and taxes."
- **Miss:** It left out equity market risk, even though common stock is 40% of CINF's investment portfolio. This is important because most insurance companies have large sums of money tied into investments.
- **Fix:** The original category list had no place for equity risk, so the model put investments into interest-rate risk. Adding an "Investment / Equity Market" category helped fix this problem. The next run listed "Stock market declines hurting investment portfolio" and dropped a duplicate climate item.

### Extraction problems found and fixed

10-K formatting varies more than expected. Each fix was checked against word counts across all 20 companies.

| Problem | Fix |
|---|---|
| CINF's heading renders as "I TEM 1 A" in plain text | Fallback rule: "Risk Factors" followed by a capitalized sentence (a heading, not a cross-reference) |
| Risk factors that mention "Item 1C. Cybersecurity" were cut short (SCHW at 111 words, HBAN at 4,633) | End at Item 1B first; HBAN went to 12,793 words |
| Some sections ran past the end (TRV at 34,032 words) | Same fix; TRV dropped to 12,073 |
| USB incorporates its risk factors by reference from the Annual Report | Detected and reported clearly; replaced with Truist (TFC) |

<!-- Add a chart of by_sector.csv here -->

## Limitations

- 10-K formatting varies, so section extraction can fail on some filings; failures are logged and skipped.
- Filings that incorporate risk factors by reference from an Annual Report exhibit (e.g. USB) aren't supported yet.
- LLM output needs a human check: it can miss risks, rate severity inconsistently, drop conditions from figures, and word its summaries differently from run to run.

## Built with

Python · pandas · BeautifulSoup · regex · SEC EDGAR API · Claude API (Haiku)
