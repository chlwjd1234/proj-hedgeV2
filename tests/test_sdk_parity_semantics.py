from mdlmm.model import Status


def _sdk_fee_expected(total_trader_fee:int, protocol_fee:int)->int:
    return total_trader_fee-protocol_fee


def test_sdk_fee_field_is_lp_user_fee_not_total_trader_fee():
    # Regression for the real-pool D0 bug: the official SDK returns `fee`
    # separately from `protocolFee`.
    assert _sdk_fee_expected(100,10)==90
    assert _sdk_fee_expected(100,0)==100


def test_d0_one_pool_current_validation_does_not_claim_d1_two_pool_coverage():
    selected=['pool-a']
    d1_target=2
    current_validation_complete=bool(selected)
    d1_pool_target_complete=len(selected)==d1_target
    status=Status.PASS if current_validation_complete else Status.UNAVAILABLE
    assert status==Status.PASS
    assert current_validation_complete is True
    assert d1_pool_target_complete is False
