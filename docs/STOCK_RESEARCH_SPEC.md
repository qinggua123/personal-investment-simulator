# Historical Stock Research Model Specification

**Status:** implementation baseline
**Date:** 2026-11-02
**Research pool:** union of the existing 16-stock priority pool and 9 additional companies (25 total)

## Objective

Build a reproducible research module inside the personal investment simulator. It extracts financial statements, prices and corporate actions; calculates explainable quality, growth, valuation and risk factors; compares a transparent score with optional machine-learning challengers; and evaluates rules using historical, point-in-time data. The module is research-only. It must not create orders, write to the paper portfolio, or change any instrument's trading approval.

## Universe

The initial fixed research universe is:

| Code | Company | Sector model |
|---|---|---|
| 000333.SZ | 美的集团 | industrial / consumer |
| 002001.SZ | 新和成 | industrial / consumer |
| 002690.SZ | 美亚光电 | industrial equipment |
| 600660.SH | 福耀玻璃 | industrial / consumer |
| 600690.SH | 海尔智家 | industrial / consumer |
| 600900.SH | 长江电力 | utility |
| 600941.SH | 中国移动 | telecom |
| 600036.SH | 招商银行 | bank |
| 601288.SH | 农业银行 | bank |
| 601398.SH | 工商银行 | bank |
| 601939.SH | 建设银行 | bank |
| 601988.SH | 中国银行 | bank |
| 601318.SH | 中国平安 | insurer |
| 600023.SH | 浙能电力 | utility |
| 600377.SH | 宁沪高速 | transport infrastructure |
| 601866.SH | 中远海发 | shipping / finance |
| 600406.SH | 国电南瑞 | power equipment |
| 002028.SZ | 思源电气 | power equipment |
| 002837.SZ | 英维克 | thermal management |
| 601869.SH | 长飞光纤 | optical network |
| 300308.SZ | 中际旭创 | optical network |
| 600845.SH | 宝信软件 | industrial software / IDC |
| 300383.SZ | 光环新网 | IDC / cloud |
| 688111.SH | 金山办公 | software |
| 688777.SH | 中控技术 | industrial automation |

Universe membership is versioned per run. It is not an order whitelist.

## Data and time discipline

- Use Hithink remote financial statement, daily price, index history and corporate-action endpoints. Keep the exact request IDs, requested windows, source, response coverage, fetched-at time and raw response cache for reproducibility.
- Pull up to ten years. Fetch daily prices in non-overlapping calendar-year windows and merge by date. Reject duplicate conflicting rows, invalid prices, unannounced data gaps, truncated API responses and rows after the evaluation date.
- Financial statement snapshots become usable only after the reported date. Backtests trade on the next available session after the publication date, never on the report date.
- Derive single-quarter values from cumulative Q1/Q2/Q3/annual statement values. For the fourth quarter, annual cumulative less the first three quarters. TTM factors require four complete quarters.
- Hithink does not expose prior amended versions in this workflow. Mark the panel `approx_point_in_time`; do not claim a fully survivorship-free or vintage-perfect database. Companies are evaluated only after listing and data availability. Report period coverage and incomplete windows.
- Historical valuation is derived from contemporaneous raw close, statements and the period's EPS/share data where possible. If shares, EPS or a required denominator are missing/non-positive, the ratio is missing; never substitute zero. Mark the derived P/E and P/B as approximations.
- Use raw daily prices plus cash dividend and bonus-share events to reconstruct a total-return index. Preserve a price-return series separately. If an event is missing or cannot be reconciled, mark the interval incomplete and exclude it from an asserted total-return result.

## Factor model

All inputs are retained alongside calculated values. Cross-sectional factors use percentile ranks and direction is explicit: higher is better or lower is better. With fewer than 20 comparable companies in a peer group, use the wider research universe and display that scope.

### Explainable score

- **Quality (35%)**: DuPont ROE and its components, operating cash flow / net profit, free-cash-flow margin and gross margin. ROIC is reserved until a consistently comparable tax and invested-capital denominator is available. The current Hithink statement response does not expose non-recurring profit, government-subsidy, investment/fair-value contribution or share-compensation line items for this historical panel. These are reported as missing and are not assigned zero or silently treated as core profit.
- **Growth (25%)**: TTM revenue/profit year-over-year growth, three-year TTM CAGR when both endpoints are positive, gross-margin trend and per-share earnings trend. Growth is not extrapolated from a single unusually strong year.
- **Valuation (25%)**: historical estimated P/E, P/B, P/S, positive-cash-flow P/CF and FCF yield for non-financial companies. Current provider valuation snapshots are shown separately and are not used in historical signals. A low multiple cannot offset deteriorating fundamentals by itself.
- **Risk (15%)**: total-debt proxy / assets, receivables / assets, accrual-to-assets, one-year volatility and drawdown calculated from the corporate-action-adjusted return series. Higher risk score means lower measured risk. Interest coverage, inventory and profit instability remain missing until the provider fields are mapped and validated.

