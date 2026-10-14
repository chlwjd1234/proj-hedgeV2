"""Recipient accounting for a trading fee already split by liquidity source.

The host rule is transcribed from Meteora's official formulas, at docs commit
0f0e88986c3a12ae08565e47d3c1300df70d5d3e. It does not infer a host account from
an observed zero fee. Exact-output rounding in the output token is a separate
amount and must never be fed into this trading-fee allocation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import Unsupported, ValidationError

if TYPE_CHECKING:
    from .swap_math import FeeSplit

BASIS_POINTS = 10_000
HOST_FEE_BPS = 2_000
U64_MAX = (1 << 64) - 1


@dataclass(frozen=True)
class RecipientFees:
    """All quantities use the same trading-fee token, supplied by the caller."""

    trading_fee: int
    mm_lp_fee: int
    limit_order_fee: int
    protocol_fee: int
    host_fee: int
    mm_host_fee: int
    limit_order_host_fee: int
    protocol_before_host: int


def allocate_recipients(split: FeeSplit, *, protocol_share: int,
                        host_present: bool) -> RecipientFees:
    """Split protocol-side fees without changing either maker's entitlement."""
    if type(host_present) is not bool:
        raise ValidationError('host account presence must be an explicit boolean')
    if type(protocol_share) is not int or not 0 <= protocol_share <= BASIS_POINTS:
        raise ValidationError('protocol share must be integer basis points')
    values = (split.mm_lp, split.mm_protocol, split.lo_maker, split.lo_protocol,
              split.unallocated_trading_fee)
    if any(type(value) is not int or not 0 <= value <= U64_MAX for value in values):
        raise ValidationError('recipient fee input must contain u64 integer amounts')
    if split.unallocated_trading_fee:
        raise Unsupported('SDK zero-principal dust fee has no validated recipient allocation')
    total = sum(values)
    if total > U64_MAX:
        raise ValidationError('trading fee sum overflows u64')
    mm_host = lo_host = 0
    if host_present:
        mm_host = split.mm_protocol * HOST_FEE_BPS // BASIS_POINTS
        total_lo_fee = split.lo_maker + split.lo_protocol
        eligible_lo_fee = total_lo_fee * protocol_share // BASIS_POINTS
        lo_host = min(eligible_lo_fee * HOST_FEE_BPS // BASIS_POINTS,
                      split.lo_protocol)
    host = mm_host + lo_host
    protocol_before_host = split.mm_protocol + split.lo_protocol
    protocol = protocol_before_host - host
    if min(protocol, host) < 0 or total != split.mm_lp + split.lo_maker + protocol + host:
        raise ValidationError('trading-fee recipient conservation failed')
    return RecipientFees(total, split.mm_lp, split.lo_maker, protocol, host,
                         mm_host, lo_host, protocol_before_host)
