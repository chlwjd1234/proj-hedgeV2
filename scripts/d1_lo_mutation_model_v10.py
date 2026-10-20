#!/usr/bin/env python3
"""Historical V10 replay of four independently captured LO mutations.

Prediction: pre-account bytes + actual instruction arguments ONLY.
Independent oracles: original RPC, events, token CPI, post-account bytes.
No D1 full replay or complete LimitOrder account parity claim.
"""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from mdlmm.codec import IdlCodec
from mdlmm.corpus_worklist import sha_file
from mdlmm.errors import (
    Unavailable, Unsupported, ValidationError
)
from mdlmm.network import RawStore
from mdlmm.normalize import (
    EVENT_CPI_TAG, _invocations, account_keys
)
from mdlmm.observed_slot_transition import (
    ObservedBoundaryLedger, account_raw
)
from mdlmm.lo_order_mutation import (
    predict, verify_economics, lo_prestate
)
from mdlmm.model import PROGRAM_ID

ROOT = Path(__file__).resolve().parents[1]

V6 = ROOT / (
    "runs/d1_isolated_mutation_pilot_v6_20261017T081517Z"
)
V8 = ROOT / (
    "runs/d1_lo_mutation_evidence_v8_20261017T095151Z"
)

V6_SHA = (
    "c55d0fd47c0790ff21b46ed97638c975c79b41a2c24a0cb43aea29f125982dee"
)
V8_SHA = (
    "ecce8c6b92a85520d19da0424018c94e0bdeece1b744a5652a5677bfaf196ccb"
)
V9_SHA = (
    "d473d03a7d840b5b15051f90a99cde996295a96cd8d2dbf5d0a280e669995312"
)

IDL_BLOB = "6715f5cf19dffa8651a185ac0dd823218c6a2a75"

POOL = "BVRbyLjjfSBcoyiYFuxbgKYnWuiFaF9CSXEa5vdSZ9Hh"


def load(path):
    return json.loads(path.read_text())


def ensure(ok, reason):
    if not ok:
        raise ValidationError(reason)


