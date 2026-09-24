# Server runbook

All commands run inside this repository. No command accesses the v3 repository. No wallet, private key, secret seed or real order is needed. Exit 3 means unavailable/gated, not a shell crash.

## 1. Environment

```bash
cd meteora_dlmm_hedged_research
python3 --version  # must be >= 3.11
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
python -m pytest -q
./run_pipeline.sh preflight
```

Core runtime has no third-party Python dependency. Tests need pytest/jsonschema. Optional archive downloads need `python -m pip install -e '.[archive]'`. Optional local SVM needs `python -m pip install -e '.[svm]'`. The optional SVM path was not executed in the authoring environment and should be validated against supplied complete replay cases.

## 2. Credentials

```bash
export SOLANA_RPC_URL='https://YOUR_ARCHIVAL_SOLANA_RPC_ENDPOINT'
# Only for authenticated requester-pays S3, when using that optional adapter:
# export AWS_PROFILE='your-profile'
# export AWS_DEFAULT_REGION='us-east-1'
```

Use a RPC that permits getProgramAccounts, coherent getMultipleAccounts, full historical getBlock/getTransaction and address-signature history. A public endpoint can rate-limit or omit history. This is reported as UNAVAILABLE. API keys in endpoint URLs are not logged as manifest config. Do not commit `.env` or credentials.

## 3. Pinned SDK and current D0 sample

Install Node.js 20+ using your normal system administration method, then:

```bash
node --version
./run_pipeline.sh source-sync
./scripts/install_sdk.sh
./run_pipeline.sh d0
./scripts/extract_d0.sh
```

The SDK adapter reads frozen captured account bytes through an in-process RPC adapter and forbids fallback network reads. It compares Python and official decoder/quote output on the same state. Missing SDK/dependencies are UNAVAILABLE. Record the generated npm lockfile before transferring the exact environment elsewhere.

## 4. Independent raw collectors

```bash
./run_pipeline.sh discover
# Set these to an actually discovered canonical SOL/USDC pool and archived range:
POOL='REPLACE_WITH_DISCOVERED_POOL_ADDRESS'
START_SLOT=0  # replace; not a suggested history boundary
END_SLOT=1    # end exclusive; replace
./run_pipeline.sh snapshot --pool "$POOL"
./run_pipeline.sh pool-history --pool "$POOL" \
  --start-slot "$START_SLOT" --end-slot "$END_SLOT" \
  --max-pages 100 --max-blocks 10000
```

A full-block alternative is `./run_pipeline.sh blocks --start-slot ... --end-slot ... --max-blocks ...`. Budgets are explicit. Neither collector writes a fabricated historical account seed. A small current snapshot can also exceed the 100-account coherent batch boundary; the collector fails rather than merging unrelated slots.

`discover-historical --input path/to/block-inventory.json` extracts historical LbPairCreate events from supplied full blocks, including addresses no longer returned by current discovery. It does not certify a globally complete past pool universe. Input inventory uses `{"blocks":[{"path":"...","sha256":"..."}]}` relative to its own directory.

## 5. Historical preparation and D0

Obtain the archival inputs in DATA_CONTRACT first. Then place the plan at `data/pilot/archival-plan.json` and run:

```bash
./run_pipeline.sh prepare-historical --input data/pilot/archival-plan.json
./run_pipeline.sh d0 --dataset data/pilot/dataset.json
./scripts/extract_d0.sh
```

`prepare-historical` consumes complete exports, normalizes and seals hashes; it is not itself D0 approval. Missing pre-state, binary binding, Clock, independent checkpoint, unsupported state change or relevant data gap blocks the result. Existing `dataset.json` is never silently overwritten.

## 6. D1 and extractor

```bash
./run_pipeline.sh d1 --dataset data/pilot/dataset.json
./scripts/extract_d1.sh
# Explicit alternate capacity ladder; no width optimization:
# ./run_pipeline.sh d1 --dataset data/pilot/dataset.json --capital-ladder 1000,10000,100000
```

Default policies are 2 pools × 2 width rules × 4 windows. Windows are explicit in the dataset, or selected from external reference data alone. D0 and D1 must see the same data/source fingerprints. If source/config changes, rerun D0; do not force the old gate.

Outputs include `case_results.jsonl`, `ledgers.jsonl.gz`, `markouts.jsonl.gz`, `capacity.jsonl.gz`, `fee_toxicity_summary.json`, `windows.json` and immutable result/manifest. Use the extractor, not entire logs. Output names are defined by the actual runner; inspect the manifest's artifact list if a file is unavailable because the case failed.

## 7. D2 and D3

```bash
./run_pipeline.sh d2
./scripts/extract_d2.sh
./run_pipeline.sh d3-smoke
./scripts/extract_d3_smoke.sh
```

D2 always runs the analytic two-bin fixture. When a current validated D1 trace exists, it additionally runs initial-inventory static shorts for every case, reserves collateral from existing idle cash, and exports residual delta. Its zero fee/funding assumptions are labeled IDEAL_ACCOUNTING_ONLY, never borrowed as real funding data.

D3 needs a real D1 candidate with documented conservative costs and a complete Hyperliquid market bundle. Default illustrative cost mode cannot pass that gate. Maker-first is conservative unfilled-then-taker without queue evidence. D3 smoke is not a strategy-success result, even if technical execution passes.

## 8. Hyperliquid optional acquisition

```bash
python -m pip install -e '.[archive]'
# Use a verified archive bucket/key; no invented availability claim:
./run_pipeline.sh archive-download --bucket VERIFIED_BUCKET --key VERIFIED_KEY \
  --output data/hyperliquid/raw_book.lz4 --max-bytes 1000000000 --requester-pays
./run_pipeline.sh funding-rates --start 2025-01-01T00:00:00Z --end 2025-01-08T00:00:00Z
# Funding rates alone are not sufficient: add independently sourced hourly oracle prices.
./run_pipeline.sh funding-join --input path/to/funding_rates.json --oracle-file path/to/oracles.jsonl
```

Requester-pays is opt-in because it may incur charges. S3 HEAD size/ETag is checked; downloaded bytes receive a SHA256. Oracle rows must carry `kind=HYPERLIQUID_ORACLE`, `symbol=SOL`, source and exact payable-hour timestamps. Missing funding stays missing.

## 9. One command and offline software validation

```bash
./run_pipeline.sh all --dataset data/pilot/dataset.json
./scripts/extract_all.sh
# No credentials/data purchase required for the synthetic integration path:
./run_pipeline.sh all --demo
```

The workflow creates every stage receipt and expected gate skips. It does not execute a successful economic D1 through D0 failure. A missing D3 market does not break completed D1/D2 code validation. Synthetic and real runs both use timestamped unique directories.

## 10. Local first Git checkpoint / remote publishing

The archive includes the initial local Git repository. Inspect it without modifying v3:

```bash
git status --short
git log -1 --format='%H %s'
```

No GitHub remote has been created or pushed automatically. Creating a remote later is a separate user-controlled action. Generated data/runs/dependencies/credentials are ignored. Do not use blanket cleanup on archival inputs.