Normalize factor values by date; winsorize ranks at the 1st/99th percentile. Category score is the mean of available factor ranks only when at least 80% of that category's required inputs are available. Missing factors remain missing and reduce coverage, never become zero. Overall score is shown only when category coverage is sufficient. Explainable labels are `quality candidate`, `growth candidate`, `valuation candidate`, `needs more data`, or `watch`; none is a buy/sell instruction.

### DuPont and sector models

For industrial companies, use consolidated net profit and consolidated equity consistently:

`ROE = net profit margin × asset turnover × financial leverage`

`net profit margin = TTM net profit / TTM operating revenue`
`asset turnover = TTM operating revenue / average total assets`
`financial leverage = average total assets / average total equity`

Display the three factors and calculated ROE separately. Reject zero or negative denominators. Also display parent net profit growth separately. Banks use PB/ROE, capital and asset-quality inputs; insurers use PB/ROE, embedded value/NBV and solvency inputs. If the current provider panel lacks capital quality, NIM, NPL, provision coverage, NBV or solvency, clearly label the financial-sector score incomplete rather than applying industrial-company FCF valuation.

## Machine-learning challengers

The explainable factor score is the baseline. Optional challengers are Ridge and LightGBM; LightGBM is an optional dependency. Training data is a point-in-time CSI 300 constituent panel. It must include factor values as of each signal date, membership as of that date, and forward labels that mature after the signal date. Use next-session open entry and 252-session forward excess total return versus CSI 300 total return. Purge overlapping forward-label windows, use rolling yearly train/validation/test windows, and lock the latest 2024-to-evaluation window for final comparison. Record seeds, feature set, hyperparameters and model version. Fewer than three years of out-of-sample results means `experimental`.

The application accepts a normalized CSI 300 point-in-time panel at `runtime/research/csi300_pit_training.csv`; the required columns are documented in `docs/CSI300_TRAINING_PANEL.md`. No model is promoted automatically. Require higher net CAGR than the baseline without more than 3 percentage points additional maximum drawdown, turnover no greater than 400% annually, and continued advantage when costs are doubled. If the training panel or optional dependency is absent, the UI reports why ML results are unavailable; it does not silently train on today's constituents.

## Portfolio backtests

Compare: CSI 300 index benchmark; equal-weight available universe; eligible top-eight explainable scores; eligible top-eight challenger scores when available. The currently queried Hithink index series is a price index, so the UI labels it `price return` and does not present it as total return. Rebalance quarterly using the latest available signal at quarter-end and trade at the next session's open. Eligibility gates: quality >= 60, risk >= 50, valuation >= 50, factor coverage >= 80%; maximum three companies per industry and two financial companies; empty slots remain cash. Resolve ties by code.

Support a fixed initial capital (default CNY 100,000) and monthly contribution (default CNY 3,000 on the first available session). Contributions first fill underweights; routine quarterly rebalance handles the rest. Show trade ledger, cash, weights, fees, taxes/assumptions, turnover, benchmark and data coverage. Report CAGR, total return, maximum drawdown, volatility, Sharpe, turnover and concentration. Do not select a best strategy on the same period used to tune it.

Transaction-cost assumptions are explicit and editable per run. The report identifies commission, minimum commission, stamp duty, transfer fee, dividend tax/reinvestment method and slippage assumptions. Fees use a time schedule where applicable. If the data source cannot establish an event or exact historical rate, state the assumption and quantify the limitation.

## Application and persistence

- Add `/research` and `/api/research/*` to the FastAPI simulator. The page exposes current score details, the latest 16 quarterly factor signals per company, and company/factor-history CSV exports.
- Use a dedicated `runtime/research/research.sqlite3`, isolated from paper-trading and existing DCA/backtest ledgers.
- Run refresh/analysis jobs in the background. Persist immutable run inputs, statuses, progress, errors, output, model versions and data provenance.
- Show the 25-company ranking, coverage, sector model, factor breakdown, DuPont components, valuation history and backtest comparison. Allow CSV/JSON/HTML export.
- Keep the existing trading simulator and instrument approvals unchanged.

## Acceptance criteria

1. A user can refresh the 25-stock dataset and see per-company coverage and API request IDs.
2. A user can inspect DuPont, quality/growth/valuation/risk factors and missing-data reasons per company.
3. The backtest uses publication-aware signals, the next tradable open, costs and event-adjusted stock returns; it exposes its trade ledger and explicitly labels the CSI 300 price-return benchmark.
4. The UI can select fixed capital, monthly contribution, evaluation period, and model strategy.
5. ML either supplies rolling out-of-sample comparisons from the CSI 300 point-in-time panel or explicitly reports the missing panel/dependency state.
6. Research operations do not alter paper-account cash, holdings, orders or instrument approvals.
7. Every report shows data date, model version, assumptions, limitations and whether a result is eligible for interpretation.
