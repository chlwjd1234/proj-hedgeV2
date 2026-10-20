"""Pure LO mutation arithmetic and fail-closed tests."""

from copy import deepcopy
import pytest

from mdlmm.codec import (
    b58encode, b58decode,
    discriminator, decode_bin_array
)
from mdlmm.errors import ValidationError, Unavailable
from mdlmm.lo_order_mutation import predict, verify_economics

POOL = b58encode(bytes([2]) * 32)
BA = b58encode(bytes([3]) * 32)
LO = b58encode(bytes([4]) * 32)
OWNER = b58encode(bytes([5]) * 32)
USER = b58encode(bytes([6]) * 32)
RESERVE_X = b58encode(bytes([7]) * 32)
RESERVE_Y = b58encode(bytes([8]) * 32)


def array(open_amount=0, age=2, index=-17, bin_id=-1141):

    raw = bytearray(10136)

    raw[:8] = discriminator("account", "BinArray")
    raw[8:16] = index.to_bytes(
        8, "little", signed=True
    )
    raw[24:56] = b58decode(POOL)

    local = bin_id - index * 70
    assert 0 <= local < 70

    off = 56 + local * 144

    raw[off + 112:off + 120] = (
        open_amount.to_bytes(8, "little")
    )
    raw[off + 136:off + 140] = (
        age.to_bytes(4, "little")
    )

    return bytes(raw)


def limit_order(amount, age=2, is_ask=False, bin_id=-1141):

    raw = bytearray(152)

    raw[:8] = discriminator("account", "LimitOrder")
    raw[8:40] = b58decode(POOL)
    raw[40:72] = b58decode(OWNER)
    raw[72:74] = (1).to_bytes(2, "little")

    raw[120:128] = amount.to_bytes(8, "little")
    raw[128:132] = age.to_bytes(4, "little")
    raw[136:140] = bin_id.to_bytes(
        4, "little", signed=True
    )
    raw[140] = int(is_ask)

    return bytes(raw)


def place_case(amount=50):

    roles = {
        "lb_pair": POOL,
        "limit_order": LO,
        "user_token": USER,
        "reserve": RESERVE_Y,
    }
    args = {
        "params": {
            "relative_bin": None,
            "is_ask_side": False,
            "bins": [{"id": -1141, "amount": amount}],
        },
        "remaining_accounts_info": {"slices": []},
    }
    return roles, args, {BA: array(), LO: None}


def cancel_case(amount=50):

    roles = {
        "lb_pair": POOL,
        "limit_order": LO,
        "owner": OWNER,
        "reserve_x": RESERVE_X,
        "reserve_y": RESERVE_Y,
        "owner_token_x": USER,
        "owner_token_y": USER,
    }
    args = {
        "bins": [-1141],
        "remaining_accounts_info": {"slices": []},
    }
    return roles, args, {
        BA: array(open_amount=amount),
        LO: limit_order(amount),
    }


def open_value(raw):
    decoded = decode_bin_array(raw)
    return decoded["bins"][-1141 - (-17) * 70][
        "open_order_amount"
    ]


def test_place_predicts_raw_binarray_without_mutating_input():

    roles, args, pre = place_case()

    snapshot = deepcopy(pre)
    result = predict("place_limit_order", POOL, roles, args, pre)

    assert pre == snapshot
    assert result.bin_deltas == {-1141: 50}
    assert open_value(result.predicted_bin_arrays[BA]) == 50

    event = {
        "lb_pair": POOL,
        "limit_order": LO,
        "params": args["params"],
    }
    transfers = [{
        "amount": 50,
        "source": USER,
        "destination": RESERVE_Y,
    }]

    assert verify_economics(
        result, args, event, transfers, roles, POOL
    )["spl_cpi"] == "PASS"


