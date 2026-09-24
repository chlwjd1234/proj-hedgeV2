# Frozen research specification v0.1

## Primary objective

Measure whether SOL/USDC DLMM leaves inventory-matched market-making surplus before committing to a dynamic hedge strategy. Profitability is not inferred from fees, raw LP price returns, sample win rates, synthetic tests, or code throughput. The v3 repository is a methodological reference only and is not an input/dependency.

## Accounting

All token amounts in factual mechanics are atomic integers. A bin's economic SOL/USDC inventory is identified by actual mint orientation, not by assuming token X is SOL. Position principal is a pro-rata ownership claim on bin reserves. Fees accrue separately; collecting a fee must not create a second income entry. Protocol cut is informational because the recorded LP fee is already net of that cut.

Let `q[k-1]` be SOL inventory held before the next external reference mark `S[k]`. The self-financing benchmark accumulates:

`B[k] - B[k-1] = q[k-1] * (S[k] - S[k-1])`

At an inventory change it buys/sells the change at that reference mark through a cash account. Cash may be negative: this is an ideal accounting comparator, not a margin-feasible hedge. D3 separately prices feasibility/capital/funding.

D1 reports principal PnL, base LP fee income, variable LP fee income, observed composition-fee income, explicit costs, fee-asset price PnL, benchmark PnL, and surplus. SOL fee holdings' subsequent directional mark is included in the comparator, so a rise in SOL fee value does not masquerade as new fee surplus. Cost-basis realized/unrealized is a separate weighted-average SOL book, not another subtraction.

`surplus = principal PnL + fee effects - LP execution/composition/transaction costs - benchmark PnL`

The code calls this **inventory-matched surplus**, not theoretical LVR. Discrete reference sampling, observation availability, inventory granularity and fee-share rounding are explicit limitations. It never subtracts LVR again from already realized inventory losses.

## Fee decomposition

The chain observes total trading/recipient fees, not necessarily two independently rounded base/variable token transfers. The analysis policy is:

`PRO_RATA_UNCAPPED_RATES_REMAINDER_TO_VARIABLE_V1`

Actual LP fee is partitioned in proportion to raw base/variable rates, with integer remainder assigned to variable. The total is exactly conserved, but the components are an attribution convention, especially when the total fee cap binds. No APR substitution is allowed. Composition fee is a separate category. Share-level rounding bounds are exposed rather than labeled actual on-chain claim parity.

## Policy set

Two canonical SOL/USDC pools are selected using metadata and criteria available before the pilot; no realized LP PnL is used. Four disjoint 7-day UTC windows represent low-volatility/sideways, uptrend, downtrend/stress and high-volatility roundtrip behavior. Automatic selection uses **external** reference-market returns and realized variation only. Uptrend requires a positive return, downtrend a negative return, and roundtrip requires absolute net log return at most half of total absolute daily log moves; these descriptive rules are fixed, not fitted to LP performance. A missing regime is unavailable. Low-vol/sideways is the best relative score, not a guaranteed absolute volatility bound. It is ex-post stratified discovery, not OOS evidence. Explicit window overrides use the same 7-day/nonoverlap rules.

Width rules: completed-day RV30 at entry, one-day log-return scale, multipliers exactly `[1,2]`, mapped to discrete bins. No width sweep and no future RV. Thirty-one daily closes are needed. The ownership surface is equal initial reference value per selected bin, named Spot in this research package. It is not silently equated with every official balanced/imbalanced SDK deposit strategy.

Each case takes existing shares, holds them for 7 days and records entry/exit costs. There is no optimized intrawindow rerange, compounding, H1 or options layer in v0.1. Those can be introduced through the event/ledger architecture after this baseline. Empty or insufficient existing bins produce unavailable cases, not invented liquidity.

## Markouts

0 ms uses a causal reference available at execution; positive horizons use the first acceptable observation after target time and are labeled ex-post diagnostics. The actual observation delay and horizon are retained. Unavailable 1-second resolution is not reported as measured 1-second data. Sign convention is LP adverse selection: with principal trade change ΔSOL and trade price P, adverse cost at future reference S is `-ΔSOL * (S-P)`. Fee-minus-adverse-cost uses comparable USDC units and excludes unrelated protocol income.

## Capacity

Default ladder is 1k/10k/100k USDC, configurable explicitly. Per-bin share, active-bin share, directional output liquidity, quote change, final/visited bin changes and price-impact change determine SMALL_PRICE_TAKER / MODERATE_PERTURBATION / LARGE_COUNTERFACTUAL diagnostics. Classification thresholds are engineering heuristics, not econometric capacity guarantees. Fixed-intent shocks are not a volume forecast; historical routing is not assumed invariant as a universal truth. The diagnostic explicitly does not model endogenous routing/arbitrage.

## Gates

D0 historical inputs and independent MM account parity must pass before historical D1. Synthetic inputs only authorize synthetic integration. D1 reports ABSENT/INCONCLUSIVE/CANDIDATE classifications with a prespecified per-window error budget and per-width robustness checks. These are pilot heuristics, not confidence intervals or probabilities. Both pool samples are correlated. No short-period annualization is provided.

Default entry/exit costs are illustrative scenarios. D3 requires actual historical D1 candidate evidence plus sourced conservative cost bounds; a changed config/source/data invalidates the parent link. Missing historical fee/funding/margin never becomes zero. D2 is explicitly an idealized zero-cost static accounting oracle and cannot establish D3 profitability.

## Deferred financial realism

Full token-2022/limit-order/referral paths, unknown historical program versions, new-position mint/withdraw execution, exact position claim rounding, endogenous routing, landing risk calibration, margin monitoring between events, actual maker queue fills, options, live transfers and OOS approval are not claimed by this release.
