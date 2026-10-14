# DLMM swap semantics V1

## Checkpoint and purpose

This stage builds on the committed replay-input foundation at
`4bbe98097368962b309feb89af28120f4725ae92`.

The foundation retains the LO account fields, pairs economic `Swap` / `Swap2Evt`
emissions, and preserves actual exact-in / exact-out instruction constraints.
This stage adds bounded quote arithmetic, participant fee allocation, and a
separate pure state-transition API. Each has an independent comparison target.

| Layer | Implementation | Comparison target |
|---|---|---|
| Per-bin amounts | `swap_math.py` | Pinned official SDK helper vectors |
| Pool quote and typed constraints | `swap_quote.py`, `sdk.py` | Actual npm SDK on explicit frozen account bytes |
| Fee recipients | `swap_fees.py` | Official formulas and actual program transfers/events |
| LO / MM / pair mutation | `swap_transition.py` | Actual deployed SBF pre/post accounts and CPI events |
| Reproducible program comparison | `program_parity.py` | Bundled program, raw inputs, Clock, expected accounts and events |

The existing `Replay` and pilot entry points retain their historical eligibility
gates. A successful synthetic comparison does not change a historical dataset's
validation level or frozen continuity hashes.

## Sources and execution provenance

The source/IDL/fixture pin is MeteoraAg/dlmm-sdk commit
`576919e3e4368e542c402f000b4264724f7f23ec`, npm SDK `1.9.14`, IDL `0.12.0`.
The full IDL byte SHA256 is
`045c0f4af044be046b6a14b350077e4242e8ac026b64a855803dab30fcdd8b35`.

The program executed for the mutation oracle is the binary matching the
canonical D1 program hash:

```text
program_id=LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo
program_sha256=d296c6771cec945601027613ca637c1be6721044c5859009d41c453477844c1f
program_bytes=2229776
program_deployment_slot=423977638
runtime=solders.LiteSVM
solders_version=0.27.1
```

The official SDK repository's `commons/tests/artifacts/lb_clmm_prod.so` instead
has SHA256 `942f609d03440911f15c40e8df1733d808670768fd9042af1de956964fb2f0e6`.
Its bytes differ. The SDK commit identifies the IDL and starting fixtures; it
does not identify the deployed executable used here.

`reference/program_cases_v1.json` records the program, compressed fixture,
uncompressed JSON, IDL, and original capture checksums. The fixture archive
preserves the original captures, including raw transaction bytes, all listed
pre/post accounts, explicit Clock, controlled initial mutations, actual events,
runtime results, and parent case IDs. Each original capture hash is reproducible
by serializing its case object with `json.dumps(indent=2, sort_keys=True)` plus a
newline. Original authoring paths in provenance are documentary strings; the
portable verifier reads the program supplied with `--program`.

All 141 cases have **SYNTHETIC** input states and Clock. There are 120 successful
program executions and 21 expected errors. Of the successful cases, 91 use the
four swap variants covered by the new model, 16 use price-impact variants, and
13 place limit orders to build reachable LO cohorts. The latter two groups
remain explicit program-execution evidence; they do not become modeled swaps.

The SDK's original fixture uses other token mints. Model comparisons make an
explicit synthetic mint-label mapping to SOL/USDC while leaving raw program
execution bytes untouched. SDK quote tests separately make fully declared raw
mint/decimal/reserve transformations before asking the SDK to quote. Neither
operation establishes historical canonical-pool account evidence.

## Typed intent and pure quote

`SwapIntent` retains the actual instruction contract:

```python
from mdlmm.swap_quote import SwapIntent, quote_swap
from mdlmm.swap_transition import simulate_swap

intent = SwapIntent.exact_out(
    out_amount=100_000_000,
    swap_for_y=True,
    max_in_amount=120_000_000,
    host_fee_present=True,
)
transition = simulate_swap(state, intent, clock.unix_timestamp, slot=clock.slot)
post_state = transition.post_state
```

`SwapIntent.from_event()` requires canonical paired input and decoded actual
instruction arguments. It supports `swap`, `swap2`, `swap_exact_out`, and
`swap_exact_out2`. The modern layouts require empty, known
`remaining_accounts_info.slices` for this SPL-token scope. Price-impact
constraints and other remaining-account extensions are rejected.

`quote_swap()` returns a `PoolQuote` with quantities, per-bin fills, source
references, and SDK scalar values. It has no `.state`. A host-bearing intent is
valid input to `simulate_swap`; that adapter uses a host-absent amount quote and
then applies the actual host allocation per bin.

Pool traversal uses the recorded internal bitmap and verified BinArrays.
Missing selected arrays, unknown required extensions, or incomplete arrays
remain unavailable evidence. SDK quotes can traverse a supplied extension;
state mutation that must clear an external bitmap is explicitly unsupported
until its mutation has been program-validated.

All token quantities use integers. Program-width validation rejects negative,
overflow, boolean, floating-point and string state scalars before reference or
fee arithmetic. Missing retained LO fields and protocol counters are not
converted to observed zero. Failed transitions leave caller-owned state intact.

## Per-bin fill and fee rules

Output liquidity is consumed in the order **MM → processed LO → open LO**.
Required input is rounded independently for each layer. The fee split uses the
MM share of net input, with the MM fee rounded upward. LO fees are divided
between maker and protocol with the odd unit assigned to protocol.

