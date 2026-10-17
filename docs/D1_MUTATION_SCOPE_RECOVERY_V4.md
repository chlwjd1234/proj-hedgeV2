# D1 Mutation Scope Recovery and Evidence-Gap Inventory V4

Historical commit binding: `9e89c64d3a553790d43ee39e8e819b7da49ecd5e`.

## Why this patch exists

The 8-segment gTFA scan found 3,377 `UNDECODED_03dd95da6f8d76d5`
instructions. That discriminator is the pinned official
`add_liquidity_by_strategy2` instruction. Its named `lb_pair` account is at
index **1** (index 0 is the position). Failure to decode Borsh instruction
arguments MUST NOT cause the scope scanner to lose the ownership evidence.

The change is restricted to **inventory** (`corpus_worklist.scoped_dlmm_instructions`).
A unique pinned IDL discriminator and named `lb_pair` account may recover the
candidate pool owner even when instruction arguments fail to parse. Such rows
are counted separately as `known_target_args_unparseable` and retain the
`IDL_ARGS_UNPARSEABLE` status. They are **not** replayable. An instruction with
no named `lb_pair` remains unresolved, even if another instruction in the
transaction references the selected pool. Foreign-pool attribution requires
explicit named ownership with no shared target account.

The normalization engine's existing strict blocker handling is deliberately
**not** weakened. This patch neither executes non-swap instructions nor
implements an observed-state mutation adapter.

## Independent V3 witness inventory

`mdlmm.mutation_witness_inventory_v4` verifies the raw bytes and hashes of
V3's 80 before/end account pairs. It reconciles those witnesses with 24 cached
full-block analyses, enumerates which state-bearing accounts associated with
mutation instructions were actually captured, and writes a deduplicated
**missing-witness acquisition worklist**. The tool makes no RPC calls.

Slot `S-1` versus slot `S` account snapshots describe the entire block's net
change, *not* the state immediately before/after a transaction or CPI. Even
an apparently complete block-boundary witness cannot by itself establish
per-instruction mutation parity when a block contains swaps and other writes.

## Explicit non-claims

- D1 historical gate remains `UNAVAILABLE`.
- No on-chain per-instruction prestate or poststate has been reconstructed.
- Account existence/closure or owner classification does not prove mechanics.
- None of the 127,743 swaps have been added to a new full D1 executable replay
  by this patch. Previously proved 11 real historical swaps remain unaffected.
- `ebc23645410aec70` remains UNKNOWN in the pinned IDL and requires a
  program/version or CPI-source investigation.

## Execution

After the installer and full pytest pass, rerun the frozen full gTFA scan with
a new immutable output path, then run `mdlmm.mutation_witness_inventory_v4`
against the exact V3 evidence directory and the new corpus summary.
