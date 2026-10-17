#!/usr/bin/env python3
"""Re-audit 11 V6 isolated boundaries against raw RPC evidence."""

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from mdlmm.codec import IdlCodec, b58decode
from mdlmm.corpus_worklist import (
    scoped_dlmm_instructions, sha_file
)
from mdlmm.model import PROGRAM_ID
from mdlmm.network import RawStore
from mdlmm.normalize import (
    _invocations, account_keys, normalize_block
)
from mdlmm.observed_slot_transition import ObservedBoundaryLedger


ROOT = Path.cwd()
V6 = ROOT / "runs/d1_isolated_mutation_pilot_v6_20261017T081517Z"

PIN = "682801d6978052f3b37825ac0e85d96f0c6f7868"
V6_SHA = "c55d0fd47c0790ff21b46ed97638c975c79b41a2c24a0cb43aea29f125982dee"
IDL_BLOB = "6715f5cf19dffa8651a185ac0dd823218c6a2a75"
POOL = "BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh"

POOL_ROLES = {
    "lb_pair", "bin_array", "bin_array_lower", "bin_array_upper",
    "bin_array_bitmap_extension", "position", "limit_order",
    "oracle", "bin_array_1", "bin_array_2",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load(path):
    return json.loads(path.read_text())


def save(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:
        json.dump(
            document, f, sort_keys=True,
            indent=2, ensure_ascii=False
        )
        f.write("\n")


head = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], text=True
).strip()

require(head == PIN, "source commit mismatch")
require(sha_file(V6 / "summary.json") == V6_SHA,
        "V6 summary hash mismatch")

source = load(V6 / "summary.json")

require(
    source["results"] == {
        "PASS_ISOLATED_BLOCK_BOUNDARY_WITNESS": 11,
        "UNAVAILABLE": 5,
    },
    "V6 result count mismatch",
)

for key, filename in (
    ("result_inventory_sha256", "per_candidate_results.json"),
    ("candidate_inventory_sha256", "selected_gfta_candidates.json"),
):
    require(
        sha_file(V6 / filename) == source[key],
        "V6 inventory hash mismatch",
    )

idl_path = (
    ROOT / "external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json"
)
idl_bytes = idl_path.read_bytes()
idl_blob = hashlib.sha1(
    b"blob " + str(len(idl_bytes)).encode()
    + b"\0" + idl_bytes
).hexdigest()

require(idl_blob == IDL_BLOB, "pinned IDL drift")

idl = IdlCodec.load(idl_path)
raw_store = RawStore(V6 / "raw_rpc")

results = load(V6 / "per_candidate_results.json")
require(len(results) == 16, "V6 candidate count mismatch")

by_type = Counter()
account_status = Counter()
account_types = Counter()
bin_fields = Counter()
details = []

print("===== V7 HISTORICAL OBSERVED TRANSITION AUDIT =====")