def test_not_filled_cancel_predicts_raw_binarray_and_refund():

    roles, args, pre = cancel_case()

    result = predict("cancel_limit_order", POOL, roles, args, pre)

    assert result.bin_deltas == {-1141: -50}
    assert open_value(result.predicted_bin_arrays[BA]) == 0
    assert result.refund_x == 0
    assert result.refund_y == 50

    event = {
        "lb_pair": POOL,
        "limit_order": LO,
        "bins": [-1141],
        "amounts": [0, 50],
    }
    transfers = [{
        "amount": 50,
        "source": RESERVE_Y,
        "destination": USER,
    }]

    assert verify_economics(
        result, args, event, transfers, roles, POOL
    )["event"] == "PASS"


@pytest.mark.parametrize(
    "issue",
    [
        "partial",
        "fulfilled",
        "underflow",
        "wrong_owner",
        "foreign_pool",
        "missing_array",
        "truncated_lo",
        "wrong_pool",
        "hook",
        "bad_amount",
    ],
)
def test_unsafe_cancellation_fails_closed(issue):

    roles, args, pre = cancel_case()

    if issue == "partial":
        pre[BA] = array(open_amount=50, age=3)
    elif issue == "fulfilled":
        pre[BA] = array(open_amount=50, age=4)
    elif issue == "underflow":
        pre[BA] = array(open_amount=49)
    elif issue == "wrong_owner":
        roles["owner"] = b58encode(bytes([12]) * 32)
    elif issue == "foreign_pool":
        roles["lb_pair"] = b58encode(bytes([13]) * 32)
    elif issue == "missing_array":
        pre.pop(BA)
    elif issue == "truncated_lo":
        pre[LO] = pre[LO][:-3]
    elif issue == "wrong_pool":
        damaged = bytearray(pre[BA])
        damaged[24:56] = bytes([15]) * 32
        pre[BA] = bytes(damaged)
    elif issue == "hook":
        args["remaining_accounts_info"] = {
            "slices": [{"length": 1}]
        }
    elif issue == "bad_amount":
        pre[LO] = limit_order(0)

    with pytest.raises((ValidationError, Unavailable)):
        predict("cancel_limit_order", POOL, roles, args, pre)


def test_place_overflow_is_rejected():

    roles, args, pre = place_case(2)
    pre[BA] = array(open_amount=2**64 - 1)

    with pytest.raises(ValidationError, match="overflow"):
        predict("place_limit_order", POOL, roles, args, pre)


def test_transfer_mismatch_is_rejected():

    roles, args, pre = place_case()
    result = predict("place_limit_order", POOL, roles, args, pre)

    event = {
        "lb_pair": POOL,
        "limit_order": LO,
        "params": args["params"],
    }
    transfers = [{
        "amount": 49,
        "source": USER,
        "destination": RESERVE_Y,
    }]

    with pytest.raises(ValidationError, match="transfer CPI"):
        verify_economics(
            result, args, event, transfers, roles, POOL
        )


@pytest.mark.parametrize(
    "factory,kind",
    [
        (place_case, "place_limit_order"),
        (cancel_case, "cancel_limit_order"),
    ],
)
def test_explicit_zero_length_hook_slice_is_allowed(factory, kind):
    roles, args, pre = factory()
    args["remaining_accounts_info"] = {
        "slices": [{
            "accounts_type": {
                "variant": "TransferHookX",
                "fields": [],
            },
            "length": 0,
        }]
    }

    result = predict(kind, POOL, roles, args, pre)
    assert result.predicted_bin_arrays


@pytest.mark.parametrize("length", [1, 2, 255])
def test_nonzero_hook_slice_remains_unsupported(length):
    roles, args, pre = place_case()
    args["remaining_accounts_info"] = {
        "slices": [{
            "accounts_type": {
                "variant": "TransferHookX",
                "fields": [],
            },
            "length": length,
        }]
    }

    with pytest.raises(ValidationError, match="transfer-hook"):
        predict("place_limit_order", POOL, roles, args, pre)
