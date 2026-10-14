from dataclasses import replace

import pytest

from mdlmm.errors import Unsupported, ValidationError
from mdlmm.swap_fees import allocate_recipients
from mdlmm.swap_math import FeeSplit


def test_mixed_host_fee_is_charged_from_eligible_source_slices():
    # Trading fee 100,000: MM 60,000, LO 40,000, protocol share 10%.
    split = FeeSplit(mm_lp=54_000, mm_protocol=6_000, lo_maker=20_000,
                     lo_protocol=20_000, unallocated_trading_fee=0)
    out = allocate_recipients(split, protocol_share=1_000, host_present=True)
    assert out.trading_fee == 100_000
    assert (out.mm_lp_fee, out.limit_order_fee) == (54_000, 20_000)
    assert (out.mm_host_fee, out.limit_order_host_fee, out.host_fee) == (1_200, 800, 2_000)
    assert (out.protocol_before_host, out.protocol_fee) == (26_000, 24_000)
    assert out.host_fee != out.protocol_before_host * 2_000 // 10_000


def test_absent_host_preserves_the_entire_protocol_fee():
    split = FeeSplit(54_000, 6_000, 20_000, 20_000, 0)
    out = allocate_recipients(split, protocol_share=1_000, host_present=False)
    assert out.host_fee == 0
    assert out.protocol_fee == 26_000
    assert out.mm_lp_fee + out.limit_order_fee + out.protocol_fee == out.trading_fee


def test_nested_rounding_and_odd_limit_order_slice():
    # ceil(101 / 3) = 34 MM fee, leaving 67 LO fee.
    split = FeeSplit(31, 3, 33, 34, 0)
    out = allocate_recipients(split, protocol_share=1_000, host_present=True)
    assert (out.mm_host_fee, out.limit_order_host_fee) == (0, 1)
    assert (out.trading_fee, out.protocol_fee, out.host_fee) == (101, 36, 1)


def test_host_cap_cannot_overdraw_lo_protocol_allocation():
    # Deliberate recipient-boundary input tests the documented explicit min.
    split = FeeSplit(0, 0, 99, 1, 0)
    out = allocate_recipients(split, protocol_share=10_000, host_present=True)
    assert out.limit_order_host_fee == 1
    assert out.protocol_fee == 0


def test_host_with_zero_eligible_fee_is_still_explicitly_present():
    out = allocate_recipients(FeeSplit(1, 0, 0, 0, 0),
                              protocol_share=1_000, host_present=True)
    assert out.host_fee == 0
    assert out.mm_lp_fee == 1


@pytest.mark.parametrize('host', [None, 0, 1, 'false'])
def test_ambiguous_host_presence_rejected(host):
    with pytest.raises(ValidationError, match='explicit boolean'):
        allocate_recipients(FeeSplit(0, 0, 0, 0, 0), protocol_share=1_000,
                            host_present=host)


def test_unallocated_sdk_dust_cannot_become_fabricated_recipient_income():
    with pytest.raises(Unsupported, match='dust'):
        allocate_recipients(FeeSplit(0, 0, 0, 0, 1), protocol_share=1_000,
                            host_present=False)


@pytest.mark.parametrize('share', [-1, 10_001, True, 0.5])
def test_invalid_protocol_share_rejected(share):
    with pytest.raises(ValidationError, match='basis points'):
        allocate_recipients(FeeSplit(0, 0, 0, 0, 0), protocol_share=share,
                            host_present=False)
