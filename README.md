# 10-K Risk Factor Summarizer

Pulls the latest 10-K filings from SEC EDGAR, extracts **Item 1A. Risk Factors**, and uses the Claude API to rank each company's top 10 risks into a fixed set of categories, then compares companies and sectors side by side.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key"
python3 risk_summarizer.py --file companies.csv   # full list (ticker,sector)
python3 risk_summarizer.py CINF FITB              # or a quick test
```

Failed filings are skipped and listed in the report; finished companies are cached, so a rerun only redoes the failures.

## Output (`output/`)

| File | What it is |
|---|---|
| `report.md` | Written comparison across sectors and companies, sector matrix, and any failures |
| `risks.csv` | Every extracted risk: ticker, category, severity (1–5), summary |
| `by_company.csv` | Count of risks per category per company |
| `by_sector.csv` | % of companies in each sector listing each risk category |
| `raw/<TICKER>_risk_factors.txt` | The raw extracted section, for checking the model's work |

## Results

_Add your chart, a sample of the report, and how well the output matched your own reading of one filing._

## Limitations

- 10-K formatting varies, so section extraction can fail on some filings.
- LLM output needs a human check: it can miss risks or rate severity inconsistently.
