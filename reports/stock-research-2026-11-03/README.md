# Stock research snapshot — 2026-11-03

- Run ID: `ee58d78a96b64ba8a1c133e6fb76f1e6`
- Provider: Hithink Finance
- Price data window: 2016-11-03 to 2026-11-02
- 25 companies fetched; 18 received a composite score.
- Files: `report.html`, `result.json`, `companies.csv`, and `factor-history.csv`.
- `../data/stock-research-2026-11-03/raw/` contains the corresponding provider response JSON files used by this run.

## Important limitations

- The CSI 300 benchmark feed begins in 2022-01, so quarterly signals begin on 2022-03-31 and the first simulated purchases occur in 2022. This is not a full-period invested 10-year backtest.
- The fixed 25-company universe has survivorship bias. The historical financial statements are approximate point-in-time data because amended vintages are unavailable.
- The CSI 300 benchmark is price return only and excludes constituent dividends. Financial-sector scores are unavailable where specialized data is missing; machine-learning challengers were not run because a point-in-time training panel was not present.
- This snapshot contains public market/fundamental response data and derived analysis. It excludes API credentials, environment files, SQLite databases, and local user ledgers.
