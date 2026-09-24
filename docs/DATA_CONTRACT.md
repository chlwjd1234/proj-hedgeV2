# Historical data and artifact contract v1

## 1. Sealed dataset

A bundle directory is self-contained: paths must be relative and cannot escape it. Each referenced file has an actual SHA256. `seal-bundle` can compute descriptor hashes but does not assert data completeness or correctness. Reference data must include at least 31 completed daily closes before each selected entry and adequate post-swap markout coverage.

```json
{
  "schema_version": 1,
  "data_kind": "HISTORICAL",
  "reference": {"path": "reference.csv", "sha256": "ACTUAL_SHA256"},
  "program_registry": {"path": "program_registry.json", "sha256": "ACTUAL_SHA256"},
  "pools": [{"pool": "ADDRESS", "selection_time_ms": 0,
             "selection_rule": "PREDECLARED_METADATA_RULE", "metadata_source": "SOURCE"}],
  "windows": [{"label": "low_vol_sideways", "start": "2025-02-10T00:00:00Z", "end": "2025-02-17T00:00:00Z"}],
  "segments": [{"id": "w0_p0", "pool": "ADDRESS", "start_ms": 0, "end_ms": 0,
    "snapshot": {"path": "w0_p0/start.json", "sha256": "ACTUAL_SHA256"},
    "events": {"path": "w0_p0/events.jsonl.gz", "sha256": "ACTUAL_SHA256"},
    "checkpoints": {"path": "w0_p0/checkpoints.json", "sha256": "ACTUAL_SHA256"},
    "coverage": {"path": "w0_p0/coverage.json", "sha256": "ACTUAL_SHA256"}}]
}
```

The one-pool/one-window snippet illustrates fields only. D1 requires **two distinct pools and four disjoint exact 7-day windows** (explicit or selected externally). Timestamps/hashes/addresses above are placeholders and intentionally not usable evidence. The complete synthetic generator provides a runnable software-only example of the same financial flow, explicitly labeled SYNTHETIC.

If `windows` is absent, the pilot selects four disjoint external-market windows from available reference history. Each selected window must be covered by an appropriate replay segment. Selection is ex-post stratified discovery, not causal deployment/OOS validation.

## 2. Starting and checkpoint captures

Historical `snapshot` files contain:

- `slot`, `timestamp_ms`, `boundary` (`END_SLOT` for starts/checkpoints).
- `state`: `PoolState.to_dict()` including canonical token identities and decoded bins.
- `source_kind`: one of `ARCHIVAL_RPC`, `GEYSER_ACCOUNT_CAPTURE`, `SVM_VALIDATED_CAPTURE`, `RECORDED_FINALIZED_RPC`.
- `raw_accounts`: address → standard Solana account object (`data: [base64,"base64"]`, owner, lamports, executable, etc.). Include LbPair, relevant complete bin arrays and Clock. Optional absent accounts use null.
- `raw_accounts_sha256`: canonical JSON digest of the raw account dictionary (`mdlmm.util.digest`).
- `raw_sha256`, `source_receipt`: original export/response evidence identifiers, not just a caller's decoded values.

Raw bytes are independently decoded and compared to supplied state. Clock slot and unix timestamp must match capture slot/time. A CURRENT capture is never accepted as a historical start by merely changing the start date. An export's source attestation is hash-bound but still needs trusted acquisition; no tool can authenticate a fabricated archive just because it was hashed.

Checkpoints JSON is `{"checkpoints":[{"event_id":"ACTUAL_EVENT_ID", "capture":{"path":"...","sha256":"..."}}]}`. Every listed checkpoint must be reached, and the final event requires an independent ending checkpoint. END_SLOT checkpoints must follow the last financial event in that slot.

## 3. Program registry

Each entry uses an exclusive end slot:

```json
{"entries":[{
  "program_id":"LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
  "start_slot":0,"end_slot":1,"version":"0.12.0","model":"MM_V012",
  "binary_file":"programs/deployed.so","binary_sha256":"ACTUAL_SHA256",
  "idl_file":"programs/idl.json","idl_sha256":"ACTUAL_SHA256",
  "binding_evidence_file":"programs/deployment_evidence.json",
  "binding_evidence_sha256":"ACTUAL_SHA256"
}]}
```

No interval is prefilled in the example package as if historical deployment were known. The current SDK revision is not proof for a past slot. Intervals cannot overlap. Every event must match the interval's semantic version/binary hash/IDL hash. Only the audited `MM_V012` direct model is supported. Earlier incompatible layouts need a distinct implemented model; unsupported ones stop rather than using the newest version.

## 4. Raw coverage and Clock

Two coverage modes are supported:

**FULL_BLOCK_SCAN:** produced slots and full `getBlock` payloads over explicit boundaries, plus collector receipts, no missing slots/events claimed as complete. Empty/skipped Solana slots are distinguished from produced blocks omitted from the archive.

**POOL_ADDRESS_SIGNATURE_SCAN:** raw paginated finalized `getSignaturesForAddress` responses starting at the current head and continuing strictly before the range start; full blocks for every relevant slot; all indexed signatures present. Completeness remains conditional on provider address-index completeness. An exhausted archive does not establish that older activity never existed.

The supplied coverage references raw block JSON wrappers `{slot,block,raw_sha256}`, signature pages where applicable, a declared exclusive slot range, and `clocks: {path,sha256}`. A Clock table contains:

```json
{"clocks":[{"slot":123,"capture":{"path":"clocks/123.json","sha256":"ACTUAL_SHA256"}}]}
```

