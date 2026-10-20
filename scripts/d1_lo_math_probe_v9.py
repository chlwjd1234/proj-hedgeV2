#!/usr/bin/env python3
"""Restricted LO mutation arithmetic against original historical account bytes.

Place: exact requested-bin increments.
Cancel: only orders proven NOT_FILLED by pinned SDK age semantics.

This does not establish complete LimitOrder account mutation parity,
intra-block instruction prestates, or full historical replay.
"""

import hashlib
import json
import time
from pathlib import Path
from collections import Counter

from mdlmm.codec import (
    IdlCodec, b58encode, decode_bin_array, discriminator
)
from mdlmm.corpus_worklist import sha_file
from mdlmm.network import RawStore
from mdlmm.observed_slot_transition import (
    ObservedBoundaryLedger, account_raw
)
from mdlmm.errors import ValidationError

ROOT = Path.cwd()

V6 = ROOT / (
    "runs/d1_isolated_mutation_pilot_v6_20261017T081517Z"
)
V8 = ROOT / (
    "runs/d1_lo_mutation_evidence_v8_20261017T095151Z"
)

V6_SHA = "c55d0fd47c0790ff21b46ed97638c975c79b41a2c24a0cb43aea29f125982dee"
V8_SHA = "ecce8c6b92a85520d19da0424018c94e0bdeece1b744a5652a5677bfaf196ccb"

POOL = "BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh"
U64_MAX = 2**64 - 1

BIN_DISC = discriminator("account", "BinArray")
LO_DISC = discriminator("account", "LimitOrder")


def require(condition, reason):
    if not condition:
        raise ValidationError(reason)


def read(path):
    return json.loads(path.read_text())


def decode_lo(raw):
    if raw is None:
        return None

    require(
        len(raw) >= 120 and raw[:8] == LO_DISC,
        "Invalid historical LimitOrder layout"
    )

    count = int.from_bytes(raw[72:74], "little")

    require(
        count <= 1000 and len(raw) >= 120 + count * 32,
        "Truncated LimitOrder bin records"
    )

    bins = []

    for i in range(count):
        offset = 120 + i * 32

        side = raw[offset + 20]
        require(side in (0, 1), "Invalid LO side")

        bins.append({
            "amount": int.from_bytes(
                raw[offset:offset+8], "little"
            ),
            "age": int.from_bytes(
                raw[offset+8:offset+12], "little"
            ),
            "bin_id": int.from_bytes(
                raw[offset+16:offset+20],
                "little", signed=True
            ),
            "is_ask": bool(side),
        })

    return {
        "pool": b58encode(raw[8:40]),
        "owner": b58encode(raw[40:72]),
        "bin_count": count,
        "bins": bins,
    }


def patch_open_order(raw, deltas):
    """Pure byte-level prediction, never use observed post values."""

    a = decode_bin_array(raw)
    require(a["pool"] == POOL, "Foreign BinArray")

    index = a["index"]
    result = bytearray(raw)

    for bin_id, delta in deltas.items():

        if bin_id // 70 != index:
            continue

        local = bin_id - index * 70
        require(0 <= local < 70, "Invalid bin position")

        # Pinned BinArray layout:
        # 56-byte header, 144-byte bins,
        # open_order_amount at byte 112 within Bin.
        offset = 56 + local * 144 + 112

        old = int.from_bytes(
            result[offset:offset+8], "little"
        )

        require(
            old == a["bins"][local]["open_order_amount"],
            "BinArray raw layout and decoder disagree"
        )

        new = old + delta

        require(
            0 <= new <= U64_MAX,
            "Open-order amount overflow/underflow"
        )

        result[offset:offset+8] = new.to_bytes(
            8, "little"
        )

    return bytes(result), a


