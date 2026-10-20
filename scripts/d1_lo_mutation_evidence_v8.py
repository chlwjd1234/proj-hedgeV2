#!/usr/bin/env python3
"""Independent factual LO instruction/account evidence.

Scope:
  Isolated place_limit_order and cancel_limit_order cases.
  No inferred program state writes.
  No full-D1 or mathematical mutation parity claim.
"""

import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from mdlmm.codec import (
    IdlCodec, b58encode, decode_bin_array, discriminator
)
from mdlmm.corpus_worklist import sha_file
from mdlmm.network import RawStore
from mdlmm.normalize import (
    _invocations, account_keys, resolve_ix, EVENT_CPI_TAG
)
from mdlmm.model import PROGRAM_ID
from mdlmm.observed_slot_transition import (
    ObservedBoundaryLedger, account_raw
)

ROOT = Path.cwd()

V6 = ROOT / (
    "runs/d1_isolated_mutation_pilot_v6_20261017T081517Z"
)
V7 = ROOT / (
    "runs/d1_observed_slot_transition_v7_20261017T090141Z"
)

V6_SHA = (
    "c55d0fd47c0790ff21b46ed97638c975c79b41a2c24a0cb43aea29f125982dee"
)
V7_SHA = (
    "0726927a9a613e44cf63e00fb62719907d27f60e18489ba0ccd31b27fb33aa54"
)
IDL_BLOB = "6715f5cf19dffa8651a185ac0dd823218c6a2a75"

POOL = "BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh"

