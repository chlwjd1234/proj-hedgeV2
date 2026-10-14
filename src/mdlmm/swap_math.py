"""Pure per-bin quote arithmetic from the pinned official DLMM helpers.

Reference: MeteoraAg/dlmm-sdk at
576919e3e4368e542c402f000b4264724f7f23ec, helpers/bin.ts:12-363,
helpers/fee.ts:79-152 and commons/src/quote.rs:37-383. These functions
describe quote amounts, not account mutations. In particular, an exact-out
rounding delta is output-denominated evidence, not a fee-token balance write.

The TS helper's ``fee`` is MM LP plus LO maker fees. The Rust helper instead
returns total trading fees. Both add output delta > 1 to their scalar protocol
fee without changing their fee scalar; those meanings remain separate here.
Host/referral allocation is outside this module's pinned-helper scope.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from .errors import Unavailable, ValidationError
from .model import Bin

Q64 = 1 << 64
U64_MAX = (1 << 64) - 1
FEE_PRECISION = 1_000_000_000
MAX_FEE_RATE = 100_000_000
BASIS_POINT_MAX = 10_000
LIMIT_ORDER_FEE_SHARE = 5_000

LayerSource = Literal['MM', 'PROCESSED_LO', 'OPEN_LO']


@dataclass(frozen=True, slots=True)
class LayerFill:
    """Amounts used by the quote, before trading fees, in execution order."""

    source: LayerSource
    net_amount_in: int
    gross_amount_out: int
    available_amount_out: int


@dataclass(frozen=True, slots=True)
class FeeSplit:
    """Base trading-fee allocation, all in the configured trading-fee token.

    The official split helper returns zero user/protocol fees if net input is
    zero. ``unallocated_trading_fee`` preserves the resulting dust discrepancy
    without assigning it to an account or silently dropping it.
    """

    mm_lp: int
    mm_protocol: int
    lo_maker: int
    lo_protocol: int
    unallocated_trading_fee: int = 0

    @property
    def mm_fee(self) -> int:
        return self.mm_lp + self.mm_protocol

    @property
    def lo_fee(self) -> int:
        return self.lo_maker + self.lo_protocol

    @property
    def user_fee(self) -> int:
        return self.mm_lp + self.lo_maker

    @property
    def protocol_fee(self) -> int:
        return self.mm_protocol + self.lo_protocol

    @property
    def total_trading_fee(self) -> int:
        return self.user_fee + self.protocol_fee + self.unallocated_trading_fee


@dataclass(frozen=True, slots=True)
class BinQuote:
    """Immutable amounts only; it deliberately contains no post-state.

    ``amount_in`` is consumed gross input; ``amount_out`` is returned user net
    output. Layers and ``gross_amount_out`` describe the exact-in re-quote that
    exact-out uses. The excess over requested output is separately recorded.
    ``sdk_fee`` follows TypeScript, ``rust_sdk_fee`` follows Rust. A scalar
    ``sdk_protocol_fee`` may mix the trading-fee token with output-token delta.
    """

    bin_id: int
    swap_for_y: bool
    fee_on_input: bool
    amount_in: int
    net_amount_in: int
    amount_out: int
    gross_amount_out: int
    trading_fee: int
    fee_split: FeeSplit
    layers: tuple[LayerFill, ...]
    requested_amount_out: int | None
    requoted_amount_out: int
    rounding_output_delta: int
    sdk_fee: int
    sdk_protocol_fee: int
    rust_sdk_fee: int
    used_full_bin_fallback: bool = False

    @property
    def sdk_protocol_rounding_delta(self) -> int:
        return self.rounding_output_delta if self.rounding_output_delta > 1 else 0


def _u64(name: str, value: int) -> int:
    if type(value) is not int or not 0 <= value <= U64_MAX:
        raise ValidationError(f'{name} must be an integer u64')
    return value


def _ceil_div(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValidationError('invalid nonnegative integer division')
    return (numerator + denominator - 1) // denominator


def _validate_fee_inputs(trade_fee_numerator: int, protocol_share: int) -> None:
    if (type(trade_fee_numerator) is not int
            or not 0 <= trade_fee_numerator <= MAX_FEE_RATE):
        raise ValidationError('trade_fee_numerator exceeds the official fee-rate bounds')
    if type(protocol_share) is not int or not 0 <= protocol_share <= BASIS_POINT_MAX:
        raise ValidationError('protocol_share must be integer basis points in [0, 10000]')


def _capacities(bin: Bin, swap_for_y: bool,
                support_limit_order: bool) -> tuple[int, int, int]:
    bin.validate()
    for name, value in (('swap_for_y', swap_for_y),
                        ('support_limit_order', support_limit_order)):
        if type(value) is not bool:
            raise ValidationError(f'{name} must be boolean')
    mm = bin.amount_y if swap_for_y else bin.amount_x
    processed = opened = 0
    if support_limit_order:
        required = ('open_order_amount', 'processed_order_remaining_amount',
                    'limit_order_ask_side')
        missing = [name for name in required if getattr(bin, name) is None]
        if missing:
            raise Unavailable('limit-order quote requires observed ' + ', '.join(missing))
        if bin.limit_order_ask_side not in (0, 1):
            raise ValidationError('unsupported limit_order_ask_side encoding')
        if swap_for_y != bool(bin.limit_order_ask_side):
            processed = bin.processed_order_remaining_amount
            opened = bin.open_order_amount
    # TS uses unbounded BN here, while Rust saturates the capacity to u64.
    # This module supports their common integer domain and rejects divergence.
    _u64('aggregate output capacity', mm + processed + opened)
    return mm, processed, opened


def max_amount_out(bin: Bin, *, swap_for_y: bool, support_limit_order: bool) -> int:
    """Available gross output with the same LO evidence and u64-domain checks.

    Capacity does not require a nonzero stored price. A caller may use it to
    skip empty bins before deriving a quote price and updating volatility.
    """
    return sum(_capacities(bin, swap_for_y, support_limit_order))


def _quote_capacities(bin: Bin, swap_for_y: bool, support_limit_order: bool,
                      fee_on_input: bool, trade_fee_numerator: int,
                      protocol_share: int) -> tuple[int, int, int]:
    capacities = _capacities(bin, swap_for_y, support_limit_order)
    if type(fee_on_input) is not bool:
        raise ValidationError('fee_on_input must be boolean')
    _validate_fee_inputs(trade_fee_numerator, protocol_share)
    if bin.price <= 0:
        raise ValidationError('quote requires an observed or separately derived positive Q64 price')
    return capacities


def _input_for_output(amount_out: int, price: int, swap_for_y: bool) -> int:
    numerator, denominator = ((amount_out * Q64, price) if swap_for_y
                              else (amount_out * price, Q64))
    return _u64('required net input', _ceil_div(numerator, denominator))


def _output_for_input(amount_in: int, price: int, swap_for_y: bool) -> int:
    numerator, denominator = ((amount_in * price, Q64) if swap_for_y
                              else (amount_in * Q64, price))
    return _u64('gross output', numerator // denominator)


def _exclude_fee(included: int, rate: int) -> tuple[int, int]:
    fee = _u64('trading fee', _ceil_div(included * rate, FEE_PRECISION))
    return included - fee, fee


def _include_fee(excluded: int, rate: int) -> tuple[int, int]:
    included = _u64('fee-inclusive amount',
                    _ceil_div(excluded * FEE_PRECISION, FEE_PRECISION - rate))
    return included, included - excluded


def split_fee(trading_fee: int, protocol_share: int, mm_amount_in: int,
              total_amount_in: int) -> FeeSplit:
    """Official source-based split; weight by net input even for output fees."""
    _u64('trading_fee', trading_fee)
    _u64('mm_amount_in', mm_amount_in)
    _u64('total_amount_in', total_amount_in)
    _validate_fee_inputs(0, protocol_share)
    if mm_amount_in > total_amount_in:
        raise ValidationError('MM net input exceeds total net input')
    if total_amount_in == 0:
        return FeeSplit(0, 0, 0, 0, trading_fee)
    mm_fee = _ceil_div(trading_fee * mm_amount_in, total_amount_in)
    lo_fee = trading_fee - mm_fee
    lo_maker = lo_fee * LIMIT_ORDER_FEE_SHARE // BASIS_POINT_MAX
    mm_protocol = mm_fee * protocol_share // BASIS_POINT_MAX
    return FeeSplit(mm_fee - mm_protocol, mm_protocol, lo_maker, lo_fee - lo_maker)


def _fill_layers(amount: int, price: int, swap_for_y: bool,
                 capacities: tuple[int, int, int]) -> tuple[LayerFill, ...]:
    remaining = amount
    result = []
    for index, (source, capacity) in enumerate(zip(
            ('MM', 'PROCESSED_LO', 'OPEN_LO'), capacities, strict=True)):
        used = output = 0
        # Official code always visits MM, but only visits later layers if
        # earlier layers leave input. Avoiding those visits also preserves the
        # Rust helper's checked-arithmetic behavior at extreme prices.
        if capacity and (index == 0 or remaining):
            max_input = _input_for_output(capacity, price, swap_for_y)
            if remaining >= max_input:
                used, output = max_input, capacity
            else:
                used = remaining
                output = _output_for_input(used, price, swap_for_y)
            remaining -= used
        result.append(LayerFill(source, used, output, capacity))
    return tuple(result)


def _quote_exact_in(bin: Bin, amount_in: int, *, swap_for_y: bool,
                    fee_on_input: bool, trade_fee_numerator: int,
                    protocol_share: int,
                    capacities: tuple[int, int, int]) -> BinQuote:
    available_net, trading_fee = (_exclude_fee(amount_in, trade_fee_numerator)
                                  if fee_on_input else (amount_in, 0))
    layers = _fill_layers(available_net, bin.price, swap_for_y, capacities)
    consumed_net = _u64('consumed net input', sum(layer.net_amount_in for layer in layers))
    gross_output = _u64('aggregate gross output', sum(layer.gross_amount_out for layer in layers))
    consumed_gross = amount_in
    if consumed_net < available_net:
        consumed_gross, trading_fee = (_include_fee(consumed_net, trade_fee_numerator)
                                       if fee_on_input else (consumed_net, 0))
    net_output = gross_output
    if not fee_on_input:
        net_output, trading_fee = _exclude_fee(gross_output, trade_fee_numerator)
    fees = split_fee(trading_fee, protocol_share, layers[0].net_amount_in, consumed_net)
    return BinQuote(
        bin_id=bin.bin_id, swap_for_y=swap_for_y, fee_on_input=fee_on_input,
        amount_in=consumed_gross, net_amount_in=consumed_net,
        amount_out=net_output, gross_amount_out=gross_output,
        trading_fee=trading_fee, fee_split=fees, layers=layers,
        requested_amount_out=None, requoted_amount_out=net_output,
        rounding_output_delta=0, sdk_fee=fees.user_fee,
        sdk_protocol_fee=fees.protocol_fee, rust_sdk_fee=trading_fee,
    )


def quote_exact_in_at_bin(bin: Bin, amount_in: int, *, swap_for_y: bool,
                          support_limit_order: bool, fee_on_input: bool,
                          trade_fee_numerator: int, protocol_share: int) -> BinQuote:
    """Quote MM, then processed orders, then open orders without mutation.

    A per-bin quote may consume only part of the offered input. Zero and dust
    requests are represented for helper parity; transaction-level meaningful
    request checks belong to the caller.
    """
    _u64('amount_in', amount_in)
    capacities = _quote_capacities(bin, swap_for_y, support_limit_order, fee_on_input,
                             trade_fee_numerator, protocol_share)
    return _quote_exact_in(bin, amount_in, swap_for_y=swap_for_y,
                           fee_on_input=fee_on_input, trade_fee_numerator=trade_fee_numerator,
                           protocol_share=protocol_share, capacities=capacities)


def quote_exact_out_at_bin(bin: Bin, amount_out: int, *, swap_for_y: bool,
                           support_limit_order: bool, fee_on_input: bool,
                           trade_fee_numerator: int, protocol_share: int) -> BinQuote:
    """Quote the official exact-out helper, including its full-bin fallback.

    When requested gross output is >= available capacity the SDK calls exact-in
    with U64_MAX and returns that result, which can be less than the request.
    Otherwise it rounds required input up separately per layer and re-quotes.
    Output delta is added to the SDK scalar protocol fee only when it is > 1.
    This rule does not establish a protocol balance mutation or denomination.
    """
    _u64('amount_out', amount_out)
    capacities = _quote_capacities(bin, swap_for_y, support_limit_order, fee_on_input,
                             trade_fee_numerator, protocol_share)
    target_gross = (amount_out if fee_on_input
                    else _include_fee(amount_out, trade_fee_numerator)[0])
    if target_gross >= sum(capacities):
        quote = _quote_exact_in(bin, U64_MAX, swap_for_y=swap_for_y,
                                fee_on_input=fee_on_input,
                                trade_fee_numerator=trade_fee_numerator,
                                protocol_share=protocol_share, capacities=capacities)
        return replace(quote, requested_amount_out=amount_out, used_full_bin_fallback=True)

    remaining_output = target_gross
    net_input = 0
    for capacity in capacities:
        layer_output = min(remaining_output, capacity)
        net_input = _u64('aggregate required net input', net_input
                         + _input_for_output(layer_output, bin.price, swap_for_y))
        remaining_output -= layer_output
        if remaining_output == 0:
            break
    if remaining_output:
        raise ValidationError('exact-out target exceeds supported layer capacity')
    gross_input = (_include_fee(net_input, trade_fee_numerator)[0]
                   if fee_on_input else net_input)
    quote = _quote_exact_in(bin, gross_input, swap_for_y=swap_for_y,
                            fee_on_input=fee_on_input,
                            trade_fee_numerator=trade_fee_numerator,
                            protocol_share=protocol_share, capacities=capacities)
    delta = quote.amount_out - amount_out
    if delta < 0:
        raise ValidationError('official exact-out re-quote does not cover requested output')
    sdk_protocol = _u64('SDK scalar protocol fee', quote.sdk_protocol_fee
                        + (delta if delta > 1 else 0))
    return replace(quote, amount_out=amount_out, requested_amount_out=amount_out,
                   rounding_output_delta=delta, sdk_protocol_fee=sdk_protocol)