def first_diff(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def audit_case(case, idl, store, helpers):

    slot = case["slot"]
    name = case["name"]

    roles = case["instruction_roles"]
    args = case["instruction_args"]

    proof = load(V6 / "proofs" / f"{slot}.json")
    saved = load(V6 / "blocks" / f"{slot}.json")

    ensure(
        proof["slot"] == saved["slot"] == slot
        and proof["pool"] == saved["pool"] == POOL
        and proof["name"] == name,
        "source block/proof identity mismatch",
    )

    ensure(
        saved["raw_sha256"] == proof["block_raw_rpc_sha256"],
        "raw RPC block SHA drift",
    )

    ensure(
        store.get(saved["raw_sha256"]).get("result")
        == saved["block"],
        "original RPC receipt differs from block",
    )

    ensure(
        proof["scope"]
        == "ISOLATED_SINGLE_TARGET_MUTATION_FULL_SLOT_BOUNDARY"
        and proof["external_writer_touches"] == []
        and proof["args_status"] == "PASS",
        "not a proven isolated successful mutation",
    )

    ensure(
        args == proof["args"]
        and case["signature"] == proof["signature"],
        "historical instruction arguments or signature drift",
    )

    tx_index = proof["instruction_order"]["tx_index"]
    txinfo = saved["block"]["transactions"][tx_index]

    tx = txinfo["transaction"]
    meta = txinfo["meta"]

    ensure(
        meta["err"] is None
        and tx["signatures"][0] == case["signature"],
        "transaction signature/status mismatch",
    )

    keys = account_keys(tx, meta)
    outer = proof["instruction_order"]["outer"]
    inner_idx = proof["instruction_order"]["inner"]

    top = tx["message"]["instructions"][outer]

    inner = next(
        (
            g["instructions"]
            for g in meta.get("innerInstructions") or []
            if g["index"] == outer
        ),
        [],
    )

    nodes = _invocations(top, inner, keys, outer)

    target = next(
        (
            n for n in nodes
            if n["inner_index"] == inner_idx
        ),
        None,
    )

    ensure(
        target is not None
        and target.get("program") == PROGRAM_ID
        and target.get("resolved") is not None,
        "DLMM instruction missing",
    )

    raw = target["resolved"]["raw"]

    ensure(
        hashlib.sha256(raw).hexdigest()
        == proof["raw_instruction_sha256"],
        "historical instruction byte hash mismatch",
    )

    decoded = idl.decode_named("instructions", raw)

    ensure(
        decoded["name"] == name
        and decoded["data"] == args,
        "raw instruction argument mismatch",
    )

    spec = next(
        x for x in idl.idl["instructions"]
        if x["name"] == name
    )

    actual_accounts = target["resolved"]["accounts"]

    ensure(
        len(actual_accounts) >= len(spec["accounts"]),
        "instruction account vector incomplete",
    )

    actual_roles = {
        x["name"]: actual_accounts[i]
        for i, x in enumerate(spec["accounts"])
    }

    ensure(
        roles == actual_roles
        and actual_roles["lb_pair"] == POOL,
        "actual instruction roles mismatch",
    )

    event_name = (
        "PlaceLimitOrderEvt"
        if name == "place_limit_order"
        else "CancelLimitOrderEvt"
    )

    events = []

    for node in nodes:
        resolved = node.get("resolved")

        if (
            resolved is not None
            and node.get("program") == PROGRAM_ID
            and helpers["belongs_to"](node, target)
            and resolved["raw"].startswith(EVENT_CPI_TAG)
        ):
            evt = idl.decode_named(
                "events", resolved["raw"][8:]
            )
            if evt["name"] == event_name:
                events.append(evt["data"])

    ensure(
        len(events) == 1
        and events[0] == case["event"],
        "target CPI event not uniquely verified",
    )

    transfers, unparsed = helpers["token_cpis"](
        nodes, target, top, inner, keys, roles
    )

    ensure(
        not unparsed
        and transfers == case["spl_token_transfers"],
        "SPL token CPI witness incomplete or changed",
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

    ledger = ObservedBoundaryLedger(
        POOL, slot - 1, before
    )

    ledger.apply(proof, before, after)

    ensure(
        ledger.slot == slot,
        "boundary continuity failure",
    )

    pre = {
        a: account_raw(c, a, slot - 1)
        for a, c in before.items()
    }
    post = {
        a: account_raw(c, a, slot)
        for a, c in after.items()
    }

    # Predictions are made without reading post or event values.
    prediction = predict(
        name, POOL, roles, args, pre
    )

    # Independently compare actual event + SPL token CPI.
    economics = verify_economics(
        prediction,
        args,
        events[0],
        transfers,
        roles,
        POOL,
    )

    comparisons = []

    for address, expected in (
        prediction.predicted_bin_arrays.items()
    ):

        actual = post.get(address)

        ensure(
            actual is not None,
            "historical post BinArray missing",
        )

        comparisons.append({
            "address": address,
            "match": expected == actual,
            "first_difference": first_diff(
                expected, actual
            ),
            "predicted_sha256": hashlib.sha256(
                expected
            ).hexdigest(),
            "actual_sha256": hashlib.sha256(
                actual
            ).hexdigest(),
        })

    ensure(
        all(x["match"] for x in comparisons),
        "restricted model/raw BinArray parity mismatch: "
        + str(comparisons),
    )

    if name == "place_limit_order":

        order_after = lo_prestate(
            post[roles["limit_order"]],
            POOL,
        )

        ensure(
            order_after is not None
            and order_after["owner"] == roles["owner"],
            "new LimitOrder account owner mismatch",
        )

        requested = args["params"]["bins"]

        ensure(
            order_after["bin_count"] == len(requested)
            and all(
                order_after["bins"].get(
                    b["id"], {}
                ).get("amount") == b["amount"]
                and order_after["bins"][b["id"]]["is_ask"]
                == args["params"]["is_ask_side"]
                for b in requested
            ),
            "created LimitOrder record mismatch",
        )

    return {
        "slot": slot,
        "name": name,
        "status": "PASS_RESTRICTED_RAW_BINARRAY",
        "predicted_open_order_deltas":
            prediction.bin_deltas,
        "raw_binarray_comparisons": comparisons,
        "event_cpi_gate": economics["event"],
        "spl_transfer_gate": economics["spl_cpi"],
        "refund_x": prediction.refund_x,
        "refund_y": prediction.refund_y,
        "limit_order_account_parity": "NOT_MODELED",
        "full_mutation_account_parity": "NOT_PROVEN",
        "scope": prediction.status,
    }


def main():

    ensure(
        sha_file(V6 / "summary.json") == V6_SHA
        and sha_file(V8 / "summary.json") == V8_SHA,
        "historical witness summary SHA drift",
    )

    v9 = list(
        (ROOT / "runs").glob(
            "d1_lo_math_v9_*/summary.json"
        )
    )

    proven = [
        p for p in v9
        if sha_file(p) == V9_SHA
    ]

    ensure(
        bool(proven),
        "previous 4/4 V9 evidence summary missing/altered",
    )

    ensure(
        load(proven[0]).get("status_counts")
        == {"PASS_RESTRICTED_RAW_BINARRAY": 4},
        "prior V9 parity is not 4/4",
    )

    idl_raw = (
        ROOT / "external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json"
    ).read_bytes()

    blob = hashlib.sha1(
        b"blob "
        + str(len(idl_raw)).encode()
        + b"\0"
        + idl_raw
    ).hexdigest()

    ensure(
        blob == IDL_BLOB,
        "pinned official IDL drift",
    )

    idl = IdlCodec(json.loads(idl_raw))

    helpers = runpy.run_path(
        str(ROOT / "scripts/d1_lo_mutation_evidence_v8.py")
    )

    cases = load(V8 / "cases.json")

    ensure(
        len(cases) == 4
        and Counter(x["name"] for x in cases)
        == {
            "place_limit_order": 2,
            "cancel_limit_order": 2,
        },
        "historical LO case inventory mismatch",
    )

    store = RawStore(V6 / "raw_rpc")
    reports = []

    for case in cases:

        try:
            report = audit_case(
                case, idl, store, helpers
            )

        except (Unavailable, Unsupported) as exc:
            report = {
                "slot": case["slot"],
                "name": case["name"],
                "status": "UNAVAILABLE",
                "reason": f"{type(exc).__name__}: {exc}",
            }

        except (
            ValidationError, ValueError, KeyError,
            TypeError, IndexError
        ) as exc:
            report = {
                "slot": case["slot"],
                "name": case["name"],
                "status": "FAIL",
                "reason": f"{type(exc).__name__}: {exc}",
            }

        reports.append(report)

        print(
            "V10_CASE",
            report["slot"],
            report["name"],
            report["status"],
            report.get("reason", ""),
            flush=True,
        )

    counts = dict(
        Counter(r["status"] for r in reports)
    )

    summary = {
        "schema": "D1_RESTRICTED_LO_MUTATION_MODEL_V10",
        "basis_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT, text=True,
        ).strip(),
        "historical_cases": len(reports),
        "status_counts": counts,
        "scope": "PLACE_AND_NOT_FILLED_CANCEL_RAW_BINARRAY_ONLY",
        "full_limit_order_account_parity": "NOT_MODELED",
        "mathematical_partial_fill_cancel": "UNAVAILABLE",
        "full_D1_instruction_prestate": "UNAVAILABLE",
        "D1_HISTORICAL_GATE": "UNAVAILABLE",
    }

    output = ROOT / "runs" / (
        "d1_lo_math_integration_v10_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
    )

    output.mkdir(
        parents=True, exist_ok=False
    )

    (output / "cases.json").write_text(
        json.dumps(
            reports, sort_keys=True, indent=2
        ) + "\n"
    )

    (output / "summary.json").write_text(
        json.dumps(
            summary, sort_keys=True, indent=2
        ) + "\n"
    )

    print("\n===== V10 FINAL =====")

    for k, v in summary.items():
        print(k.upper(), "=", v)

    print("SUMMARY =", output / "summary.json")
    print(
        "SUMMARY_SHA256 =",
        sha_file(output / "summary.json"),
    )

    if any(s == "FAIL" for s in counts):
        raise SystemExit(2)

    if any(s == "UNAVAILABLE" for s in counts):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
