"""Pure, bounded pool quotes against the pinned Meteora SDK semantics.

Reference: dlmm-sdk 576919e3e4368e542c402f000b4264724f7f23ec,
ts-client/src/dlmm/index.ts swapQuote/swapQuoteExactOut and helpers/binArray.ts.
The result describes quote amounts and the references used to compute them.
It has no post-state and cannot authorize historical replay or account writes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Literal

from .codec import Reader, discriminator
from .errors import InsufficientLiquidity, Unavailable, Unsupported, ValidationError
from .mechanics import _eligible, rates, update_references, update_volatility
from .model import ECONOMIC_STATE_HASH_SCHEMA, Event, FunctionType, PoolState, _program_integer
from .swap_math import (
    U64_MAX, BinQuote, FeeSplit, max_amount_out,
    quote_exact_in_at_bin, quote_exact_out_at_bin,
)
from .util import sha256

SDK_COMMIT = '576919e3e4368e542c402f000b4264724f7f23ec'
SDK_VERSION = '1.9.14'
INTERNAL_MIN, INTERNAL_MAX = -512, 511
EXTENSION_MIN, EXTENSION_MAX = -6656, 6655
BINS_PER_ARRAY = 70


class SwapMode(StrEnum):
    EXACT_IN = 'EXACT_IN'
    EXACT_OUT = 'EXACT_OUT'


def _u64(name: str, value: int, *, positive: bool = False) -> int:
    if type(value) is not int or not int(positive) <= value <= U64_MAX:
        raise ValidationError(f'{name} must be an integer u64' + (' greater than zero' if positive else ''))
    return value


def _wire_integer(name: str, value) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        # Bound before conversion, including against oversized untrusted input.
        if len(value) > 20:
            raise ValidationError(f'{name} exceeds u64')
        value = int(value)
    return _u64(name, value)


def _validate_quote_state_scalars(source: PoolState) -> None:
    """Enforce the pinned LbPair integer layout before quote arithmetic.

    Legacy VariableState/PoolState validation intentionally remains unchanged.
    In this API, even values that reference updates would later replace or
    clamp must be valid observed program integers; bool is never an integer
    account field. Widths match codec.decode_lb_pair and the pinned SDK IDL.
    """
    for name in ('volatility_accumulator', 'volatility_reference'):
        _program_integer('variable.' + name, getattr(source.variable, name), 32)
    _program_integer('variable.index_reference', source.variable.index_reference, 32, signed=True)
    _program_integer('variable.last_update_timestamp', source.variable.last_update_timestamp, 64, signed=True)
    for bits, names in (
        (16, ('base_factor', 'filter_period', 'decay_period', 'reduction_factor', 'protocol_share')),
        (32, ('variable_fee_control', 'max_volatility_accumulator')),
        (8, ('base_fee_power_factor', 'function_type', 'collect_fee_mode')),
    ):
        for name in names:
            _program_integer('parameters.' + name, getattr(source.parameters, name), bits)
    for name in ('min_bin_id', 'max_bin_id'):
        _program_integer('parameters.' + name, getattr(source.parameters, name), 32, signed=True)
    _program_integer('active_id', source.active_id, 32, signed=True)
    _program_integer('bin_step', source.bin_step, 16)


@dataclass(frozen=True, slots=True)
class SwapIntent:
    """Actual instruction constraints, distinct from observed consumed amounts.

    Exact-out fixes user output and caps input. It must never be reconstructed
    as exact-in from a Swap event's consumed input. Host presence is explicit;
    this pinned SDK quote interface only supports an absent host account.
    """

    mode: SwapMode
    swap_for_y: bool
    amount_in: int | None = None
    min_amount_out: int | None = None
    out_amount: int | None = None
    max_in_amount: int | None = None
    host_fee_present: bool | None = False

    def __post_init__(self):
        try:
            object.__setattr__(self, 'mode', SwapMode(self.mode))
        except (ValueError, TypeError) as exc:
            raise ValidationError('unknown swap intent mode') from exc
        if type(self.swap_for_y) is not bool:
            raise ValidationError('swap_for_y must be boolean')
        if self.host_fee_present is None:
            raise Unavailable('host account presence is unknown')
        if type(self.host_fee_present) is not bool:
            raise ValidationError('host_fee_present must be boolean')
        if self.mode == SwapMode.EXACT_IN:
            _u64('amount_in', self.amount_in, positive=True)
            _u64('min_amount_out', self.min_amount_out)
            if self.out_amount is not None or self.max_in_amount is not None:
                raise ValidationError('exact-in intent contains exact-out constraints')
        else:
            _u64('out_amount', self.out_amount, positive=True)
            _u64('max_in_amount', self.max_in_amount, positive=True)
            if self.amount_in is not None or self.min_amount_out is not None:
                raise ValidationError('exact-out intent contains exact-in constraints')

    @classmethod
    def exact_in(cls, amount_in: int, swap_for_y: bool, *, min_amount_out: int = 0,
                 host_fee_present: bool | None = False) -> 'SwapIntent':
        return cls(SwapMode.EXACT_IN, swap_for_y, amount_in=amount_in,
                   min_amount_out=min_amount_out, host_fee_present=host_fee_present)

    @classmethod
    def exact_out(cls, out_amount: int, swap_for_y: bool, *, max_in_amount: int = U64_MAX,
                  host_fee_present: bool | None = False) -> 'SwapIntent':
        return cls(SwapMode.EXACT_OUT, swap_for_y, out_amount=out_amount,
                   max_in_amount=max_in_amount, host_fee_present=host_fee_present)

    def to_request(self) -> dict:
        """Lossless Node wire format; u64 values do not pass through JS floats."""
        result = {'mode': self.mode.value, 'swap_for_y': self.swap_for_y,
                  'host_fee_present': self.host_fee_present}
        names = (('amount_in', 'min_amount_out') if self.mode == SwapMode.EXACT_IN
                 else ('out_amount', 'max_in_amount'))
        result.update({name: str(getattr(self, name)) for name in names})
        return result

    @classmethod
    def from_request(cls, request: dict) -> 'SwapIntent':
        if not isinstance(request, dict):
            raise ValidationError('swap request must be an object')
        try:
            mode = SwapMode(request.get('mode'))
        except (ValueError, TypeError) as exc:
            raise ValidationError('typed swap request requires mode EXACT_IN or EXACT_OUT') from exc
        names = (('amount_in', 'min_amount_out') if mode == SwapMode.EXACT_IN
                 else ('out_amount', 'max_in_amount'))
        if set(request) != {'mode', 'swap_for_y', 'host_fee_present', *names}:
            raise ValidationError('typed swap request must contain exactly its actual constraints and host presence')
        return cls(mode, request['swap_for_y'], host_fee_present=request['host_fee_present'],
                   **{name: _wire_integer(name, request[name]) for name in names})

    @classmethod
    def from_event(cls, event: Event) -> 'SwapIntent':
        payload = event.payload
        if (event.kind != 'SWAP' or payload.get('normalization_mode') != 'CANONICAL_PAIRED'
                or payload.get('canonical_pair_verified') is not True):
            raise Unavailable('actual canonical swap instruction is required for intent')
        name = payload.get('instruction_name')
        if name not in ('swap', 'swap2', 'swap_exact_out', 'swap_exact_out2'):
            raise Unsupported('price-impact or unknown instruction bounds are not supported by SwapIntent')
        args = payload.get('instruction_args')
        if not isinstance(args, dict):
            raise Unavailable('actual instruction arguments are missing')
        mode = SwapMode.EXACT_OUT if name.startswith('swap_exact_out') else SwapMode.EXACT_IN
        if payload.get('exact_out') is not (mode == SwapMode.EXACT_OUT):
            raise ValidationError('instruction name and exact_out evidence disagree')
        args = dict(args)
        if name.endswith('2'):
            info = args.pop('remaining_accounts_info', None)
            if info != {'slices': []}:
                raise Unsupported('modern swap requires observed empty remaining-account slices for SPL-only quote scope')
        # swap_for_y was independently observed and paired by normalization;
        # these instructions encode direction by the input/output accounts.
        return cls.from_request({'mode': mode.value, 'swap_for_y': payload.get('swap_for_y'),
                                 'host_fee_present': payload.get('host_fee_present'), **args})


@dataclass(frozen=True, slots=True)
class BitmapExtension:
    """Observed extension bitmap, separate from evidence that its PDA is absent."""

    pool: str
    positive: tuple[tuple[int, ...], ...]
    negative: tuple[tuple[int, ...], ...]
    account_sha256: str | None = None

    def __post_init__(self):
        for name in ('positive', 'negative'):
            rows = getattr(self, name)
            if not isinstance(rows, (tuple, list)) or len(rows) != 12:
                raise ValidationError('extension bitmap must contain 12 groups on each side')
            normalized = []
            for row in rows:
                if not isinstance(row, (tuple, list)) or len(row) != 8:
                    raise ValidationError('extension bitmap group must contain 8 u64 words')
                normalized.append(tuple(_u64('extension bitmap word', value) for value in row))
            object.__setattr__(self, name, tuple(normalized))

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'BitmapExtension':
        if len(raw) != 1576 or raw[:8] != discriminator('account', 'BinArrayBitmapExtension'):
            raise Unsupported('unrecognized pinned BinArrayBitmapExtension layout')
        reader = Reader(raw, 8)
        pool = reader.key()
        sides = [tuple(tuple(reader.integer(64) for _ in range(8)) for _ in range(12))
                 for _ in range(2)]
        return cls(pool, sides[0], sides[1], sha256(raw))

    def initialized_indices(self) -> tuple[int, ...]:
        indices = []
        for positive, groups in ((True, self.positive), (False, self.negative)):
            for group_index, words in enumerate(groups):
                for word_index, word in enumerate(words):
                    while word:
                        bit = (word & -word).bit_length() - 1
                        offset = (group_index + 1) * 512 + word_index * 64 + bit
                        indices.append(offset if positive else -offset - 1)
                        word &= word - 1
        return tuple(sorted(indices))


@dataclass(frozen=True, slots=True)
class QuoteReference:
    source_economic_hash: str
    timestamp: int
    slot: int
    index_reference: int
    volatility_reference: int
    final_quote_volatility_accumulator: int
    source_last_update_timestamp: int
    visited_bin_arrays: tuple[int, ...]
    bitmap_extension_evidence: Literal['UNKNOWN', 'ABSENT', 'PRESENT']
    hash_schema: str = ECONOMIC_STATE_HASH_SCHEMA
    sdk_commit: str = SDK_COMMIT
    sdk_version: str = SDK_VERSION
    scope: str = 'PINNED_SDK_QUOTE_AMOUNTS_NO_ACCOUNT_MUTATION'


@dataclass(frozen=True, slots=True)
class PoolBinQuote:
    quote: BinQuote
    price: int
    base_rate: int
    variable_rate: int
    total_rate: int
    volatility_accumulator: int


@dataclass(frozen=True, slots=True)
class PoolQuote:
    """Quote-only result. SDK fee scalars must not be used as account writes.

    ``fee_split`` is nominal trading-fee allocation in ``fee_token``. The
    exact-out ``rounding_output_delta`` is in ``output_token``; the SDK adds
    delta > 1 to its protocol scalar even when those tokens differ. Actual
    program accounting requires independent transition evidence.
    """

    intent: SwapIntent
    amount_in: int
    amount_out: int
    input_remaining: int | None
    output_remaining: int | None
    input_cap_remaining: int | None
    fee_token: Literal['x', 'y']
    output_token: Literal['x', 'y']
    fee_split: FeeSplit
    trading_fee: int
    rounding_output_delta: int
    sdk_protocol_rounding_delta: int
    sdk_fee: int
    sdk_protocol_fee: int
    rust_sdk_fee: int
    start_bin: int
    end_bin: int
    fills: tuple[PoolBinQuote, ...]
    reference: QuoteReference

    def summary(self) -> dict:
        return asdict(self)


def _initialized_internal(words: tuple[int, ...]) -> tuple[int, ...]:
    result = []
    for word_index, word in enumerate(words):
        while word:
            bit = (word & -word).bit_length() - 1
            result.append(word_index * 64 + bit + INTERNAL_MIN)
            word &= word - 1
    return tuple(result)


def _next_array(active: int, swap_for_y: bool, internal: tuple[int, ...],
                extension: tuple[int, ...] | None, extension_known: bool) -> int:
    current = active // BINS_PER_ARRAY
    if not EXTENSION_MIN <= current <= EXTENSION_MAX:
        raise Unsupported('active bin array exceeds pinned bitmap bounds')
    # Outside the internal range the SDK consults the extension before it can
    # move inward, even if a populated internal array was supplied.
    if not INTERNAL_MIN <= current <= INTERNAL_MAX and extension is None:
        if not extension_known:
            raise Unavailable('bitmap extension presence/content is unknown')
        raise InsufficientLiquidity('no bitmap extension for the active bin array')
    candidates = [i for i in internal if (i <= current if swap_for_y else i >= current)]
    if extension is not None:
        candidates += [i for i in extension if (i <= current if swap_for_y else i >= current)]
    if candidates:
        # A missing extension on the other side cannot precede an internal bit
        # when the cursor starts inside the internal range.
        return max(candidates) if swap_for_y else min(candidates)
    if not extension_known:
        raise Unavailable('further liquidity requires unknown bitmap extension evidence')
    raise InsufficientLiquidity('no further initialized bin array in recorded bitmaps')


def quote_swap(source: PoolState, intent: SwapIntent, timestamp: int, *,
               slot: int | None = None, bitmap_extension: BitmapExtension | None = None,
               extension_known: bool = False, max_bin_visits: int = 100000) -> PoolQuote:
    """Quote full exact-in/out intent using observed bitmap/account coverage.

    Unknown arrays are never inferred empty. ``extension_known=True`` with no
    extension means the relevant PDA was explicitly observed absent. Providing
    an extension itself supplies known presence. Partial fills are not an
    executable intent result: insufficiency or a violated bound raises.
    """
    if not isinstance(intent, SwapIntent):
        raise ValidationError('quote_swap requires a typed SwapIntent')
    if intent.host_fee_present:
        raise Unsupported('pinned SDK pool quotes do not model host/referral allocation')
    if type(timestamp) is not int or not -(1 << 63) <= timestamp < (1 << 63):
        raise ValidationError('timestamp must be integer Clock i64 seconds')
    quote_slot = _u64('slot', source.slot if slot is None else slot)
    if type(extension_known) is not bool or type(max_bin_visits) is not int or max_bin_visits <= 0:
        raise ValidationError('invalid traversal evidence or visit bound')
    _validate_quote_state_scalars(source)
    # These helpers only mutate active_id and the copied variable parameters.
    state = replace(source, parameters=replace(source.parameters), variable=replace(source.variable))
    _eligible(state, timestamp, quote_slot)
    effective_mode = state.effective_function_type
    if effective_mode is None:
        raise Unavailable('effective function type requires recorded reward mints')
    support_lo = effective_mode == FunctionType.LIMIT_ORDER
    if state.bin_array_bitmap is None:
        raise Unavailable('pool quote requires the recorded internal bin-array bitmap')
    if bitmap_extension is not None:
        if not isinstance(bitmap_extension, BitmapExtension) or bitmap_extension.pool != source.pool:
            raise ValidationError('bitmap extension belongs to another pool')
        extension_known = True
    internal = _initialized_internal(state.bin_array_bitmap)
    external = bitmap_extension.initialized_indices() if bitmap_extension is not None else None
    arrays = source.known_bin_arrays
    if (not isinstance(arrays, list) or any(type(i) is not int for i in arrays)
            or len(set(arrays)) != len(arrays)):
        raise ValidationError('known_bin_arrays must contain distinct integer indices')
    update_references(state, timestamp)
    fee_on_input = state.parameters.collect_fee_mode == 0 or not intent.swap_for_y
    fee_token = ('x' if intent.swap_for_y else 'y') if fee_on_input else 'y'
    output_token = 'y' if intent.swap_for_y else 'x'
    remaining = intent.amount_in if intent.mode == SwapMode.EXACT_IN else intent.out_amount
    fills: list[PoolBinQuote] = []
    visited_arrays: list[int] = []
    visited_bins = 0
    while remaining:
        if not state.parameters.min_bin_id <= state.active_id <= state.parameters.max_bin_id:
            raise InsufficientLiquidity('pool bin bounds exhausted')
        index = _next_array(state.active_id, intent.swap_for_y, internal, external, extension_known)
        if index not in arrays:
            raise Unavailable(f'bitmap-selected BinArray {index} is not captured')
        if index not in visited_arrays:
            missing = [i for i in range(index * 70, index * 70 + 70) if i not in state.bins]
            if missing:
                raise ValidationError(f'known BinArray {index} is incomplete')
            visited_arrays.append(index)
        if state.active_id // 70 != index:
            state.active_id = index * 70 + (69 if intent.swap_for_y else 0)
            if not state.parameters.min_bin_id <= state.active_id <= state.parameters.max_bin_id:
                raise InsufficientLiquidity('next initialized array lies outside pool bin bounds')
        visited_bins += 1
        if visited_bins > max_bin_visits:
            raise Unsupported('pool quote traversal bound exceeded')
        bin = state.bins[state.active_id]
        if max_amount_out(bin, swap_for_y=intent.swap_for_y, support_limit_order=support_lo):
            if bin.price == 0:
                raise Unavailable('nonempty bin requires an observed positive price for SDK parity')
            update_volatility(state)
            base, variable, rate = rates(state.parameters, state.variable, state.bin_step)
            fn = quote_exact_in_at_bin if intent.mode == SwapMode.EXACT_IN else quote_exact_out_at_bin
            result = fn(bin, remaining, swap_for_y=intent.swap_for_y,
                        support_limit_order=support_lo, fee_on_input=fee_on_input,
                        trade_fee_numerator=rate, protocol_share=state.parameters.protocol_share)
            consumed = result.amount_in if intent.mode == SwapMode.EXACT_IN else result.amount_out
            if consumed > remaining:
                raise ValidationError('bin quote exceeds remaining intent')
            if consumed:
                fills.append(PoolBinQuote(result, bin.price, base, variable, rate,
                                          state.variable.volatility_accumulator))
                remaining -= consumed
        if remaining:
            state.active_id += -1 if intent.swap_for_y else 1
    if not fills:
        raise InsufficientLiquidity('no filled liquidity')
    amount_in = _u64('total consumed input', sum(f.quote.amount_in for f in fills))
    amount_out = _u64('total output', sum(f.quote.amount_out for f in fills))
    if intent.mode == SwapMode.EXACT_IN and amount_out < intent.min_amount_out:
        raise ValidationError('quote violates actual minimum output constraint')
    if intent.mode == SwapMode.EXACT_OUT and amount_in > intent.max_in_amount:
        raise ValidationError('quote violates actual maximum input constraint')
    split = FeeSplit(**{name: sum(getattr(f.quote.fee_split, name) for f in fills)
                        for name in FeeSplit.__dataclass_fields__})
    reference = QuoteReference(
        source.economic_hash(), timestamp, quote_slot, state.variable.index_reference,
        state.variable.volatility_reference, state.variable.volatility_accumulator,
        source.variable.last_update_timestamp, tuple(visited_arrays),
        'PRESENT' if bitmap_extension is not None else 'ABSENT' if extension_known else 'UNKNOWN')
    return PoolQuote(
        intent, amount_in, amount_out,
        0 if intent.mode == SwapMode.EXACT_IN else None,
        0 if intent.mode == SwapMode.EXACT_OUT else None,
        intent.max_in_amount - amount_in if intent.mode == SwapMode.EXACT_OUT else None,
        fee_token, output_token, split,
        sum(f.quote.trading_fee for f in fills), sum(f.quote.rounding_output_delta for f in fills),
        sum(f.quote.sdk_protocol_rounding_delta for f in fills),
        sum(f.quote.sdk_fee for f in fills), sum(f.quote.sdk_protocol_fee for f in fills),
        sum(f.quote.rust_sdk_fee for f in fills), source.active_id, fills[-1].quote.bin_id,
        tuple(fills), reference)
