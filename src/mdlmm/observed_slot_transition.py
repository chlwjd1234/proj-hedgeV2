"""Evidence-bounded isolated DLMM slot-boundary transitions.

This is NOT a mathematical instruction execution model.
Only the explicitly captured archived account set can be advanced.
"""

from __future__ import annotations

import base64
import hashlib
from collections import Counter
from copy import deepcopy

from .codec import (
    b58encode, decode_bin_array, decode_lb_pair, discriminator
)
from .errors import ValidationError, Unavailable
from .model import PROGRAM_ID

TYPES = (
    "LbPair", "BinArray", "BinArrayBitmapExtension",
    "PositionV2", "LimitOrder",
)

SCOPE = "EXACT_ARCHIVED_ACCOUNTS_AT_ISOLATED_BLOCK_BOUNDARY_ONLY"


def require(ok, message):
    if not ok:
        raise ValidationError(message)


def account_raw(capture, address, slot):
    require(capture.get("pubkey") == address,
            "archive account address mismatch")
    require(
        capture.get("requested_slot") == slot
        and capture.get("context_slot") == slot,
        "archive historical slot mismatch",
    )

    account = capture.get("account")
    if account is None:
        return None

    require(isinstance(account, dict) and account.get("owner"),
            "invalid account metadata")
    require(
        type(account.get("lamports")) is int
        and account["lamports"] >= 0,
        "invalid account lamports",
    )

    try:
        raw = base64.b64decode(
            account["data_base64"], validate=True
        )
    except (KeyError, ValueError) as exc:
        raise ValidationError("invalid account base64") from exc

    require(len(raw) == account.get("space"),
            "account data/space mismatch")
    require(
        hashlib.sha256(raw).hexdigest() == account.get("data_sha256"),
        "account SHA256 mismatch",
    )
    return raw


def identity(capture, address, slot):
    raw = account_raw(capture, address, slot)
    account = capture.get("account")

    if account is None:
        return ("ABSENT",)

    return (
        "PRESENT",
        account["owner"],
        account["lamports"],
        account.get("rent_epoch"),
        account.get("executable"),
        len(raw),
        hashlib.sha256(raw).hexdigest(),
    )


def classify(pre, post, address, slot):
    before = account_raw(pre, address, slot - 1)
    after = account_raw(post, address, slot)

    if before is None and after is None:
        return "ABSENT_BOTH"
    if before is None:
        return "CREATED_IN_SLOT"
    if after is None:
        return "CLOSED_IN_SLOT"
    if before != after:
        return "CHANGED"

    if identity(pre, address, slot - 1)[1:] != \
       identity(post, address, slot)[1:]:
        return "METADATA_ONLY_CHANGE"

    return "UNCHANGED"


def account_type(raw):
    if raw is None:
        return "ABSENT"
    return next(
        (
            name for name in TYPES
            if raw[:8] == discriminator("account", name)
        ),
        "OPAQUE",
    )


def describe_change(pre, post, address, slot, pool):
    before = account_raw(pre, address, slot - 1)
    after = account_raw(post, address, slot)

    typ = account_type(
        after if after is not None else before
    )

    report = {
        "address": address,
        "status": classify(pre, post, address, slot),
        "type": typ,
        "before_sha256": (
            hashlib.sha256(before).hexdigest()
            if before is not None else None
        ),
        "after_sha256": (
            hashlib.sha256(after).hexdigest()
            if after is not None else None
        ),
    }

    if before is not None and after is not None:
        if account_type(before) != account_type(after):
            raise Unavailable(
                "account type changed within slot"
            )

    for raw in (before, after):
        if raw is None:
            continue

        if typ == "BinArray":
            require(
                decode_bin_array(raw)["pool"] == pool,
                "BinArray belongs to foreign pool",
            )

        elif typ == "BinArrayBitmapExtension":
            from .swap_quote import BitmapExtension
            require(
                BitmapExtension.from_bytes(raw).pool == pool,
                "bitmap extension belongs to foreign pool",
            )

        elif typ in ("PositionV2", "LimitOrder"):
            require(
                len(raw) >= 40
                and b58encode(raw[8:40]) == pool,
                "position/order belongs to foreign pool",
            )

        elif typ == "LbPair":
            require(address == pool, "LbPair key mismatch")
            decode_lb_pair(raw)

    if before is None or after is None:
        return report

    if before == after:
        report["changed_bytes"] = 0
        return report

    report["changed_bytes"] = (
        sum(a != b for a, b in zip(before, after))
        + abs(len(before) - len(after))
    )

    report["first_changed_offset"] = next(
        (
            i for i, (a, b)
            in enumerate(zip(before, after))
            if a != b
        ),
        min(len(before), len(after)),
    )

    if typ == "LbPair":
        a = decode_lb_pair(before)
        b = decode_lb_pair(after)

        for name in ("parameters", "variable"):
            report[name + "_changed_fields"] = [
                k for k in a[name]
                if a[name][k] != b[name][k]
            ]

        report["scalar_changed_fields"] = [
            k for k in a
            if k not in (
                "parameters", "variable", "account_sha256"
            ) and a[k] != b[k]
        ]

    elif typ == "BinArray":
        a = decode_bin_array(before)
        b = decode_bin_array(after)

        require(
            a["index"] == b["index"],
            "BinArray index changed",
        )

        changed_fields = Counter()
        changed_bins = []

        for old_bin, new_bin in zip(a["bins"], b["bins"]):
            fields = [
                k for k in old_bin
                if k != "bin_id"
                and old_bin[k] != new_bin[k]
            ]
            if fields:
                changed_bins.append(old_bin["bin_id"])
                changed_fields.update(fields)

        report["bin_array_index"] = a["index"]
        report["changed_bin_count"] = len(changed_bins)
        report["changed_bin_ids_sample"] = changed_bins[:12]
        report["changed_field_counts"] = dict(changed_fields)

    elif typ == "BinArrayBitmapExtension":
        from .swap_quote import BitmapExtension

        a = BitmapExtension.from_bytes(before)
        b = BitmapExtension.from_bytes(after)

        old_indices = set(a.initialized_indices())
        new_indices = set(b.initialized_indices())

        report["initialized_indices_added"] = sorted(
            new_indices - old_indices
        )[:32]
        report["initialized_indices_removed"] = sorted(
            old_indices - new_indices
        )[:32]

    else:
        report["decoded_field_parity"] = (
            "NOT_IMPLEMENTED_OPAQUE_RAW_DIFF_ONLY"
        )

    return report


