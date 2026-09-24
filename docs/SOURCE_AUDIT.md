# Source audit — 2026-09-24

## Immutable upstream checkpoint

- Repository: https://github.com/MeteoraAg/dlmm-sdk
- Observed commit: `576919e3e4368e542c402f000b4264724f7f23ec`
- Commit date: `2026-09-03T08:09:21Z`
- IDL metadata: `lb_clmm 0.12.0`
- Program ID: `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo`
- Package: `@meteora-ag/dlmm 1.9.14`; Anchor 0.31.0

The exact file blob hashes are in `reference/source_lock.json`. They are **Git blob SHA1**, not SHA256: SHA1(`blob <length>\0<bytes>`). `source-sync` verifies each blob and separately records raw SHA256. The complete IDL and SDK source are fetched on the user's network-enabled server, not claimed as vendored here. The compact Python account/event layouts are transcribed from the pinned IDL.

The SDK checkpoint is **not historical deployment proof**. Binary hash, valid slot interval, matching IDL and a separately sourced binding-evidence file are required for a historical registry. Current `ProgramData` inspection can identify the observed binary but does not reconstruct previous deployed binaries. Unknown intervals remain unsupported.

## Audited mechanics

Sources:

- Pinned `commons/src/extensions/lb_pair.rs`: fee formulas, filter/decay/reference, mode logic.
- Pinned `commons/src/extensions/bin.rs`: Q64.64 input/output rounding, MM reserve semantics.
- Pinned `commons/src/quote.rs`: exact-input quote, MM first then limit-order layers, activation/empty-bin transitions.
- Pinned `commons/src/math/{price_math,u64x64_math}.rs`: actual integer power algorithm, not floating-point price exponentiation.
- Pinned `ts-client/src/dlmm/idl/idl.json`: account and event layouts.
- Pinned `commons/src/{pda,seeds}.rs`: signed i64 bin-array PDA, `bin_array` and `bitmap` seeds.
- https://docs.meteora.ag/core-products/dlmm/formulas
- https://docs.meteora.ag/core-products/dlmm/collect-fee-mode
- https://docs.meteora.ag/developer-guides/dlmm/changelog

Fee precision `10^9`; total cap `10^8`. Base rate `base_factor*bin_step*10*10^power`. Variable rate `ceil(control*(volatility_accumulator*bin_step)^2/10^11)`. Protocol share comes from the actual pool, not a hard-coded current 10%/20%. Input and output fee modes differ; protocol cut must not be deducted twice. Composition formula is implemented for a supplied verified composition delta, not as a substitute for decoding the real liquidity instruction.

`Swap2Evt.mm_fee` interpretation in the MM-only normalizer is tested against the model and must pass actual observed recipient/account parity before any historical approval. The authoring environment did not perform that real-chain parity test.

## Layouts

LbPair: 904 bytes including discriminator. BinArray: 10,136 bytes, 70 bins, 144 bytes/bin. PositionV2 decoder covers the fixed 70-bin prefix; extension handling is not silently assumed. Raw owner, discriminator and size are checked. Unknown layout is not a success. BinArray doc comments with obsolete example bin counts are not used instead of the actual `[Bin;70]` schema.

## Solana ordering/Clock

- https://solana.com/docs/rpc/http/getblock
- https://solana.com/docs/rpc/http/gettransaction
- https://solana.com/docs/rpc/http/getaccountinfo
- https://solana.com/docs/rpc/http/getmultipleaccounts
- https://solana.com/docs/rpc/http/getsignaturesforaddress

Ordering is `(slot, transaction index in getBlock, instruction index, inner instruction index, event index)`, not timestamp then signature. Loaded addresses resolve v0 lookups. CPI Anchor event instruction bytes give an identifiable position. Log-only events do not acquire invented instruction indices. Failed transactions do not mutate pool state.

`minContextSlot` is a lower bound for current context, not a historical-state query. `blockTime` is not automatically the exact Clock sysvar used by the program. Historical Clock evidence is required for strict fee-state replay.

## Other primary interfaces

- Hyperliquid historical data: https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data
- Hyperliquid information endpoint: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Hyperliquid fee/funding documentation: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees and /trading/funding
- Solders LiteSVM: https://kevinheavey.github.io/solders/tutorials/litesvm.html

No present fee tier or SOL lot/margin parameter is substituted for an unknown historical rule. Historical orderbook observation time must be provided or an explicitly declared observation-delay assumption used. Funding additionally needs historical payable-hour oracle price; spot is not silently substituted.

## Actual evidence obtained locally

`tests/fixtures/official_lb_pair.json` is an actual account-byte fixture from the pinned official repository, Git blob `5b5fa60de395394440c9d8fc9f5fc04e9ab5c3ca`. Its raw account hash is recorded in its provenance JSON. It is **not SOL/USDC** and its original capture slot is not established here. It tests the decoder and published PDA addresses, not target-market historical correctness. All target SOL/USDC transaction streams packaged as demonstrations are explicitly SYNTHETIC.