EXPECTED = {
    "place_limit_order": 2,
    "cancel_limit_order": 2,
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load(path):
    return json.loads(path.read_text())


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(
            value, f,
            sort_keys=True, indent=2,
            ensure_ascii=False
        )
        f.write("\n")


def belongs_to(node, target):
    current = node.get("parent")
    while current is not None:
        if current is target:
            return True
        current = current.get("parent")
    return False


def lo_account_prefix(raw):
    """Decode only the pinned IDL's fixed LimitOrder header."""
    if raw is None:
        return None

    require(
        raw[:8] == discriminator("account", "LimitOrder"),
        "unexpected LimitOrder discriminator"
    )
    require(len(raw) >= 120, "truncated LimitOrder account")

    return {
        "pool": b58encode(raw[8:40]),
        "owner": b58encode(raw[40:72]),
        "bin_count": int.from_bytes(raw[72:74], "little"),
        "raw_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "dynamic_bin_data": "NOT_DECODED",
    }


def array_deltas(before, after):
    a = decode_bin_array(before)
    b = decode_bin_array(after)

    require(
        a["pool"] == b["pool"] == POOL
        and a["index"] == b["index"],
        "BinArray identity mismatch"
    )

    changed = []

    for old, new in zip(a["bins"], b["bins"]):
        require(
            old["bin_id"] == new["bin_id"],
            "Bin ID mismatch"
        )

        differences = {
            k: {
                "before": old[k],
                "after": new[k],
                "delta": new[k] - old[k],
            }
            for k in old
            if k != "bin_id"
            and old[k] != new[k]
            and type(old[k]) is int
            and type(new[k]) is int
        }

        if differences:
            changed.append({
                "bin_id": old["bin_id"],
                "fields": differences,
            })

    return changed


def token_cpis(nodes, target, top, inner, keys, roles):
    token_programs = {
        value for key, value in roles.items()
        if key in (
            "token_program",
            "token_x_program",
            "token_y_program",
            "token_program_x",
            "token_program_y",
        )
    }

    transfers = []
    unparsed = []

    for node in nodes:
        if not belongs_to(node, target):
            continue

        if node.get("program") not in token_programs:
            continue

        index = node["inner_index"]
        ix = top if index == -1 else inner[index]

        try:
            resolved = resolve_ix(ix, keys)
        except Exception as exc:
            unparsed.append({
                "inner": index,
                "error": type(exc).__name__,
            })
            continue

        raw = resolved["raw"]
        acc = resolved["accounts"]

        if raw and raw[0] == 3 and len(raw) == 9 and len(acc) >= 2:
            kind = "Transfer"
            src, dst = acc[0], acc[1]
        elif raw and raw[0] == 12 and len(raw) == 10 and len(acc) >= 3:
            kind = "TransferChecked"
            src, dst = acc[0], acc[2]
        else:
            unparsed.append({
                "inner": index,
                "opcode": raw[0] if raw else None,
            })
            continue

        transfers.append({
            "inner": index,
            "kind": kind,
            "source": src,
            "destination": dst,
            "amount": int.from_bytes(raw[1:9], "little"),
            "source_role": next(
                (k for k, v in roles.items() if v == src), None
            ),
            "destination_role": next(
                (k for k, v in roles.items() if v == dst), None
            ),
        })

    return transfers, unparsed


def main():
    require(sha_file(V6 / "summary.json") == V6_SHA,
            "V6 summary drift")
    require(sha_file(V7 / "summary.json") == V7_SHA,
            "V7 summary drift")
    require(
        load(V7 / "summary.json")["isolated_boundaries_passed"] == 11,
        "V7 witness gate unavailable"
    )

    idl_path = ROOT / (
        "external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json"
    )

    raw = idl_path.read_bytes()
    blob = hashlib.sha1(
        b"blob " + str(len(raw)).encode() + b"\0" + raw
    ).hexdigest()

    require(blob == IDL_BLOB, "pinned official IDL drift")

    idl = IdlCodec.load(idl_path)

    cases = [
        r for r in load(V6 / "per_candidate_results.json")
        if r["status"] == "PASS_ISOLATED_BLOCK_BOUNDARY_WITNESS"
        and r["name"] in EXPECTED
    ]

    require(
        Counter(r["name"] for r in cases) == EXPECTED,
        "LO historical case inventory mismatch"
    )

    store = RawStore(V6 / "raw_rpc")
    field_counts = Counter()
    results = []

    print("===== V8 HISTORICAL LO MECHANICS EVIDENCE =====")

    for case in cases:
        slot = case["slot"]
        name = case["name"]

        proof = load(V6 / "proofs" / f"{slot}.json")
        saved = load(V6 / "blocks" / f"{slot}.json")

        require(
            proof["slot"] == saved["slot"] == slot,
            "slot mismatch"
        )
        require(
            proof["pool"] == saved["pool"] == POOL,
            "pool mismatch"
        )
        require(
            proof["name"] == name
            and proof["args_status"] == "PASS",
            "instruction arguments unavailable"
        )

        raw_sha = saved["raw_sha256"]

        require(
            raw_sha == proof["block_raw_rpc_sha256"]
            == case["block_raw_rpc_sha256"],
            "block provenance mismatch"
        )

        block = saved["block"]

        require(
            store.get(raw_sha).get("result") == block,
            "original RPC data mismatch"
        )

        loc = proof["instruction_order"]
        tx_index = loc["tx_index"]
        item = block["transactions"][tx_index]
        tx, meta = item["transaction"], item["meta"]

        require(
            meta["err"] is None
            and tx["signatures"][0] == proof["signature"],
            "transaction status or signature mismatch"
        )

        keys = account_keys(tx, meta)

        outer = loc["outer"]

        inner = next(
            (
                g["instructions"]
                for g in meta.get("innerInstructions") or []
                if g["index"] == outer
            ),
            [],
        )

        top = tx["message"]["instructions"][outer]
        nodes = _invocations(top, inner, keys, outer)

        target = next(
            (
                n for n in nodes
                if n["inner_index"] == loc["inner"]
            ),
            None,
        )

        require(
            target is not None
            and target.get("program") == PROGRAM_ID
            and target.get("resolved") is not None,
            "target instruction missing"
        )

        ix_raw = target["resolved"]["raw"]

        require(
            hashlib.sha256(ix_raw).hexdigest()
            == proof["raw_instruction_sha256"],
            "instruction bytes changed"
        )

        instruction = idl.decode_named(
            "instructions", ix_raw
        )

        require(
            instruction["name"] == name
            and instruction["data"] == proof["args"],
            "instruction argument mismatch"
        )

        declaration = next(
            d for d in idl.idl["instructions"]
            if d["name"] == name
        )

        fixed = declaration["accounts"]
        addresses = target["resolved"]["accounts"]

        require(
            len(addresses) >= len(fixed),
            "instruction account vector truncated"
        )

        roles = {
            spec["name"]: addresses[i]
            for i, spec in enumerate(fixed)
        }

        require(
            roles["lb_pair"] == POOL,
            "foreign pool mutation"
        )

        lo_address = roles["limit_order"]

        require(
            lo_address in proof["observed_account_set"],
            "LimitOrder account missing from proof"
        )

        # CPI events must descend from this particular mutation,
        # not merely occur somewhere in the same transaction.
        events = []

        for node in nodes:
            resolved = node.get("resolved")

            if (
                resolved is not None
                and node.get("program") == PROGRAM_ID
                and belongs_to(node, target)
                and resolved["raw"].startswith(EVENT_CPI_TAG)
            ):
                events.append(
                    idl.decode_named(
                        "events",
                        resolved["raw"][8:]
                    )
                )

        event_name = (
            "PlaceLimitOrderEvt"
            if name == "place_limit_order"
            else "CancelLimitOrderEvt"
        )

        matching = [
            e["data"] for e in events
            if e["name"] == event_name
        ]

        require(
            len(matching) == 1,
            "LO event CPI is missing or ambiguous"
        )

        event = matching[0]

        require(
            event["lb_pair"] == POOL
            and event["limit_order"] == lo_address,
            "LO event account/pool mismatch"
        )

        before = {}
        after = {}

        for address in proof["observed_account_set"]:
            before[address] = load(
                V6 / "accounts" / str(slot - 1)
                / f"{address}.json"
            )
            after[address] = load(
                V6 / "accounts" / str(slot)
                / f"{address}.json"
            )

        # Independent boundary witness is mandatory.
        ledger = ObservedBoundaryLedger(
            POOL, slot - 1, before
        )
        boundary = ledger.apply(proof, before, after)

        require(ledger.slot == slot, "boundary did not advance")

        changed_bins = []

        for address in proof["observed_account_set"]:
            pre_raw = account_raw(
                before[address], address, slot - 1
            )
            post_raw = account_raw(
                after[address], address, slot
            )

            if (
                pre_raw is not None
                and post_raw is not None
                and pre_raw[:8] == discriminator("account", "BinArray")
            ):
                changed_bins.extend(
                    array_deltas(pre_raw, post_raw)
                )

        for entry in changed_bins:
            field_counts.update(entry["fields"].keys())

        lo_pre = account_raw(
            before[lo_address], lo_address, slot - 1
        )
        lo_post = account_raw(
            after[lo_address], lo_address, slot
        )

        lo_before = lo_account_prefix(lo_pre)
        lo_after = lo_account_prefix(lo_post)

        require(
            all(
                x is None or x["pool"] == POOL
                for x in (lo_before, lo_after)
            ),
            "LimitOrder account belongs to another pool"
        )

        transfers, unparsed = token_cpis(
            nodes, target, top, inner, keys, roles
        )

        args = instruction["data"]

        if name == "place_limit_order":
            requested = args["params"]["bins"]

            amount_by_bin = {
                int(x["id"]): int(x["amount"])
                for x in requested
            }

            delta_open = {
                x["bin_id"]:
                    x["fields"]["open_order_amount"]["delta"]
                for x in changed_bins
                if "open_order_amount" in x["fields"]
            }

            # This is an empirical hypothesis, NOT a program rule.
            if len(requested) != len(amount_by_bin):
                hypothesis = "UNAVAILABLE_DUPLICATE_BIN"
            elif args["params"]["relative_bin"] is not None:
                hypothesis = "UNAVAILABLE_RELATIVE_BIN"
            elif (
                all(
                    delta_open.get(i, 0) == quantity
                    for i, quantity in amount_by_bin.items()
                )
                and set(delta_open) <= set(amount_by_bin)
            ):
                hypothesis = "MATCH_IN_THIS_SAMPLE"
            else:
                hypothesis = "NOT_MATCHED_IN_THIS_SAMPLE"

            comparison = {
                "requested_bins": requested,
                "requested_total": sum(amount_by_bin.values()),
                "actual_open_order_deltas": delta_open,
                "open_order_amount_hypothesis": hypothesis,
                "event_params_match_instruction":
                    event.get("params") == args["params"],
            }

        else:
            comparison = {
                "cancel_bins": args["bins"],
                "event_bins_match_instruction":
                    event.get("bins") == args["bins"],
                "event_refund_amounts": event.get("amounts"),
            }

        result = {
            "slot": slot,
            "name": name,
            "signature": proof["signature"],
            "tx_index": tx_index,
            "instruction_order": loc,
            "instruction_args": args,
            "instruction_roles": roles,
            "event": event,
            "account_changes": boundary["account_changes"],
            "bin_deltas": changed_bins,
            "limit_order_before": lo_before,
            "limit_order_after": lo_after,
            "spl_token_transfers": transfers,
            "unparsed_token_cpis": unparsed,
            "empirical_comparison": comparison,
            "mathematical_mutation_parity": "NOT_VALIDATED",
        }

        results.append(result)

        print(
            "LO_CASE",
            slot,
            name,
            "BOUNDARY=PASS",
            "CHANGED_BINS=", len(changed_bins),
            "SPL_TRANSFERS=", len(transfers),
            "COMPARISON=", comparison,
            flush=True,
        )

    require(len(results) == 4, "missing isolated LO cases")

    summary = {
        "schema": "D1_LO_MUTATION_EVIDENCE_V8",
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True
        ).strip(),
        "historical_cases": 4,
        "instruction_types": dict(
            Counter(x["name"] for x in results)
        ),
        "changed_bin_field_counts": dict(field_counts),
        "token_cpi_transfers": sum(
            len(x["spl_token_transfers"])
            for x in results
        ),
        "unparsed_token_cpis": sum(
            len(x["unparsed_token_cpis"])
            for x in results
        ),
        "evidence_gate": "PASS_ISOLATED_BOUNDARY",
        "mathematical_mutation_parity": "NOT_VALIDATED",
        "full_write_set_completeness": "NOT_PROVEN",
        "intra_block_swap_prestate": "UNAVAILABLE",
        "D1_HISTORICAL_GATE": "UNAVAILABLE",
    }

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )

    output = ROOT / "runs" / (
        "d1_lo_mutation_evidence_v8_" + stamp
    )

    write_new(output / "cases.json", results)
    write_new(output / "summary.json", summary)

    print("\n===== V8 FINAL =====")

    for key, value in summary.items():
        print(key.upper(), "=", value)

    for case in results:
        print(
            "CASE",
            case["slot"],
            case["name"],
            "BIN_FIELDS=",
            dict(Counter(
                field
                for item in case["bin_deltas"]
                for field in item["fields"]
            )),
            "LO_BEFORE=",
            None if case["limit_order_before"] is None
            else case["limit_order_before"]["bin_count"],
            "LO_AFTER=",
            None if case["limit_order_after"] is None
            else case["limit_order_after"]["bin_count"],
        )

    print("REPORT =", output / "summary.json")
    print(
        "REPORT_SHA256 =",
        sha_file(output / "summary.json")
    )


if __name__ == "__main__":
    main()