class ObservedBoundaryLedger:
    """Apply only validated captured account boundary states."""

    def __init__(self, pool, slot, before):
        require(
            type(slot) is int and slot >= 0 and bool(before),
            "invalid boundary ledger initialization",
        )

        self.pool = pool
        self.slot = slot
        self.accounts = deepcopy(before)

        for address, capture in self.accounts.items():
            account_raw(capture, address, slot)

    def apply(self, proof, before, after):
        slot = proof.get("slot")

        require(
            type(slot) is int and slot == self.slot + 1,
            "non-consecutive or double-applied slot",
        )

        require(
            proof.get("pool") == self.pool
            and proof.get("scope")
            == "ISOLATED_SINGLE_TARGET_MUTATION_FULL_SLOT_BOUNDARY",
            "unverified boundary proof scope",
        )

        require(
            proof.get("external_writer_touches") == []
            and proof.get("args_status") == "PASS",
            "unresolved mutation or external writer",
        )

        targets = proof.get("observed_account_set")

        require(
            isinstance(targets, list)
            and bool(targets)
            and len(targets) == len(set(targets)),
            "missing or duplicated account inventory",
        )

        require(
            set(targets)
            == set(before)
            == set(after)
            == set(self.accounts),
            "account inventory mismatch",
        )

        rows = proof.get("slot_boundary_accounts")
        patch = proof.get("observed_slot_patch")

        require(
            isinstance(rows, list)
            and len(rows) == len(targets)
            and isinstance(patch, dict)
            and set(patch) == set(targets),
            "incomplete proof inventory",
        )

        row_map = {r["address"]: r for r in rows}

        require(
            len(row_map) == len(rows)
            and set(row_map) == set(targets),
            "duplicate or missing proof account",
        )

        projected = []

        for address in sorted(targets):
            require(
                identity(
                    self.accounts[address], address, self.slot
                ) == identity(
                    before[address], address, self.slot
                ),
                "prestate continuity mismatch",
            )

            item = describe_change(
                before[address],
                after[address],
                address,
                slot,
                self.pool,
            )

            expected = patch[address]
            recorded = row_map[address]

            for key, name in (
                ("before", "before_sha256"),
                ("after", "after_sha256"),
                ("status", "status"),
            ):
                require(
                    expected.get(key) == item[name],
                    "proof patch mismatch: " + key,
                )

            for key in (
                "pre_sha256", "post_sha256", "status"
            ):
                compare = {
                    "pre_sha256": item["before_sha256"],
                    "post_sha256": item["after_sha256"],
                    "status": item["status"],
                }
                require(
                    recorded.get(key) == compare[key],
                    "archive witness/proof mismatch: " + key,
                )

            projected.append(item)

        # Atomic state advancement: nothing changes before full validation.
        self.accounts = deepcopy(after)
        self.slot = slot

        return {
            "scope": SCOPE,
            "slot": slot,
            "pool": self.pool,
            "instruction": proof["name"],
            "account_changes": projected,
            "mathematical_program_parity": "NOT_TESTED",
            "account_write_completeness": "CAPTURED_INVENTORY_ONLY",
            "intra_block_instruction_prestate": "UNAVAILABLE",
            "D1_HISTORICAL_GATE": "UNAVAILABLE",
        }
