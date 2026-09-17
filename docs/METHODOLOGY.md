# Methodology and limitations

## Trade reconstruction and hedge candidates

Broker statement rows are matched with FIFO within account, contract and direction.
This is a bookkeeping assumption, not proof of the trader's intended position allocation.
TXT statements may have aggregated fills or no accurate intraday timestamps. Missing
initial positions and ambiguous records require review of parsing errors. Do not infer
intraday timing from placeholder midnight timestamps.

Overlapping opposite-direction legs are scored as hedge/arbitrage **candidates**.
One leg may appear in several candidates; candidate profits must not simply be summed.
The greedy leg-disjoint subset prevents selected leg reuse but does not guarantee
independent observations, non-overlapping time intervals or globally optimal matching.
Naming an output “arbitrage” does not establish riskless arbitrage or trader intent.

## Account-period profit

For one account per working root, account daily rows are sorted by date before
calculating period balances. The first opening balance is reconstructed using
the existing cash-ledger convention:

`opening = first closing balance - first realized PnL + first fees - first net cashflow`

Monthly, yearly and cumulative account-profit calculations share this opening
balance. Each subsequent period starts at the previous period's closing equity.
Period profit is closing equity minus opening equity minus deposits plus withdrawals.
A first-day deposit is therefore not subtracted twice, and first-day PnL and fees
remain in the period result.

This fix does not reconstruct missing initial floating PnL, missing months or
multi-account equity. Complete account data and the stated balance conventions
remain prerequisites. Existing CSV/Excel reports must be regenerated after updates.

## Palm-oil price features

- Near/far contract identity is based on delivery year/month.
- Point spread: near settlement minus far settlement.
- Percentage spread: near settlement / far settlement - 1.
- Log spread: log(near / far).
- Entry/exit explanatory features use the latest available observation strictly before
  the recorded event date.
- Rolling Z20 requires at least 15 observations; Z60 requires at least 40 observations.
  The current observation belongs to its rolling window. A daily row's features are
  not available before that row's closing/settlement observation.
- Missing two-leg observations reduce coverage. Stale-price flags, session boundaries,
  exchange holidays and full contract-universe coverage need further audit.
- Some fields called “主连” in inherited outputs refer to a vendor continuation RIC.
  A first-nearby continuation is not necessarily a volume-defined main contract;
  roll discontinuities and adjustment conventions can affect trend features.

## Monthly main/far selection

Each month uses the preceding five available market dates. Contracts require at least
three reference observations, average volume >= 100 and average open interest >= 500.
The main candidate is within 1–8 delivery months ahead, ranked by reference-period
average volume and then open interest. The far leg is the most liquid eligible contract
3, 4 or 5 delivery months beyond the selected main contract.

Selection is fixed within the month; it is not a daily dominant-contract strategy.
The implementation only sees contracts in its input. An incomplete universe can alter
both the “main” contract and backtest opportunities. Using only today's chain to build
older history creates survivorship/coverage problems.

## Backtests and evaluation

Historical scripts retain their own parameters; they are research iterations, not
a unified production engine. Inspect constants/dataclasses before running.
The main/far experiment includes a five-trading-day cooldown and entry confirmation,
and uses prior-day signals with a subsequent settlement-price proxy. Settlement is not
a guaranteed executable two-leg fill. Costs, legging risk, slippage, margin calls,
price limits, rolls and trading-session alignment need more realistic modelling.

Some reported drawdowns use cumulative realized closed-trade PnL, excluding intervening
unrealized losses. They must not be presented as fully marked-to-market account drawdown.
Statement-derived daily balances also have estimation limitations when daily cashflows
and floating PnL are missing.

The daily panel labels 2018–2019 as warmup/history, 2020–2024 as research, 2025 as test,
and 2026 as observation. Those labels do not prove a pristine out-of-sample experiment:
earlier iterations have already examined later years. A newly frozen specification
needs genuinely unseen forward data or a carefully nested evaluation plan.
No accuracy, Sharpe ratio, return or profitability improvement is claimed by this repository.

## Test coverage

Offline tests cover imports, month parsing, monthly selection, a future-volume perturbation,
spread arithmetic, pre-event timing, a future-price perturbation and P/P filtering.
Report tests additionally cover synthetic TXT/XLSX statement layouts through parsing,
matching, analysis and Excel export, plus dynamic chart references and input handling.
They do not validate all real broker variants, live market-data responses, PostgreSQL
integrations, all backtest PnL calculations or actual execution. No private data is
needed for the tests.