Host fees follow Meteora's official
[DLMM formulas](https://docs.meteora.ag/core-products/dlmm/formulas), also checked
against docs commit `0f0e88986c3a12ae08565e47d3c1300df70d5d3e` and the bundled
program transfers. With `p` the protocol share in basis points:

```text
MM_host = floor(MM_protocol * 2000 / 10000)
LO_eligible = floor(LO_total_fee * p / 10000)
LO_host = min(floor(LO_eligible * 2000 / 10000), LO_protocol)
host = MM_host + LO_host
protocol_after_host = MM_protocol + LO_protocol - host
```

These floors are applied per bin before aggregation. MM LP and LO maker
entitlements remain unchanged by host presence. Zero observed host fee does not
establish that the host account was absent.

## Actual state mutation rules

Only MM-layer principal changes `Bin.amount_x/y`. The LO side uses its own
processing, fulfilled, and maker-fee counters.

On the first positive output from an open cohort, the program moves the old
open amount into `total_processing_order_amount`, sets remaining to the unfilled
part, clears open amount, and increments `order_age`. Processed liquidity is
consumed before a newer open cohort. Fulfilled amounts and maker-fee counters
remain cumulative across cohort rollovers. When processed liquidity reaches
zero, its total-processing amount remains recorded.

Positive LO input that rounds to zero LO output does **not** start a cohort.
The actual swap may still succeed if MM produced positive output. The bundled
edge cases check this in both directions after actual `place_limit_order`
instructions.

MM fee growth first floors the scaled supply:

```text
scaled_supply = liquidity_supply >> 64
fee_growth_delta = (mm_lp_fee << 64) // scaled_supply
```

Dividing `mm_lp_fee << 128` by the full fractional supply gives a different
answer for captured cases. Zero scaled supply with a positive MM LP fee is
rejected.

The active bin, reference fields, volatility accumulator, protocol counters and
relevant bitmap bits are updated in the owned result. A depleted array's
fulfilled counters, total-processing amount or order age do not keep its
liquidity bitmap set.

`variable.last_update_timestamp` is updated to Clock only when the ending active
bin differs from the starting active bin. Same-bin reference/volatility changes
do not update it. Crossing updates it even with zero fees or an unchanged,
saturated volatility accumulator. Thirteen dedicated program cases distinguish
these conditions. `last_updated_at` is preserved in this modeled swap scope.

## Exact-out: SDK scalars and actual protocol accounting

The SDK's exact-out quote reuses exact-in arithmetic and reports a protocol
scalar that may include output rounding. That scalar is retained verbatim for
SDK parity. It is not used as an actual fee-token counter increment.

The following zero-fee controlled cases use `price = 10 * Q64`:

| Requested output | Consumed input | Bin output decrement | Input-fee mode: protocol increment | Output-fee mode: protocol increment |
|---:|---:|---:|---:|---:|
| 10 | 1 | 10 | 0 | 0 |
| 9 | 1 | 10 | 0 | 1 |
| 1 | 1 | 10 | 0 | 9 |
| 11 | 2 | 20 | 0 | 9 |

Actual user output equals the requested output. In output-fee mode every
positive remainder, including one unit, accrues to protocol. In input-fee mode
the output remainder stays as separately reported unallocated vault surplus.
The remainder is not host-eligible. The pinned SDK instead adds the remainder
to its scalar only when it exceeds one, independently of fee-token identity.

The transition therefore reports `base_trading_fee`,
`rounding_protocol_fee`, `unallocated_output_surplus`, `fee_token`, and
`output_token` separately. For exact-out, `Swap2Evt.amount_left` is
`max_in_amount - consumed_input`.

## Verification and remaining historical work

Run the complete tests in the existing Python environment. For the independent
program replay, `solders==0.27.1` is the pinned optional runtime:

```bash
PYTHONPATH=src .venv_dlmm/bin/python -m mdlmm.program_parity \
  --cases tests/fixtures/dlmm_program_cases_v1.json.gz \
  --program tests/fixtures/dlmm_d296_program.so.gz \
  --run-svm
```

The program verifier creates a fresh local VM for each raw transaction and
loads only the explicit bundled inputs. No transaction is submitted to a
network. Expected error cases must reproduce their execution status, error,
post-account rollback state and any recorded CPI events.

Model comparisons project all represented Bin/pair fields into copied prestate
bytes, compare the complete resulting pair/BinArray bytes, and compare user,
reserve and host token-account changes plus Swap2Evt economics. Oracle-account
mutation and transaction fee-payer behavior are checked by SVM replay; they are
outside this model's account projection. Source state purity is checked with
the full represented object, since the continuity hash omits MM fee growth.

Historical completion still requires typed replay/capacity dispatch, actual
canonical instruction prestates and account coverage, and validation against
the frozen D1 dataset. In particular, the existing pilot capacity path assumes
exact-in `payload['amount_in']`; it must be adapted before enabling exact-out
historical evaluation. The 127,743 D1 economic swaps have not been replayed by
this patch. Dataset acquisition, continuity resealing, D1 result promotion and
PnL computation are not performed by the installer.

The installer's program/SDK results are synthetic comparison gates. It reports
`D1_HISTORICAL_GATE=UNAVAILABLE` until those separate historical requirements
have been satisfied.
