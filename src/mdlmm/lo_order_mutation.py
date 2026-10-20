"""Pure restricted Meteora DLMM limit-order bin mutation model.

Only placement and provably NOT_FILLED cancellation are supported.

Predictions use prestate + actual instruction arguments, never
post-state observations, events, or transfer amounts.

This is not complete LimitOrder account mutation parity.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from .codec import (
    b58encode,
    b58decode,
    decode_bin_array,
    discriminator,
)
from .errors import Unsupported, Unavailable, ValidationError

U64 = (1 << 64) - 1
LO_DISC = discriminator("account", "LimitOrder")
BA_DISC = discriminator("account", "BinArray")

BASE_OFFSET = 56
BIN_BYTES = 144
OPEN_OFFSET = 112


def require(ok, message):
    if not ok:
        raise ValidationError(message)


def uint(value, name, *, positive=False):
    valid = (
        0 < value <= U64
        if positive else 0 <= value <= U64
    )
    require(
        type(value) is int and valid,
        f"{name} must be a valid u64",
    )
    return value


def pubkey(value, name):
    require(
        isinstance(value, str)
        and len(b58decode(value)) == 32,
        f"{name} must be a 32-byte base58 address",
    )
    return value


def lo_prestate(raw, pool):
    """Decode the pinned LimitOrder header and 32-byte bin records."""

    if raw is None:
        return None

    require(
        isinstance(raw, bytes)
        and len(raw) >= 120
        and raw[:8] == LO_DISC,
        "LimitOrder account layout/discriminator invalid",
    )

    n = int.from_bytes(raw[72:74], "little")

    require(
        n <= 512 and len(raw) >= 120 + 32 * n,
        "LimitOrder bin inventory truncated",
    )

    require(
        b58encode(raw[8:40]) == pool,
        "LimitOrder pool mismatch",
    )

    records = {}

    for i in range(n):
        o = 120 + 32 * i

        bin_id = int.from_bytes(
            raw[o + 16:o + 20],
            "little",
            signed=True,
        )

        require(
            bin_id not in records,
            "LimitOrder duplicate bin",
        )

        side = raw[o + 20]
        require(side in (0, 1), "LimitOrder side invalid")

        records[bin_id] = {
            "amount": int.from_bytes(
                raw[o:o + 8], "little"
            ),
            "age": int.from_bytes(
                raw[o + 8:o + 12], "little"
            ),
            "is_ask": bool(side),
        }

    return {
        "owner": b58encode(raw[40:72]),
        "bins": records,
        "bin_count": n,
    }


@dataclass(frozen=True)
class RestrictedResult:
    kind: str
    bin_deltas: dict[int, int]
    predicted_bin_arrays: dict[str, bytes]
    refund_x: int
    refund_y: int
    pre_bin_array_sha256: dict[str, str]

    status: str = (
        "MODEL_RESTRICTED_OPEN_ORDER_RAW_BINARRAY_ONLY"
    )


def predict(
    kind: str,
    pool: str,
    roles: dict,
    args: dict,
    pre: Mapping[str, bytes | None],
) -> RestrictedResult:
    """Predict without reading poststate/event/transfer observations."""

    if kind not in (
        "place_limit_order",
        "cancel_limit_order",
    ):
        raise Unsupported(
            f"LO mutation not supported: {kind}"
        )

    pubkey(pool, "pool")

    require(
        isinstance(roles, dict)
        and roles.get("lb_pair") == pool,
        "instruction pool mismatch",
    )

    require(
        isinstance(args, dict)
        and isinstance(pre, Mapping),
        "instruction arguments/prestate missing",
    )

    account = pubkey(
        roles.get("limit_order"), "limit_order"
    )

    require(
        account in pre,
        "LimitOrder account prestate missing",
    )

    info = args.get("remaining_accounts_info")
    require(
        isinstance(info, dict)
        and set(info) == {"slices"}
        and isinstance(info["slices"], list),
        "remaining accounts info missing or malformed",
    )

    allowed_hook_types = {
        "TransferHookX",
        "TransferHookY",
        "TransferHookReward",
        "TransferHookMultiReward",
        "TransferHookReferral",
    }

    for item in info["slices"]:
        require(
            isinstance(item, dict)
            and set(item) == {"accounts_type", "length"}
            and isinstance(item["accounts_type"], dict)
            and item["accounts_type"].get("variant") in allowed_hook_types
            and type(item["length"]) is int
            and item["length"] == 0,
            "active or unrecognized transfer-hook slice unsupported",
        )

    arrays = {}

    for address, raw in pre.items():

        pubkey(address, "captured account")

        if raw is None:
            continue

        require(
            isinstance(raw, bytes),
            "pre-account bytes invalid",
        )

        if raw[:8] != BA_DISC:
            continue

        decoded = decode_bin_array(raw)

        require(
            decoded["pool"] == pool,
            "foreign BinArray in prestate",
        )

        index = decoded["index"]

        require(
            index not in arrays,
            "duplicate BinArray index",
        )

        arrays[index] = (
            address, raw, decoded
        )

    require(
        bool(arrays),
        "no BinArray prestate supplied",
    )

    deltas = {}
    refund_x = 0
    refund_y = 0

    if kind == "place_limit_order":

        require(
            pre[account] is None,
            "place requires newly created LimitOrder",
        )

        params = args.get("params")

        require(
            isinstance(params, dict)
            and params.get("relative_bin") is None,
            "relative-bin or missing place params",
        )

        require(
            type(params.get("is_ask_side")) is bool,
            "place side unknown",
        )

        rows = params.get("bins")

        require(
            isinstance(rows, list) and bool(rows),
            "empty place bins",
        )

        for row in rows:

            require(
                isinstance(row, dict)
                and type(row.get("id")) is int,
                "invalid place bin ID",
            )

            k = row["id"]

            uint(
                row.get("amount"),
                "place amount",
                positive=True,
            )

            require(
                k not in deltas,
                "duplicate place bin",
            )

            require(
                k // 70 in arrays,
                "place bin not captured",
            )

            deltas[k] = row["amount"]

        uint(
            sum(deltas.values()),
            "sum of place amounts",
            positive=True,
        )

    else:

        lo = lo_prestate(
            pre[account], pool
        )

        require(
            lo is not None,
            "cancel requires LimitOrder prestate",
        )

        require(
            lo["owner"] == pubkey(
                roles.get("owner"), "cancel owner"
            ),
            "cancel owner differs from LimitOrder owner",
        )

        bins = args.get("bins")

        require(
            isinstance(bins, list)
            and bool(bins)
            and all(type(x) is int for x in bins)
            and len(set(bins)) == len(bins),
            "invalid cancel bin IDs",
        )

        for k in bins:

            require(
                k in lo["bins"]
                and k // 70 in arrays,
                "cancel target bin absent from prestate",
            )

            index = k // 70
            local = k - index * 70

            data = arrays[index][2]["bins"][local]
            record = lo["bins"][k]

            # Official SDK LimitOrder::get_status:
            # equal ages mean NOT_FILLED.
            if record["age"] != data["order_age"]:
                raise Unavailable(
                    "cancel age differs: partial/fulfilled "
                    "order is outside V10"
                )

            amount = uint(
                record["amount"],
                "unfilled amount",
                positive=True,
            )

            require(
                data["open_order_amount"] >= amount,
                "cancel would underflow open_order_amount",
            )

            deltas[k] = -amount

            if record["is_ask"]:
                refund_x += amount
            else:
                refund_y += amount

        uint(refund_x, "refund x")
        uint(refund_y, "refund y")

        if refund_x and refund_y:
            raise Unavailable(
                "mixed ask/bid cancel requires "
                "multilateral CPI model"
            )

    predicted = {}
    sha = {}

    for index, (address, raw, decoded) in arrays.items():

        updated = bytearray(raw)

        for bin_id, delta in deltas.items():

            if bin_id // 70 != index:
                continue

            local = bin_id - index * 70

            assert 0 <= local < 70

            offset = (
                BASE_OFFSET
                + BIN_BYTES * local
                + OPEN_OFFSET
            )

            old = int.from_bytes(
                updated[offset:offset + 8],
                "little",
            )

            require(
                old
                == decoded["bins"][local]["open_order_amount"],
                "BinArray decoder/raw layout mismatch",
            )

            new = old + delta

            require(
                0 <= new <= U64,
                "open_order_amount overflow/underflow",
            )

            updated[offset:offset + 8] = (
                new.to_bytes(8, "little")
            )

        predicted[address] = bytes(updated)

        sha[address] = hashlib.sha256(
            raw
        ).hexdigest()

    return RestrictedResult(
        kind,
        deltas,
        predicted,
        refund_x,
        refund_y,
        sha,
    )


def verify_economics(
    model: RestrictedResult,
    args: dict,
    event: dict,
    transfers: list[dict],
    roles: dict,
    pool: str,
) -> dict:
    """Validate external event/CPI evidence against the pure prediction."""

    require(
        event.get("lb_pair") == pool
        and event.get("limit_order")
        == roles["limit_order"],
        "event pool/order mismatch",
    )

    require(
        isinstance(transfers, list)
        and len(transfers) == 1,
        "restricted model requires exactly "
        "one SPL transfer CPI",
    )

    t = transfers[0]

    if model.kind == "place_limit_order":

        require(
            event.get("params") == args["params"],
            "place event args mismatch",
        )

        total = sum(model.bin_deltas.values())

        source = roles["user_token"]
        dest = roles["reserve"]

        require(
            t.get("amount") == total
            and t.get("source") == source
            and t.get("destination") == dest,
            "place transfer CPI mismatch",
        )

    else:

        require(
            event.get("bins") == args["bins"],
            "cancel event bins mismatch",
        )

        require(
            event.get("amounts")
            == [model.refund_x, model.refund_y],
            "cancel refund amount mismatch",
        )

        if model.refund_x:

            source = roles["reserve_x"]
            dest = roles["owner_token_x"]
            total = model.refund_x

        else:

            source = roles["reserve_y"]
            dest = roles["owner_token_y"]
            total = model.refund_y

        require(
            t.get("amount") == total
            and t.get("source") == source
            and t.get("destination") == dest,
            "cancel transfer CPI mismatch",
        )

    return {
        "event": "PASS",
        "spl_cpi": "PASS",
        "scope": "RESTRICTED_ONE_SIDE_ONE_TRANSFER_ONLY",
    }
