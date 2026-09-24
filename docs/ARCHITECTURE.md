# Architecture and supported boundaries

## Pipeline

`raw HTTP/RPC/S3 bytes → source receipts → normalized ordered events + captured state → D0 replay → immutable compact traces → D1 attribution/benchmark → D2 static / gated D3 execution`

Core uses integer token amounts/Q64.64 arithmetic. Financial valuations use 60-digit Decimal. SQLite is used for causal reference/L2 indexes and raw-history consistency checks. JSONL streams are compressed deterministically. A run never rewrites an earlier result.

## Module map

| Module | Responsibility |
|---|---|
| network, history, archive | read-only collection, current discovery, historical creation discovery, full-block or address-index coverage, bounded S3 download |
| codec, normalize | raw owner/discriminator/length checks, IDL Borsh decoding, versioned transactions, ALT resolution, CPI event order |
| model, mechanics | validated state types, integer prices/swaps, fee modes, fee references, composition formula |
| dataset, prepare, replay | hash-bound raw evidence, version intervals, independent checkpoints, factual MM replay |
| reference, regimes | causal as-of/availability, ex-post diagnostic markouts, independent regime-window selection |
| ledger, attribution | principal/fees/cash/hedge ledger, weighted-average basis, existing-share ownership, self-financing benchmark |
| capacity, pilot | per-bin finite-capital perturbation and 16-case multi-window orchestration |
| static, perp | D2 actual inventory static short; D3 typed venue, funding, fills, lots, margin |
| sdk, svm | optional frozen official SDK oracle; optional local deployed-binary SVM |
| config, util, validation, cli | explicit assumptions, manifests, gates, compact receipts |

## Direct state engine scope

Supported direct model identifier is `MM_V012`. It models exact-input swaps using the audited 0.12.0 MM path; InputOnly and OnlyY fee modes; Q64.64 bin pricing; fee rounding and volatility-reference updates. It can ingest independently observed, raw-byte-backed liquidity changes and fee parameter changes. It does not infer per-bin deposits from aggregate add-liquidity amounts.

The following stop direct financial replay: unsupported historical version, matching limit-order inventory, Token-2022 transfer fees/hooks, exact-output instructions, nonzero referral host fees, unknown instruction mutations, unavailable Clock, and incomplete relevant bins. These are boundaries, not zeros. Event decoding can still retain observations of unsupported paths without claiming replay support.

Checkpoint parity is **MM economic state**, not entire program memory. Growth-counter rounding, oracle ring and reward accounting are outside the direct checkpoint scope. All outputs carry this scope. Full-byte SVM comparison is a separate optional test and still does not prove historical runtime feature-set equivalence.

## Current capture versus historical data

Current coherent capture batches pool, all known bin arrays, Clock, mints/reserves and optional bitmap/reward accounts into one `getMultipleAccounts` request, with surrounding bin-array inventory checks. More than the bounded RPC batch capacity fails rather than silently mixing slots. A current capture is labeled CURRENT and cannot seed a past D1 experiment.

Historical replay requires an END_SLOT start plus every later relevant state mutation and ending checkpoints. Raw Clock values must be independently captured at the relevant slots. Account update archives, validator/SVM exports, or equivalent independent archival evidence must supply these values. `getBlock.blockTime` is not silently substituted for Clock.

## Counterfactual scope

D1 attributes fractions of **existing historical MM shares**. It therefore avoids changing historical execution when checking accounting, but is not an executable new-liquidity allocation. Capacity separately adds proportional inventory and requotes fixed historical swap intent at three capital levels. It reports route/bin changes and local shares, not an endogenous market equilibrium. Router/arbitrage/competition reactions are not modeled.

## D3 limitations

`PerpVenue` supplies terms, marks, order cost/fill and funding. Hyperliquid implements these from historical files. The default mark proxy is contemporaneous L2 mid; the margin model is isolated initial/maintenance margin observed at decision/fill events, not a continuous exchange liquidation engine. Positive funding pays a short with the recorded historical oracle notional. Missing terms/rates/oracle/depth are unavailable.

Maker-first does not infer fills from a touch. Without independent queue/fill evidence, it waits and uses taker fallback. Targets are frozen at decision time and one request is in flight. Actual order submission is intentionally absent. D3 smoke output always states `strategy_success=false`.

## Provenance and failure behavior

Input SHA256s, immutable source blobs and a code/config/test fingerprint prevent mixing old D0 with changed D1. Code changes during a run force FAIL. Program interval evidence is operator-supplied and hash-bound; it must be independently reviewed and is not magically authenticated by being JSON. No synthetic result authorizes historical economics. No fee-only yield is called strategy return.
