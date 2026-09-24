"""Integer MM-only exact-input engine, transcribed from pinned official quote semantics.

State-transition fidelity is earned by observed checkpoints, not by this module's
name. Limit-order mutations, transfer fees and exact-out need a different adapter.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, asdict, replace
from .errors import InsufficientLiquidity, Unsupported, ValidationError
from .model import Bin, FeeParameters, PoolState, VariableState, SOL_MINT
from .util import dec, D

Q64 = 1 << 64
U128 = (1 << 128) - 1
U64 = (1 << 64) - 1
FEE_PRECISION = 1_000_000_000
MAX_FEE_RATE = 100_000_000
BPS = 10000


def ceil_div(n: int, d: int) -> int:
    if n < 0 or d <= 0:
        raise ValidationError('ceil_div requires n>=0 and d>0')
    return (n + d - 1) // d


def checked_u64(n: int) -> int:
    if not isinstance(n, int) or not 0 <= n <= U64:
        raise ValidationError('u64 overflow/invalid value')
    return n


def price_from_id(bin_id: int, bin_step: int) -> int:
    if not 1 <= bin_step <= 10000 or abs(bin_id) >= 0x80000:
        raise ValidationError('price exponent/bin step out of bounds')
    if bin_id == 0:
        return Q64
    exponent = abs(bin_id)
    invert = bin_id < 0
    squared = Q64 + (bin_step << 64) // BPS
    result = Q64
    if squared >= result:
        squared = U128 // squared
        invert = not invert
    for bit in range(19):
        if exponent & (1 << bit):
            product = result * squared
            if product > U128:
                raise ValidationError('Q64 multiply overflow')
            result = product >> 64
        if bit < 18:
            product = squared * squared
            if product > U128:
                raise ValidationError('Q64 square overflow')
            squared = product >> 64
    if result == 0:
        raise ValidationError('Q64 underflow')
    return U128 // result if invert else result


def human_price(state: PoolState, b: Bin) -> D:
    price = dec(b.price or price_from_id(b.bin_id, state.bin_step)) / Q64
    price *= dec(10) ** (state.decimals_x - state.decimals_y)
    return price if state.token_x_mint == SOL_MINT else dec(1) / price


def rates(p: FeeParameters, v: VariableState, bin_step: int) -> tuple[int,int,int]:
    base = p.base_factor * bin_step * 10 * 10**p.base_fee_power_factor
    variable = ceil_div(p.variable_fee_control * (v.volatility_accumulator * bin_step)**2, 100_000_000_000)
    if base > U128 or variable > U128:
        raise ValidationError('fee rate u128 overflow')
    return base, variable, min(base + variable, MAX_FEE_RATE)


def fee_from_gross(gross: int, rate: int) -> int:
    checked_u64(gross)
    if not 0 <= rate <= MAX_FEE_RATE:
        raise ValidationError('fee rate out of bounds')
    return checked_u64(ceil_div(gross * rate, FEE_PRECISION))


def fee_from_net(net: int, rate: int) -> int:
    checked_u64(net)
    if not 0 <= rate <= MAX_FEE_RATE:
        raise ValidationError('fee rate out of bounds')
    return checked_u64(ceil_div(net * rate, FEE_PRECISION - rate))


def fee_components(total: int, base_rate: int, variable_rate: int, protocol_share: int) -> dict:
    """Analytical pro-rata-rate allocation, NOT two independently charged fees.

    Allocation uses uncapped relative rates, preserving proportions at the cap.
    Integer remainder goes to variable. Protocol base is allocated pro-rata from
    the actual protocol total; LP base+variable EXACTLY equals total-protocol.
    """
    if min(total, base_rate, variable_rate) < 0 or not 0 <= protocol_share <= BPS:
        raise ValidationError('invalid fee decomposition input')
    rate_sum = base_rate + variable_rate
    if total and not rate_sum:
        raise ValidationError('nonzero fee with zero rate')
    base = total * base_rate // rate_sum if rate_sum else 0
    protocol = total * protocol_share // BPS
    protocol_base = protocol * base // total if total else 0
    return {'trader_fee': total, 'trader_base': base, 'trader_variable': total-base,
            'protocol_fee': protocol, 'lp_base': base-protocol_base,
            'lp_variable': total-base-(protocol-protocol_base),
            'decomposition_policy': 'PRO_RATA_UNCAPPED_RATES_REMAINDER_TO_VARIABLE_V1'}


def update_references(state: PoolState, timestamp: int):
    p, v = state.parameters, state.variable
    elapsed = timestamp - v.last_update_timestamp
    if elapsed < 0:
        raise ValidationError('on-chain Clock goes backwards')
    if elapsed >= p.filter_period:
        v.index_reference = state.active_id
        v.volatility_reference = (v.volatility_accumulator * p.reduction_factor // BPS
                                  if elapsed < p.decay_period else 0)


def update_volatility(state: PoolState):
    v, p = state.variable, state.parameters
    v.volatility_accumulator = min(v.volatility_reference + abs(v.index_reference-state.active_id)*BPS,
                                   p.max_volatility_accumulator)


def amount_out(net: int, price: int, swap_for_y: bool) -> int:
    if price <= 0:
        raise ValidationError('nonpositive bin price')
    return checked_u64(net * price // Q64 if swap_for_y else net * Q64 // price)


def amount_in_for_output(out: int, price: int, swap_for_y: bool) -> int:
    return checked_u64(ceil_div(out*Q64, price) if swap_for_y else ceil_div(out*price, Q64))


@dataclass
class BinFill:
    bin_id: int
    swap_for_y: bool
    price: int
    gross_input: int
    net_input: int
    gross_output: int
    user_output: int
    fee_token: str
    trader_fee: int
    protocol_fee: int
    lp_base: int
    lp_variable: int
    base_rate: int
    variable_rate: int
    total_rate: int
    volatility_accumulator: int
    supply: int
    pre_x: int
    pre_y: int
    post_x: int
    post_y: int
    token_conservation_x: int
    token_conservation_y: int


@dataclass
class Quote:
    amount_in: int
    amount_out: int
    fee: int
    protocol_fee: int
    amount_left: int
    start_bin: int
    end_bin: int
    fills: list[BinFill]
    state: PoolState

    def summary(self):
        return {'amount_in': self.amount_in, 'amount_out': self.amount_out, 'fee': self.fee,
                'protocol_fee': self.protocol_fee, 'amount_left': self.amount_left,
                'start_bin': self.start_bin, 'end_bin': self.end_bin,
                'fills': [asdict(f) for f in self.fills]}


def _eligible(state: PoolState, timestamp: int, slot: int):
    state.validate()
    if state.status != 0:
        raise Unsupported('disabled pool')
    if state.parameters.collect_fee_mode not in (0,1):
        raise Unsupported('unknown collect fee mode')
    if state.parameters.function_type not in (0,1,2):
        raise Unsupported('unknown pool function type')
    if state.token_x_program_flag or state.token_y_program_flag:
        raise Unsupported('Token-2022 transfer fees/hooks not supported by MM engine')
    if state.pair_type not in (0,1,2,3):
        raise Unsupported('unknown pool type')
    if state.activation_type not in (0,1):
        raise Unsupported('unknown activation type')
    if state.pair_type in (1,2):
        point = slot if state.activation_type == 0 else timestamp
        if point < state.activation_point:
            raise Unsupported('pool not activated at this event')


def quote_exact_in(source: PoolState, gross_input: int, swap_for_y: bool, timestamp: int,
                   *, slot: int | None = None, allow_partial: bool = False,
                   max_bin_visits: int = 100000) -> Quote:
    """Pure quote; original state is never mutated. MM-only reserve transition.

    Fee-growth accumulators and last-update timestamp write are outside quote
    scope. A factual replay verifies those semantics against independent state.
    """
    checked_u64(gross_input)
    if gross_input == 0:
        raise ValidationError('zero-size swap is not a meaningful quote')
    if not isinstance(swap_for_y, bool):
        raise ValidationError('swap direction must be boolean')
    state = replace(source, parameters=replace(source.parameters), variable=replace(source.variable),
                    bins={i:Bin(**vars(b)) for i,b in source.bins.items()}, known_bin_arrays=list(source.known_bin_arrays))
    _eligible(state, timestamp, source.slot if slot is None else slot)
    update_references(state, timestamp)
    fee_on_input = state.parameters.collect_fee_mode == 0 or not swap_for_y
    fee_token = ('x' if swap_for_y else 'y') if fee_on_input else 'y'
    remaining = gross_input
    fills: list[BinFill] = []
    start = state.active_id
    step = -1 if swap_for_y else 1
    visits = 0
    while remaining:
        visits += 1
        if visits > max_bin_visits:
            raise Unsupported('bin traversal bound exceeded')
        if not state.parameters.min_bin_id <= state.active_id <= state.parameters.max_bin_id:
            if allow_partial:
                break
            raise InsufficientLiquidity('pool has insufficient supported liquidity')
        b = state.bins.get(state.active_id)
        if b is None:
            if state.active_id // 70 in state.known_bin_arrays:
                raise ValidationError('known BinArray has a missing bin')
            if not state.complete_bin_map:
                raise Unsupported('missing BinArray is not proven empty')
            candidates = [i for i in state.bins if (i-state.active_id)*step > 0]
            if not candidates:
                if allow_partial:
                    break
                raise InsufficientLiquidity('no further initialized bin')
            state.active_id = min(candidates) if step > 0 else max(candidates)
            continue
        if any((b.open_order_amount, b.processed_order_remaining_amount, b.total_processing_order_amount)):
            raise Unsupported('live limit-order liquidity requires SVM/order-state adapter')
        reserve = b.amount_y if swap_for_y else b.amount_x
        if reserve:
            if b.liquidity_supply <= 0:
                raise ValidationError('nonempty MM bin has zero supply')
            b = replace(b)
            state.bins[b.bin_id] = b
            b.price = b.price or price_from_id(b.bin_id, state.bin_step)
            update_volatility(state)
            base, variable, rate = rates(state.parameters, state.variable, state.bin_step)
            initial_fee = fee_from_gross(remaining, rate) if fee_on_input else 0
            available_net = remaining-initial_fee
            max_net = amount_in_for_output(reserve, b.price, swap_for_y)
            used_net = min(available_net, max_net)
            gross_out = reserve if available_net >= max_net else amount_out(used_net, b.price, swap_for_y)
            gross_out = min(gross_out, reserve)
            if fee_on_input:
                fee = fee_from_net(used_net, rate) if available_net > used_net else initial_fee
                used_gross = used_net+fee
                user_out = gross_out
            else:
                used_gross = used_net
                fee = fee_from_gross(gross_out, rate)
                user_out = gross_out-fee
            if used_gross <= 0:
                raise Unsupported('zero-progress dust quote; no fabricated fill')
            if used_gross > remaining:
                raise ValidationError('quote consumes more input than provided')
            parts = fee_components(fee, base, variable, state.parameters.protocol_share)
            pre_x, pre_y = b.amount_x, b.amount_y
            if swap_for_y:
                b.amount_x = checked_u64(b.amount_x+used_net)
                b.amount_y -= gross_out
            else:
                b.amount_y = checked_u64(b.amount_y+used_net)
                b.amount_x -= gross_out
            # Pool principal + fee vaults + trader = 0, by token.
            trader_x = -used_gross if swap_for_y else user_out
            trader_y = user_out if swap_for_y else -used_gross
            rx = b.amount_x-pre_x + trader_x + (fee if fee_token == 'x' else 0)
            ry = b.amount_y-pre_y + trader_y + (fee if fee_token == 'y' else 0)
            if rx or ry:
                raise ValidationError('swap token conservation failed')
            fills.append(BinFill(b.bin_id, swap_for_y, b.price, used_gross, used_net, gross_out, user_out,
                                 fee_token, fee, parts['protocol_fee'], parts['lp_base'], parts['lp_variable'],
                                 base, variable, rate, state.variable.volatility_accumulator, b.liquidity_supply,
                                 pre_x, pre_y, b.amount_x, b.amount_y, rx, ry))
            remaining -= used_gross
        if remaining:
            state.active_id += step
    if not fills:
        raise InsufficientLiquidity('no filled liquidity')
    return Quote(gross_input-remaining, sum(f.user_output for f in fills),
                 sum(f.trader_fee for f in fills), sum(f.protocol_fee for f in fills),
                 remaining, start, state.active_id, fills, state)


def composition_fee(delta_amount: int, total_fee_rate: int, protocol_share: int) -> dict:
    """Integer amount formula; determining the actual composition delta is separate.

    A caller must obtain delta_amount from verified deposit/pre-state semantics;
    this function never guesses it from total deposit amount.
    """
    if delta_amount<0 or not 0<=total_fee_rate<=MAX_FEE_RATE or not 0<=protocol_share<=10000:
        raise ValidationError('invalid composition fee inputs')
    total=delta_amount*total_fee_rate*(FEE_PRECISION+total_fee_rate)//(FEE_PRECISION**2)
    protocol=total*protocol_share//10000
    return {'trader_total':total,'lp_composition':total-protocol,'protocol':protocol,
            'delta_source':'CALLER_MUST_PROVIDE_VERIFIED_COMPOSITION_DELTA'}
