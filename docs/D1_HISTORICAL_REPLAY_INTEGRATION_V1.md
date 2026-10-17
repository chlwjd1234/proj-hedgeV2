# D1 Historical Replay Engine Integration V1

Checkpoint: `main` commit `c2a6771c10e60bdcb32abdde0f906f6d77a3d85c`.

## Implemented

- Strict model-version selection: `MM_V012` remains the established legacy path; `LO_V012` requires the recorded d296 deployed-binary SHA256 and effective LIMIT_ORDER pool.
- Historical LO `Replay.step` uses actual canonical instruction constraints and the separately verified `simulate_swap` transition. 13 `Swap2Evt` fields plus the compatibility fee are compared before state advancement.
- The bitmap extension is either **explicitly absent** in the swap instruction (`PROGRAM_ID` sentinel) or is supplied from a verified raw account capture. Unknown is not zero.
- LP fee attribution receives only the MM LP entitlement (`lp_base`, `lp_variable`). LO participant, protocol and host slices remain separately accounted; output-token protocol rounding is a separate fee entry.
- Checkpoints compare *all represented* pair/BinArray fields including MM growth, LO processing and protocol counters. This is not raw-account byte equality.
- MM-only D0 synthetic runs retain their original behavior. Historical LO receipts **cannot** be promoted to a D1 historical PASS by the old D0 validator without the external account/Oracle/CPI/full-block evidence gate.
- Historical D1 capacity perturbation remains explicit UNAVAILABLE for LO and exact-out trades until a separately validated hypothetical-liquidity model exists.
- `mdlmm.corpus_worklist` verifies the frozen canonical index, 127,743 swap count, all four instruction variants and immutable sample windows/pools. Writes a deduplicated `(slot,pool)` acquisition worklist; optional pool-touch page scan classifies potential non-swap mutations but does **not** prove full-block coverage.
- The five previously proven real historical blocks are re-run in a new output directory with the typed Replay adapter additionally compared against the independent original transition and original on-chain account bytes.

## Important evidence boundaries

1. SBF 141 program cases and SDK tests already validate arithmetic and account mutation in controlled inputs.
2. The 5-block historical batch validates 11 real swaps and 24 account comparisons, provided its matching server outputs are PASS.
3. Full D1 historical replay of 127,743 swaps requires block-start/predecessor account state, all in-block target-pool mutations (including non-swap state updates), and independent account/Oracle/Token-CPI checkpoint evidence.
4. The gTFA transaction pages are **pool-touch inventory**, not a complete finalized-block or a transaction pre-state archive.
5. No historical PnL/OOS inference or capacity estimate is authorized by this stage.

## Program entry point

```bash
PYTHONPATH=src .venv_dlmm/bin/python -m mdlmm.corpus_worklist \
  --root . --output runs/d1_worklist_v1 --scan-pages \
  --max-pages-per-segment 2
```

For full pool-touch inventory audit, use a *new* output directory and omit `--max-pages-per-segment`. It can read several GiB of existing local JSON but makes no RPC calls.

Cross-check the standalone historical batch using `d1_historical_batch_pilot_integration_v1.py` in `/root`, generated and hash-pinned by the installer. That runner is a validation harness, not a D1 execution or PnL CLI. The immutable V1 and V1.1 results remain untouched.