def main():

    require(
        sha_file(V6 / "summary.json") == V6_SHA,
        "V6 evidence hash mismatch"
    )
    require(
        sha_file(V8 / "summary.json") == V8_SHA,
        "V8 evidence hash mismatch"
    )

    cases = read(V8 / "cases.json")
    require(len(cases) == 4, "Expected four historical LO cases")

    require(
        Counter(x["name"] for x in cases)
        == {"place_limit_order": 2, "cancel_limit_order": 2},
        "Historical mutation case inventory mismatch"
    )

    store = RawStore(V6 / "raw_rpc")
    results = []

    print("===== V9 RESTRICTED LO MATHEMATICAL PARITY =====")

    for case in cases:

        slot = case["slot"]
        name = case["name"]
        roles = case["instruction_roles"]
        args = case["instruction_args"]
        event = case["event"]

        proof = read(
            V6 / "proofs" / f"{slot}.json"
        )
        block = read(
            V6 / "blocks" / f"{slot}.json"
        )

        require(
            proof["slot"] == block["slot"] == slot,
            "Historical slot mismatch"
        )
        require(
            proof["pool"] == roles["lb_pair"] == POOL,
            "Historical pool mismatch"
        )
        require(
            proof["name"] == name
            and proof["args"] == args
            and proof["signature"] == case["signature"],
            "Actual instruction evidence mismatch"
        )
        require(
            proof["scope"]
            == "ISOLATED_SINGLE_TARGET_MUTATION_FULL_SLOT_BOUNDARY"
            and proof["external_writer_touches"] == []
            and proof["args_status"] == "PASS",
            "Non-isolated or unverified historical mutation"
        )
        require(
            block["raw_sha256"]
            == proof["block_raw_rpc_sha256"],
            "Block hash binding mismatch"
        )
        require(
            store.get(block["raw_sha256"]).get("result")
            == block["block"],
            "Original RPC receipt mismatch"
        )

        pre = {}
        post = {}

        for addr in proof["observed_account_set"]:
            pre[addr] = read(
                V6 / "accounts" / str(slot - 1)
                / f"{addr}.json"
            )
            post[addr] = read(
                V6 / "accounts" / str(slot)
                / f"{addr}.json"
            )

        # Revalidate original archived boundary receipts.
        ledger = ObservedBoundaryLedger(
            POOL, slot - 1, pre
        )
        ledger.apply(proof, pre, post)
        require(ledger.slot == slot, "Boundary continuity failure")

        lo_addr = roles["limit_order"]

        require(
            lo_addr in pre and lo_addr in post,
            "LimitOrder account missing"
        )

        raw_pre = {
            addr: account_raw(cap, addr, slot-1)
            for addr, cap in pre.items()
        }

        raw_post = {
            addr: account_raw(cap, addr, slot)
            for addr, cap in post.items()
        }

        lo_before = decode_lo(raw_pre[lo_addr])
        lo_after = decode_lo(raw_post[lo_addr])

        expected_deltas = {}
        diagnostic = {}
        available = True

        arrays = {}

        for addr, raw in raw_pre.items():
            if raw is not None and raw[:8] == BIN_DISC:
                decoded = decode_bin_array(raw)
                require(
                    decoded["pool"] == POOL,
                    "BinArray pool mismatch"
                )
                require(
                    decoded["index"] not in arrays,
                    "Duplicate BinArray index"
                )
                arrays[decoded["index"]] = (addr, decoded)

        if name == "place_limit_order":

            requested = args["params"]["bins"]

            require(
                args["params"]["relative_bin"] is None,
                "Relative-bin order outside V9 scope"
            )

            require(
                lo_before is None and lo_after is not None,
                "Order creation account lifecycle mismatch"
            )

            require(
                lo_after["pool"] == POOL
                and lo_after["owner"] == roles["owner"],
                "Created LO ownership mismatch"
            )

            for row in requested:
                bin_id = row["id"]
                amount = row["amount"]

                require(
                    type(bin_id) is int
                    and type(amount) is int
                    and 0 < amount <= U64_MAX
                    and bin_id not in expected_deltas,
                    "Invalid/duplicate requested LO bin"
                )

                expected_deltas[bin_id] = amount

            require(
                lo_after["bin_count"] == len(requested),
                "Created LO bin count mismatch"
            )

            created = {
                row["bin_id"]: row
                for row in lo_after["bins"]
            }

            require(
                len(created) == len(requested)
                and set(created) == set(expected_deltas),
                "Created LO bin inventory differs"
            )

            for bin_id, amount in expected_deltas.items():
                require(
                    created[bin_id]["amount"] == amount
                    and created[bin_id]["is_ask"]
                    == args["params"]["is_ask_side"],
                    "Created LO record differs from actual instruction"
                )

            require(
                event["params"] == args["params"],
                "PlaceLimitOrderEvt disagrees with instruction"
            )

            transfers = case["spl_token_transfers"]

            require(
                not case["unparsed_token_cpis"]
                and len(transfers) == 1
                and transfers[0]["amount"] == sum(expected_deltas.values())
                and transfers[0]["source"] == roles["user_token"]
                and transfers[0]["destination"] == roles["reserve"],
                "Place LO SPL Token CPI mismatch"
            )

        elif name == "cancel_limit_order":

            require(
                lo_before is not None
                and lo_before["pool"] == POOL,
                "Cancel LO historical prestate missing"
            )

            records = {
                row["bin_id"]: row
                for row in lo_before["bins"]
            }

            require(
                len(records) == len(lo_before["bins"]),
                "Duplicate LO prestate bin ID"
            )

            requested_bins = args["bins"]

            require(
                len(requested_bins) == len(set(requested_bins)),
                "Duplicate cancel bin ID"
            )
            require(
                event["bins"] == requested_bins,
                "CancelLimitOrderEvt bin mismatch"
            )

            cancel_status = {}
            refund_x = refund_y = 0

            for bin_id in requested_bins:

                require(
                    bin_id in records
                    and bin_id // 70 in arrays,
                    "Cancellation bin missing from captured prestate"
                )

                record = records[bin_id]
                index = bin_id // 70

                previous = arrays[index][1]["bins"][
                    bin_id - index * 70
                ]

                age = record["age"]
                order_age = previous["order_age"]

                # From pinned SDK LimitOrder::get_status().
                if age == order_age:
                    status = "NOT_FILLED"
                    unfilled = record["amount"]
                elif age + 1 == order_age:
                    status = "PARTIAL_FILLED"
                    available = False
                    unfilled = None
                elif age + 2 <= order_age:
                    status = "FULFILLED"
                    available = False
                    unfilled = None
                else:
                    raise ValidationError(
                        "Historical LO age relationship invalid"
                    )

                cancel_status[bin_id] = {
                    "status": status,
                    "lo_age": age,
                    "bin_order_age": order_age,
                    "original_amount": record["amount"],
                    "open_before": previous["open_order_amount"],
                }

                if unfilled is not None:

                    expected_deltas[bin_id] = -unfilled

                    if record["is_ask"]:
                        refund_x += unfilled
                    else:
                        refund_y += unfilled

            diagnostic["cancel_status"] = cancel_status
            diagnostic["event_refund"] = event["amounts"]

            if available:

                require(
                    event["amounts"] == [refund_x, refund_y],
                    "Not-filled cancel refund mathematical mismatch"
                )

                transfers = case["spl_token_transfers"]

                require(
                    not case["unparsed_token_cpis"]
                    and len(transfers) == 1
                    and transfers[0]["amount"] == refund_x + refund_y,
                    "Cancel LO refund CPI mismatch"
                )

        else:
            raise ValidationError("Unsupported mutation type")

        raw_mismatches = []
        observed_deltas = {}

        if available:

            for bin_id in expected_deltas:
                require(
                    bin_id // 70 in arrays,
                    "Predicted bin not covered by archived account"
                )

            for index, (addr, decoded) in arrays.items():

                predicted, _ = patch_open_order(
                    raw_pre[addr], expected_deltas
                )

                actual = raw_post[addr]

                if predicted != actual:
                    first_diff = next(
                        (
                            i for i, (a, b)
                            in enumerate(zip(predicted, actual))
                            if a != b
                        ),
                        min(len(predicted), len(actual))
                    )

                    raw_mismatches.append({
                        "address": addr,
                        "first_diff": first_diff,
                    })

                post_decoded = decode_bin_array(actual)

                for before_bin, after_bin in zip(
                    decoded["bins"], post_decoded["bins"]
                ):
                    delta = (
                        after_bin["open_order_amount"]
                        - before_bin["open_order_amount"]
                    )

                    if delta:
                        observed_deltas[before_bin["bin_id"]] = delta

        if not available:
            status = "UNAVAILABLE_PARTIAL_OR_FILLED_CANCEL"
        elif raw_mismatches:
            status = "FAIL_RAW_BINARRAY_PARITY"
        elif observed_deltas != {
            k: v for k, v in expected_deltas.items() if v
        }:
            status = "FAIL_OPEN_ORDER_DELTA"
        else:
            status = "PASS_RESTRICTED_RAW_BINARRAY"

        result = {
            "slot": slot,
            "instruction": name,
            "status": status,
            "predicted_open_order_deltas": expected_deltas,
            "observed_open_order_deltas": observed_deltas,
            "raw_binarray_mismatches": raw_mismatches,
            "limit_order_before": lo_before,
            "limit_order_after": lo_after,
            "diagnostic": diagnostic,
            "full_mutation_account_parity": "NOT_VALIDATED",
        }

        results.append(result)

        print(
            "CASE", slot, name,
            "STATUS", status,
            "PREDICTED", expected_deltas,
            "OBSERVED", observed_deltas,
            "RAW_MISMATCH", raw_mismatches,
            flush=True
        )

        if diagnostic:
            print("CANCEL_DIAGNOSTIC =", diagnostic, flush=True)

    counts = Counter(x["status"] for x in results)

    summary = {
        "schema": "D1_RESTRICTED_LO_MATH_V9",
        "historical_cases": len(results),
        "status_counts": dict(counts),
        "math_scope": "OPEN_ORDER_AMOUNT_AND_RAW_BINARRAY_ONLY",
        "full_limit_order_mutation_parity": "NOT_VALIDATED",
        "intra_block_instruction_prestate": "UNAVAILABLE",
        "D1_HISTORICAL_GATE": "UNAVAILABLE",
    }

    output = ROOT / "runs" / (
        "d1_lo_math_v9_" + str(time.time_ns())
    )

    output.mkdir(parents=True, exist_ok=False)

    (output / "cases.json").write_text(
        json.dumps(results, sort_keys=True, indent=2) + "\n"
    )
    (output / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n"
    )

    print("\n===== V9 FINAL =====")
    print("STATUS_COUNTS =", dict(counts))
    print("HISTORICAL_CASES =", len(results))
    print("FULL_MUTATION_PARITY = NOT_VALIDATED")
    print("D1_HISTORICAL_GATE = UNAVAILABLE")
    print("REPORT =", output / "summary.json")
    print(
        "REPORT_SHA256 =",
        sha_file(output / "summary.json")
    )

    if any(k.startswith("FAIL") for k in counts):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
