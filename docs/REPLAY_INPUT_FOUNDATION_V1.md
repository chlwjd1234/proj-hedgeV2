# Replay input foundation V1

Base: `8525df0923a7ad33efe3d8a13e71e5171586313d`.
Official layout/quote reference: MeteoraAg/dlmm-sdk
`576919e3e4368e542c402f000b4264724f7f23ec`, SDK `1.9.14`.

This change preserves the state and instruction evidence required by the next
exact-out, host/referral and limit-order execution work. It does not open the
D1 historical replay gate. The canonical D1 corpus, its hashes, selected pools,
and four windows remain external frozen inputs; this patch does not rewrite them.

## State representation

`Bin` retains open/processing quantities, fulfilled-order amounts, LO fee
counters, side and order age. Missing serialized LO fields are `None`; known
on-chain zero stays integer zero. `limit_order_fields_complete` means all nine
fields are represented, not that their mutation is validated.

`PoolState` retains the two reward mints, both protocol-fee counters, the 16-word
internal bin-array bitmap, and `last_updated_at`. Reward mints and bitmap words
use immutable tuples internally and ordinary JSON arrays on serialization.
This records the internal bitmap only; it does not supply a missing bitmap
extension account or prove that all initialized arrays were captured.

`effective_function_type` resolves explicit types 1/2 directly. Type 0 requires
recorded reward mints: any initialized reward means liquidity mining, while
two default pubkeys mean limit order. Missing reward evidence returns unknown.
This classification does not enable a new execution path.

## Canonical instruction/event connection

`normalize_block(..., require_canonical_swaps=True)` requires one `Swap2Evt`
and one compatibility `Swap` owned by each recognized swap invocation. It
checks their shared fields and retains the actual instruction arguments,
accounts, variant, CPI location and both event-source locations. The economic
event uses the rich event's location; its payload separately records the
owning swap instruction's location.

All six swap variants are recognized. Fixed legacy arguments have a bounded
fallback decoder. The `*2` variants require the supplied IDL to decode their
`RemainingAccountsInfo` tail. Exact-out requested output and input cap come
from instruction bytes. Host presence comes from the actual optional account,
including the Anchor program-ID sentinel for an absent account. Neither is
inferred from observed output or a zero fee.

CPI ancestry uses recorded stack heights and instruction order. Ambiguous
ownership, missing or duplicate pair members, malformed inputs and inconsistent
events are blockers. They cannot be cleared by substituting an observed
liquidity transition. Unsupported state mutations retain their separate
requirement for independent account evidence or an SVM adapter.

Compatibility mode can retain a single legacy Swap with an explicit weaker
evidence label. It never reports a strict canonical pass. Historical bundle
preparation and raw-block coverage verification request strict mode.

The diagnostic CLI exposes the same choice:

```bash
python -m mdlmm.cli normalize --input BLOCK_DIRECTORY --pool POOL_ADDRESS \
  --require-canonical-swaps
```

Even a strict normalization pass is event/instruction evidence, not historical
prestate, SDK quote parity, or account-mutation parity. Blockers are retained
when no valid economic events can be emitted.

## Hash and trace compatibility

The expanded representation uses the explicit hash domain
`MDLMM_ECONOMIC_STATE_V2`. Missing optional keys and explicit `None` normalize
consistently; unknown remains distinct from observed zero. The pre-existing
MM fee-growth exclusion remains in place, so this hash still does not certify
complete account bytes.

New compact traces carry the hash schema and a new trace version. Old stored
continuity hashes and unmarked legacy traces cannot certify the expanded state.
They are rejected rather than automatically rehashed. A future migration of
such evidence must create a separately versioned dataset with checked raw
account provenance; frozen captures and canonical D1 artifacts are not resealed
by this change.

Historical capture loading checks represented preserved fields against the raw
decoded account bytes. This input check is separate from the replay checkpoint's
existing MM-only projection, which does not yet validate LO, protocol-counter,
reward, or bitmap mutations.

## Execution gates retained

The existing MM-only exact-in quote arithmetic is unchanged. Factual exact-out,
host/referral, and live LO execution still fail closed. Canonical host-account
presence is checked even when the observed host fee is zero. Historical replay
cannot bypass canonical pairing by directly supplying a weaker swap event.

The next implementation must prove MM/processed/open fill ordering, exact-out
rounding, fee-source allocation, mixed host allocation, and all affected state
transitions. SDK quote parity and program/account-state parity are independent
gates. Real canonical event parity still requires independently established
historical prestate. No archive acquisition, strategy run or parameter sweep is
part of this patch.

## Validation

The untouched base was independently reproduced with 243 passing tests before
the patch. The final patched tree passes 400 tests. Added tests cover byte-level state preservation and integer bounds,
unknown versus known state, hash/trace compatibility, canonical event pairing,
CPI ambiguity, all instruction families, strict CLI behavior and replay gates.
The supplied installer runs the full test suite on the user's existing Python
environment after applying and verifying the patch's file hashes.
