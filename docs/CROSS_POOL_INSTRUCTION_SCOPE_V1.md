# DLMM Cross-Pool Normalization Scope Repair V1

## Evidence and root cause

At historical slot `440295936`, transaction index `1230` contains a router
with distinct DLMM invocations. The frozen D1 index identifies two target
`2L47...` pool swaps; the same transaction also has a `claim_fee2` and a swap
for `8tp8...`. The original normalizer reported two `event pool attribution
unresolved` blockers for the foreign pool's `ClaimFee` and `ClaimFee2`, and
one `STATE_MUTATION` blocker for its `claim_fee2`; transaction-level fail-closed
then suppressed the two otherwise canonical target swaps.

The pinned official IDL declares the first `claim_fee2` account as `lb_pair`.
An unrelated pool must be scoped by this **typed instruction account** and
the actual event's `lb_pair`; membership in the transaction's global account
list is not sufficient to infer mutation ownership.

## Source change

`normalize_block()` now ignores a non-target non-swap event or mutation only
when an unambiguous decoded owner, the pinned IDL's `lb_pair` account name,
and any explicit event pool agree that it concerns a foreign pool, and its
instruction account vector does not reference the target pool. All ambiguous
owner, cross-pool account-overlap, target-pool mutation, pool mismatch, and
unknown-instruction cases remain fail-closed. Existing swap pairing rules
and real economic event counts are unchanged.

## Verification boundary

The separate pilot v1_1 reruns five frozen historical blocks and their
full-block normalization, recorded prestate, SDK-derived quote, actual raw
Swap2Evt parity, owner-scoped SPL CPI transfers, and exact bound end-of-slot
LbPair/BinArray/BitmapExtension byte comparisons. It emits new immutable
reports; it does NOT rewrite v1 reports or canonical D1 source artifacts.
This change does not implement claim_fee2 for the *target* pool, per-position
fee/reward mutation, price-impact swaps or the complete D1 replay engine.
The official D1 historical gate remains UNAVAILABLE pending comprehensive
mutation coverage and full corpus replay.
