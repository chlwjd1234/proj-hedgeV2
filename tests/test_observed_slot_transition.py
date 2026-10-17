import base64
import hashlib
from copy import deepcopy

import pytest

from mdlmm.errors import ValidationError
from mdlmm.observed_slot_transition import (
    ObservedBoundaryLedger, account_raw
)

POOL = "So11111111111111111111111111111111111111112"
ADDRESS = "2L47gbUzGsnJZqRzfny4b3VWvKBnMRTMeNH3uzFrj6ko"


def capture(slot, value):
    return {
        "pubkey": ADDRESS,
        "requested_slot": slot,
        "context_slot": slot,
        "account": None if value is None else {
            "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "lamports": 2039280,
            "executable": False,
            "rent_epoch": 0,
            "space": len(value),
            "data_base64": base64.b64encode(value).decode(),
            "data_sha256": hashlib.sha256(value).hexdigest(),
        },
    }


def fixture(before=b"X", after=b"Y"):
    a, b = capture(9, before), capture(10, after)
    sha = lambda x: (
        hashlib.sha256(x).hexdigest()
        if x is not None else None
    )

    status = (
        "CREATED_IN_SLOT" if before is None
        else "CLOSED_IN_SLOT" if after is None
        else "CHANGED" if before != after
        else "UNCHANGED"
    )

    proof = {
        "slot": 10,
        "pool": POOL,
        "name": "claim_fee2",
        "args_status": "PASS",
        "scope": "ISOLATED_SINGLE_TARGET_MUTATION_FULL_SLOT_BOUNDARY",
        "external_writer_touches": [],
        "observed_account_set": [ADDRESS],
        "slot_boundary_accounts": [{
            "address": ADDRESS,
            "pre_sha256": sha(before),
            "post_sha256": sha(after),
            "status": status,
        }],
        "observed_slot_patch": {
            ADDRESS: {
                "before": sha(before),
                "after": sha(after),
                "status": status,
            }
        },
    }

    return proof, {ADDRESS: a}, {ADDRESS: b}


def test_atomic_transition():
    proof, pre, post = fixture()
    ledger = ObservedBoundaryLedger(POOL, 9, pre)

    result = ledger.apply(proof, pre, post)

    assert ledger.slot == 10
    assert result["mathematical_program_parity"] == "NOT_TESTED"
    assert result["D1_HISTORICAL_GATE"] == "UNAVAILABLE"

    with pytest.raises(ValidationError):
        ledger.apply(proof, pre, post)


@pytest.mark.parametrize(
    "before,after",
    [(None, b"Y"), (b"X", None), (b"X", b"X")]
)
def test_account_lifecycle(before, after):
    proof, pre, post = fixture(before, after)
    ledger = ObservedBoundaryLedger(POOL, 9, pre)

    result = ledger.apply(proof, pre, post)
    assert (
        result["account_changes"][0]["status"]
        == proof["slot_boundary_accounts"][0]["status"]
    )


def test_corrupt_account_cannot_advance_state():
    proof, pre, post = fixture()
    post = deepcopy(post)
    post[ADDRESS]["account"]["data_base64"] = (
        base64.b64encode(b"Z").decode()
    )

    ledger = ObservedBoundaryLedger(POOL, 9, pre)

    with pytest.raises(ValidationError, match="SHA256"):
        ledger.apply(proof, pre, post)

    assert ledger.slot == 9
    assert account_raw(
        ledger.accounts[ADDRESS], ADDRESS, 9
    ) == b"X"


@pytest.mark.parametrize(
    "defect",
    [
        "missing", "wrong_pre", "wrong_claim",
        "writer", "scope", "wrong_slot"
    ],
)
def test_untrusted_evidence_fails_atomically(defect):
    proof, pre, post = fixture()
    ledger = ObservedBoundaryLedger(POOL, 9, pre)

    if defect == "missing":
        post = {}
    elif defect == "wrong_pre":
        pre = {ADDRESS: capture(9, b"Q")}
    elif defect == "wrong_claim":
        proof["observed_slot_patch"][ADDRESS]["after"] = "0" * 64
    elif defect == "writer":
        proof["external_writer_touches"] = [{"tx_index": 11}]
    elif defect == "scope":
        proof["scope"] = "UNVERIFIED"
    elif defect == "wrong_slot":
        post[ADDRESS]["context_slot"] = 999

    with pytest.raises(ValidationError):
        ledger.apply(proof, pre, post)

    assert ledger.slot == 9