for rec in results:

    if rec["status"] == "UNAVAILABLE":
        continue

    require(
        rec["status"] == "PASS_ISOLATED_BLOCK_BOUNDARY_WITNESS",
        "unexpected V6 candidate status",
    )

    slot = rec["slot"]

    proof = load(V6 / "proofs" / f"{slot}.json")
    saved = load(V6 / "blocks" / f"{slot}.json")

    require(
        proof["slot"] == saved["slot"] == slot
        and proof["pool"] == saved["pool"] == POOL
        and proof["name"] == rec["name"],
        "block/proof identity mismatch",
    )

    require(proof["args_status"] == "PASS",
            "mutation instruction arguments unparseable")

    require(
        saved["raw_sha256"]
        == proof["block_raw_rpc_sha256"]
        == rec["block_raw_rpc_sha256"],
        "raw block provenance mismatch",
    )

    original = raw_store.get(saved["raw_sha256"])
    block = saved["block"]

    require(
        original.get("result") == block,
        "original RPC receipt differs from block",
    )

    norm = normalize_block(
        slot, block, {POOL},
        raw_hash=saved["raw_sha256"],
        idl=idl,
        require_canonical_swaps=True,
    )

    require(
        norm["canonical_swap_count"] == 0,
        "mutation-only block contains swaps",
    )

    calls = []

    for i, item in enumerate(block["transactions"]):
        tx, meta = item["transaction"], item["meta"]

        if meta is None or meta.get("err") is not None:
            continue

        if POOL not in account_keys(tx, meta):
            continue

        for call in scoped_dlmm_instructions(
            item, POOL, idl
        ):
            calls.append(
                (i, tx["signatures"][0], call)
            )

    require(
        len(calls) == 1
        and calls[0][2]["owner"] == "TARGET",
        "multiple or unresolved mutation ownership",
    )

    tx_index, signature, call = calls[0]
    loc = proof["instruction_order"]

    require(
        signature == proof["signature"]
        and tx_index == loc["tx_index"]
        and call["outer"] == loc["outer"]
        and call["inner"] == loc["inner"]
        and call["name"] == proof["name"],
        "instruction ordering/provenance mismatch",
    )

    item = block["transactions"][tx_index]
    tx, meta = item["transaction"], item["meta"]
    keys = account_keys(tx, meta)

    outer = loc["outer"]
    inner = loc["inner"]

    group = next(
        (
            g["instructions"]
            for g in meta.get("innerInstructions") or []
            if g["index"] == outer
        ),
        [],
    )

    nodes = _invocations(
        tx["message"]["instructions"][outer],
        group, keys, outer,
    )

    node = next(
        (x for x in nodes if x["inner_index"] == inner),
        None,
    )

    require(
        node is not None
        and node.get("resolved") is not None
        and node.get("program") == PROGRAM_ID,
        "actual DLMM instruction missing",
    )

    raw = node["resolved"]["raw"]
    accounts = node["resolved"]["accounts"]

    require(
        hashlib.sha256(raw).hexdigest()
        == proof["raw_instruction_sha256"],
        "instruction byte hash mismatch",
    )

    decoded = idl.decode_named("instructions", raw)

    require(
        decoded["name"] == proof["name"]
        and decoded["data"] == proof["args"],
        "instruction argument discrepancy",
    )

    decl = next(
        x for x in idl.idl["instructions"]
        if x["name"] == proof["name"]
    )

    specs = []

    def flatten(entries):
        for entry in entries:
            if "accounts" in entry:
                flatten(entry["accounts"])
            else:
                specs.append(entry)

    flatten(decl["accounts"])

    require(
        len(accounts) >= len(specs),
        "IDL account vector truncated",
    )

    roles = {
        x["name"]: accounts[i]
        for i, x in enumerate(specs)
    }

    require(
        roles.get("lb_pair") == POOL,
        "foreign pool instruction",
    )

    expected = {
        accounts[i]
        for i, x in enumerate(specs)
        if x["name"] in POOL_ROLES
        and (x.get("writable") or x["name"] == "lb_pair")
        and accounts[i] != PROGRAM_ID
        and len(b58decode(accounts[i])) == 32
    }

    expected.update(
        a for a in accounts[len(specs):]
        if a != PROGRAM_ID and len(b58decode(a)) == 32
    )

    require(
        expected == set(proof["observed_account_set"]),
        "captured account set differs from actual IDL accounts",
    )

    for i, other in enumerate(block["transactions"]):
        if i == tx_index:
            continue

        other_meta = other["meta"]

        if other_meta is None or other_meta.get("err") is not None:
            continue

        touched = set(
            account_keys(other["transaction"], other_meta)
        )

        require(
            not (touched & expected),
            "other transaction touches captured state account",
        )

    before = {}
    after = {}

    for address in expected:
        before[address] = load(
            V6 / "accounts" / str(slot - 1)
            / f"{address}.json"
        )
        after[address] = load(
            V6 / "accounts" / str(slot)
            / f"{address}.json"
        )

    ledger = ObservedBoundaryLedger(
        POOL, slot - 1, before
    )

    receipt = ledger.apply(
        proof, before, after
    )

    require(
        ledger.slot == slot,
        "observed ledger did not advance",
    )

    for item in receipt["account_changes"]:
        account_status[item["status"]] += 1
        account_types[item["type"]] += 1
        bin_fields.update(
            item.get("changed_field_counts", {})
        )

    by_type[proof["name"]] += 1

    details.append({
        "slot": slot,
        "name": proof["name"],
        "result": "PASS_OBSERVED_CAPTURED_BOUNDARY",
        "transaction_index": tx_index,
        "account_count": len(expected),
        "block_sha256": saved["raw_sha256"],
        "account_changes": receipt["account_changes"],
        "mathematical_semantics": "NOT_TESTED",
        "instruction_prestate": "UNAVAILABLE",
    })

    print(
        "BOUNDARY",
        slot,
        proof["name"],
        "PASS",
        "ACCOUNTS", len(expected),
        "ACCOUNT_TYPES",
        dict(Counter(
            a["type"] for a in receipt["account_changes"]
        )),
        flush=True,
    )

require(
    len(details) == 11,
    "not all isolated boundary proofs passed",
)

report = {
    "schema": "D1_OBSERVED_SLOT_TRANSITION_V7",
    "source_commit": PIN,
    "source_V6_sha256": V6_SHA,
    "scope": "EXACT_CAPTURED_ACCOUNT_SET_ONLY",
    "isolated_boundaries_passed": len(details),
    "unavailable_existing_candidates": 5,
    "boundary_by_instruction": dict(by_type),
    "account_status_counts": dict(account_status),
    "account_type_counts": dict(account_types),
    "changed_bin_field_counts": dict(bin_fields),
    "mathematical_non_swap_mutation_parity": "NOT_TESTED",
    "full_account_write_coverage": "NOT_PROVEN",
    "intra_block_instruction_prestates": "UNAVAILABLE",
    "D1_HISTORICAL_GATE": "UNAVAILABLE",
}

stamp = datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%SZ"
)
out = ROOT / "runs" / (
    "d1_observed_slot_transition_v7_" + stamp
)

save(out / "details.json", details)
save(out / "summary.json", report)

print("\n===== V7 FINAL =====")
print("ISOLATED_BOUNDARY_PROOFS =", len(details))
print("INSTRUCTION_TYPES =", dict(by_type))
print("CAPTURED_ACCOUNT_TYPES =", dict(account_types))
print("ACCOUNT_STATUS_COUNTS =", dict(account_status))
print(
    "TOP_CHANGED_BIN_FIELDS =",
    bin_fields.most_common(15),
)
print("NON_SWAP_PROGRAM_MODEL_PARITY = NOT_TESTED")
print("INSTRUCTION_PRESTATE = UNAVAILABLE")
print("D1_HISTORICAL_GATE = UNAVAILABLE")
print("REPORT =", out / "summary.json")
print(
    "REPORT_SHA256 =",
    sha_file(out / "summary.json"),
)
