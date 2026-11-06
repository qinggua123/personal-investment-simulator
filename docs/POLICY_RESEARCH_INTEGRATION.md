# Policy research in the simulator

## Purpose

The simulator adds a policy research area beside the existing stock factor research. It lets you inspect the policy event seeds, record evidence that connects an event to a company, calculate exploratory event-window returns, and view time-decayed policy exposure. It does not change stock rankings, portfolio backtests, trading approvals, orders, or account balances.

## Design

The `/policy` page and `/api/policy/*` routes use a dedicated SQLite database at `runtime/research/policy.sqlite3`. The policy module owns event links and study runs. The existing market research module owns Hithink requests and the shared historical price cache. Policy studies read that cache through a public price-history function.

The simulator includes the nine policy seeds and 65-company research universe from `policy_event_research`. It keeps the policy event dates and official source URLs. The original project has no reviewed company-event links, so the imported link table starts empty. The simulator does not claim that policy impact results exist until a user records evidence and runs a study.

## Research rules

- Event date-only publications become visible after the Shanghai close. The event maps to the next CSI 300 session.
- A company link needs a reviewed flag, a non-empty business explanation, an evidence date, an event ID, and a company in the policy research universe.
- A reviewed link must include a traceable company evidence URL; the review workflow retains the URL with its point-in-time availability date.
- A link can enter the event study only when its evidence was available by the policy event's publication time.
- Missing policy evidence remains `null` or blocked. A reviewed, expired exposure can be zero. The report keeps its evidence IDs.
- Event studies use a CSI 300 market model, an estimation window of 250 to 31 sessions before the event, at least 120 paired observations, and event windows `[-20,-2]`, `[-1,1]`, `[0,5]`, `[0,20]`, and `[0,60]`. The current source uses unadjusted close prices, so corporate actions can distort event returns.
- Benjamini-Hochberg q-values adjust the successful tests in one study. The results describe historical associations and do not establish causation or a profitable strategy.
- The first release does not fit or backtest a policy prediction model. The source project's policy model was blocked by the empty company-link table, so there is no validated policy predictor to port.

## Synthesis decision

The implementation uses the independent policy page and store from candidate B. The cross-judge scored B 23/25 and A 21/25. B provides a usable event-link review workflow on day one and keeps incomplete policy evidence outside the factor model and paper account.

The implementation grafts candidate A's point-in-time distinction between missing evidence and known zero exposure. It also uses immutable study input snapshots. The market-data reader is exposed by the existing stock research module rather than reaching into private cache helpers.

## Accepted tradeoffs

- Policy research has a separate database so evidence edits cannot alter stock-research runs or paper-trading state.
- The event catalogue lists official source URLs, but the source project's cached documents are not copied into this simulator.
- Policy exposures and event studies can be displayed before there is enough evidence for forecasting. The prediction and portfolio layers remain blocked until a separate model and coverage gate are validated.
- The event study uses price returns to match the CSI 300 price-return benchmark. It excludes dividends and should not be read as an investor total-return backtest.

## Implementation phases

1. Add the policy event and company seed files, then create the isolated policy store and evidence-link API.
2. Add a public Hithink price-history boundary and calculate event studies from reviewed, time-eligible links.
3. Add the policy page, study history, exposure view, source links, and limitations.
4. Review the changed code and artifacts. Automated tests are not run in this task.
