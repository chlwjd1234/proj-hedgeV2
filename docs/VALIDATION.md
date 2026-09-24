# Validation and interpretation

Read the machine-readable `validation/authoring_report.json` for the final measured counts, checkpoint SHA, commands, and result locations. Test assertions establish only the scope they actually exercise.

## Independent categories

| Category | What is exercised | Evidence boundary |
|---|---|---|
| Unit/regression | integer arithmetic, decoder boundaries, funding/margin/depth, ordering, gates, hash tamper, reference availability | Synthetic values and explicit test doubles are not chain data |
| Public account fixture | exact bytes/blob hash of a published official LbPair account; PDA vectors | NOT SOL/USDC, not a current RPC snapshot, no historical performance |
| Synthetic integration | D0→D1→D2, four 7-day windows, two pools, two widths, full ledger/markout/capacity artifacts | Software integration only; every economic/historical promotion is blocked |
| Live SOL/USDC | read-only RPC discovery/account/actual transaction sample | UNAVAILABLE in authoring environment; no target transaction PASS claim |
| Official SDK parity | frozen actual accounts and sampled quotes against pinned Node SDK | Adapter implemented; dependency/source download not available locally |
| SVM oracle | actual binary + complete pre accounts/transaction/Clock vs post bytes | Harness implemented; optional runtime/binary/input not available locally |
| Historical D1 economics | archived target pool/bin/fee state with independent reference | Not run; no return, edge or success claim |

## Evidence levels are scopes, not marketing grades

Direct ACCOUNT_PARITY checks reserves/supply/price/active/parameters/fee references at supplied independent checkpoints. It does not include full account bytes, oracle rings, rewards or per-position claim rounding. SDK_PARITY is possible only after the actual SDK process ran and matched. SVM byte parity is limited to listed post accounts and does not silently become full historical runtime EXACT.

## Tests against false positives

The suite rejects unknown version bindings, future/current seeds used as history, unsupported liquidity sources, duplicate/unordered events, missing relevant coverage, missing/stale reference, missing payable funding, altered input hashes, double NAV/collateral counting, off-grid/band-violating orders, and D3 promotion from synthetic/scenario-only D1. Fee totals conserve protocol plus LP recipients; protocol fee is not subtracted twice.

## Not validated or not implemented

No live SOL/USDC data sample was obtained in the isolated authoring container. No truthful statement that historical mechanics have already passed is made. Unknown historical program versions and unsupported state mutations are blockers, not routine data repair. The SDK, SVM and optional S3 code paths require their stated server dependencies and real inputs; offline tests use explicit fakes only to test adapter behavior.

The direct engine is MM-only exact-input 0.12.0. Full LO/Token-2022/referral/exact-output state replay and automatic reconstruction of arbitrary historic position changes without raw account evidence are not implemented. D1 is an existing-share attribution oracle, not a full executable new-position/rerange backtest. Its capacity shock is fixed-intent and not an endogenous routing model. D3 isolated margin is event-sampled/L2-mid-based and not exact exchange liquidation semantics. These limitations are also written into result scopes.