Each Clock capture is `{"pubkey":"SysvarC1ock11111111111111111111111111111111", "account":{"data":["RAW_BASE64","base64"]}, "source_receipt":"ORIGINAL_SOURCE"}`. Do not synthesize Clock from OHLCV, current state, or approximate block timestamps. D0 re-normalizes raw swaps and compares event IDs/payloads to prevent omitted events. Hash lists alone do not suffice.

Collectors write coverage paths relative to their own output directory. When assembling a larger bundle, relocate referenced files and update references/hashes explicitly. `prepare-historical` expects all references inside the supplied coverage to be relative to the **bundle root**. Never change file contents without updating parent hashes, and rerun D0 after input changes.

## 5. Normalized events

The machine schema is `schemas/event.schema.json`. Key fields:

- `order`: slot, transaction_index, instruction_index, inner_instruction_index (-1 for top level), event_index.
- `block_time_ms`, tx_signature, pool, kind, program_id.
- program_version, binary_sha256, idl_hash, clock_unix_timestamp, clock_source.
- `observed`: actual start/end bin and swap output/total/recipient fee quantities.
- `payload`: exact input amount/direction, fee update or independently observed liquidity delta.
- optional pre_state_hash/post_state_hash; raw_sha256; data_kind.

Amounts are integer atomic token units, not UI floats. Prices in state are integer Q64.64. SOL/USDC orientation is determined from actual mints. Slots and timestamps are separate. Unknown field/version/instruction handling cannot silently advance state.

Normalized swaps are observations, not permission to run an unsupported path. Token-2022/LO/referral/exact-out events can remain raw observations while the strict financial path is blocked.

## 6. Observed liquidity transitions

Aggregate `AddLiquidity`/`RemoveLiquidity` amounts do not uniquely specify every bin after a complex operation. For `OBSERVED_LIQUIDITY`, provide:

- raw before/after captures at `BEFORE_INSTRUCTION`/`AFTER_INSTRUCTION`;
- exact tx_signature and instruction_order in each capture;
- pre/post economic state hashes;
- `bins_after` changed bin state, verified against those raw bytes;
- external_flow_x/y integer token transfers with an independently sourced `token_flow_evidence` file bound to the same instruction;
- any LP/protocol/composition fee allocation needed by the event ledger, separately reconciled.

The direct model applies only these verified state changes; it does not claim to simulate every liquidity instruction's deposit strategy. A mixed swap+liquidity outer instruction is currently blocked unless exported/handled at the supported granular boundary. Position ownership is decoded when recoverable, not invented from wallet totals.

## 7. Preparation plan

`configs/examples/archival-plan.example.json` shows the input structure. Fill eight segments (four windows × two pools), real raw captures/coverage/Clock/version registry and external reference. The plan uses paths before sealing; `prepare-historical` constructs normalized event/checkpoint/coverage files and a hashed dataset. Existing output is refused. It may leave partial artifacts with an unavailable/failure receipt; use a new export directory after correcting evidence, rather than overriding history.

## 8. Reference data

CSV header or equivalent JSONL keys:

```text
ts_ms,available_ms,price,source,symbol
```

`symbol` is exactly `SOL/USDC`, timestamps are Unix milliseconds, price is a positive decimal string, source is explicit, available_ms ≥ ts_ms. No duplicate timestamps. A SOL/USD or SOL/USDT series needs an independently justified conversion before import; renaming the symbol is not such a conversion. Future markout observations are permitted only by the ex-post diagnostic API, never by policy/as-of lookup.

## 9. Hyperliquid market bundle

```json
{"data_kind":"HISTORICAL","end_ms":0,"observation_delay_ms":1000,
 "books":{"path":"books.jsonl.lz4","sha256":"ACTUAL_SHA256"},
 "terms":{"path":"terms.json","sha256":"ACTUAL_SHA256"},
 "funding":{"path":"funding.json","sha256":"ACTUAL_SHA256"},
 "decisions":{"path":"decisions.jsonl","sha256":"ACTUAL_SHA256"}}
```

Books accept canonical `{coin:"SOL",ts_ms,available_ms,bids:[[price,size],...],asks:...,source}` or native `data`/`levels` form. If native archives lack observation time, a declared observation_delay_ms is required. L2 snapshots must have strictly ordered levels and uncrossed bids/asks. Repeated timestamps must be explicitly reconciled, not dropped silently.

Terms: `{"intervals":[{start_ms,end_ms,maker_rate,taker_rate,lot_sol,initial_margin_rate,maintenance_margin_rate,minimum_notional,source}]}`. No historical default is supplied.

Funding: `{coverage_start_ms,coverage_end_ms,source,events:[{time_ms,rate,oracle_price}]}` with exact payable UTC hours. Missing rates/oracle or uncovered holding interval is unavailable. `funding-rates` only obtains rates; `funding-join` requires independent Hyperliquid oracle evidence.

Decisions JSONL: `{time_ms,lp_delta_sol,nav_usdc,reference_price}`. This is an explicit downstream interface, not a claim that every D1 trace has already been translated into a feasible live strategy. D3 reserves its supplied isolated collateral; compare results only with full portfolio NAV, not LP deployed capital alone.

## 10. Immutable artifacts

Every stage has `run_started.json`, `result.json`, and final `manifest.json` with code/input/artifact hashes. Extractors verify stored artifact hashes. Incomplete/interrupted runs lack a final manifest and cannot become parents. `validation/authoring_report.json` is a package-delivery report; it is not a historical D0 receipt and cannot open a gate.
