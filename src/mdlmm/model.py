from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import IntEnum, StrEnum
from typing import Any, Callable
from .errors import ResearchError, Unavailable, Unsupported, ValidationError
from .util import digest

PROGRAM_ID = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'
SOL_MINT = 'So11111111111111111111111111111111111111112'
USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
CLOCK_ID = 'SysvarC1ock11111111111111111111111111111111'
DEFAULT_PUBKEY = '11111111111111111111111111111111'

# Version 2 includes the newly preserved LO/account fields and distinguishes
# unavailable evidence (None) from observed zero. Stored v1 continuity hashes
# must not certify this projection. Resealing requires a separately versioned
# dataset; reading legacy states does not rewrite their frozen artifacts.
ECONOMIC_STATE_HASH_SCHEMA = 'MDLMM_ECONOMIC_STATE_V2'
BIN_LIMIT_ORDER_FIELDS = (
    'open_order_amount', 'processed_order_remaining_amount',
    'total_processing_order_amount', 'limit_order_ask_side',
    'fulfilled_order_amount_x', 'fulfilled_order_amount_y',
    'limit_order_fee_ask_side', 'limit_order_fee_bid_side', 'order_age',
)
POOL_PRESERVED_ACCOUNT_FIELDS = (
    'reward_mints', 'protocol_fee_x', 'protocol_fee_y',
    'bin_array_bitmap', 'last_updated_at',
)


class FunctionType(IntEnum):
    UNDETERMINED = 0
    LIQUIDITY_MINING = 1
    LIMIT_ORDER = 2


def _program_integer(name: str, value: int | None, bits: int, *,
                     signed: bool = False, optional: bool = False):
    if optional and value is None:
        return
    lower = -(1 << (bits - 1)) if signed else 0
    upper = 1 << (bits - 1 if signed else bits)
    if type(value) is not int or not lower <= value < upper:
        kind = 'i' if signed else 'u'
        raise ValidationError(f'{name} must be an integer {kind}{bits}')


def _optional_tuple(name: str, value: Any, length: int) -> tuple | None:
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise ValidationError(f'{name} must contain exactly {length} entries')
    return tuple(value)


def _validate_reward_mints(value: tuple[str, str] | None):
    if value is None:
        return
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValidationError('reward_mints must be an immutable pair of public keys')
    # Import at call time: codec imports the model to build decoded states.
    from .codec import b58decode
    for mint in value:
        if len(b58decode(mint)) != 32:
            raise ValidationError('reward mint must encode a 32-byte public key')

class Status(StrEnum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    UNAVAILABLE = 'UNAVAILABLE'

class Level(StrEnum):
    EXACT = 'EXACT'
    SDK_PARITY = 'SDK_PARITY'
    ACCOUNT_PARITY = 'ACCOUNT_PARITY'
    PARTIAL_RECONSTRUCTION = 'PARTIAL_RECONSTRUCTION'
    UNVERIFIED = 'UNVERIFIED'

@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ''
    metrics: dict = field(default_factory=dict)


def checked(name: str, fn: Callable[[], Any]) -> Check:
    try:
        result = fn()
        if result is False:
            raise ValidationError('comparison returned False')
        return Check(name, Status.PASS, metrics=result if isinstance(result, dict) else {})
    except Unavailable as exc:
        return Check(name, Status.UNAVAILABLE, str(exc))
    except (ResearchError, ValueError, KeyError, TypeError, AssertionError, OSError) as exc:
        return Check(name, Status.FAIL, f'{type(exc).__name__}: {exc}')


def aggregate(checks: list[Check]) -> Status:
    if not checks:
        return Status.UNAVAILABLE
    if any(c.status == Status.FAIL for c in checks):
        return Status.FAIL
    if any(c.status == Status.UNAVAILABLE for c in checks):
        return Status.UNAVAILABLE
    return Status.PASS

@dataclass(order=True, frozen=True)
class Order:
    slot: int
    transaction_index: int
    instruction_index: int
    inner_instruction_index: int = -1
    event_index: int = 0

    def __post_init__(self):
        if any(type(v) is not int for v in vars(self).values()):
            raise ValidationError('transaction ordering fields must be integers')
        if min(self.slot, self.transaction_index, self.instruction_index, self.event_index) < 0:
            raise ValidationError('unresolved transaction/instruction order')
        if self.inner_instruction_index < -1:
            raise ValidationError('invalid inner instruction index')

@dataclass
class FeeParameters:
    base_factor: int
    filter_period: int
    decay_period: int
    reduction_factor: int
    variable_fee_control: int
    max_volatility_accumulator: int
    protocol_share: int
    base_fee_power_factor: int = 0
    collect_fee_mode: int = 0
    function_type: int = 0
    min_bin_id: int = -443636
    max_bin_id: int = 443636

    def __post_init__(self):
        if any(type(v) is not int for v in vars(self).values()):
            raise ValidationError('fee parameters must be integer program units')
        for k in ('base_factor', 'filter_period', 'decay_period', 'reduction_factor', 'variable_fee_control',
                  'max_volatility_accumulator', 'protocol_share', 'base_fee_power_factor'):
            if getattr(self, k) < 0:
                raise ValidationError(f'negative fee parameter: {k}')
        if not 0 <= self.protocol_share <= 10000 or not 0 <= self.reduction_factor <= 10000:
            raise ValidationError('invalid basis-point parameter')
        if self.filter_period > self.decay_period:
            raise ValidationError('filter period exceeds decay period')

@dataclass
class VariableState:
    volatility_accumulator: int
    volatility_reference: int
    index_reference: int
    last_update_timestamp: int

@dataclass
class Bin:
    bin_id: int
    amount_x: int
    amount_y: int
    price: int
    liquidity_supply: int
    fee_x_stored: int = 0
    fee_y_stored: int = 0
    # None means the serialized evidence did not retain this account field.
    # In particular, legacy/synthetic MM state must not invent observed LO zeros.
    open_order_amount: int | None = None
    processed_order_remaining_amount: int | None = None
    total_processing_order_amount: int | None = None
    limit_order_ask_side: int | None = None
    fulfilled_order_amount_x: int | None = None
    fulfilled_order_amount_y: int | None = None
    limit_order_fee_ask_side: int | None = None
    limit_order_fee_bid_side: int | None = None
    order_age: int | None = None

    @property
    def limit_order_fields_complete(self) -> bool:
        """All LO fields are represented; this is not a mutation-parity claim."""
        return all(getattr(self, name) is not None for name in BIN_LIMIT_ORDER_FIELDS)

    def validate(self):
        _program_integer('bin_id', self.bin_id, 32, signed=True)
        for k in ('amount_x', 'amount_y'):
            _program_integer(f'bin {k}', getattr(self, k), 64)
        for k in ('price', 'liquidity_supply', 'fee_x_stored', 'fee_y_stored'):
            _program_integer(f'bin {k}', getattr(self, k), 128)
        for k in BIN_LIMIT_ORDER_FIELDS:
            bits = 32 if k == 'order_age' else 8 if k == 'limit_order_ask_side' else 64
            _program_integer(f'bin {k}', getattr(self, k), bits, optional=True)

@dataclass
class PoolState:
    pool: str
    active_id: int
    bin_step: int
    parameters: FeeParameters
    variable: VariableState
    bins: dict[int, Bin]
    token_x_mint: str = SOL_MINT
    token_y_mint: str = USDC_MINT
    decimals_x: int = 9
    decimals_y: int = 6
    program_id: str = PROGRAM_ID
    program_version: str | None = None
    binary_sha256: str | None = None
    idl_hash: str | None = None
    slot: int = 0
    status: int = 0
    pair_type: int = 0
    activation_type: int = 0
    activation_point: int = 0
    token_x_program_flag: int = 0
    token_y_program_flag: int = 0
    known_bin_arrays: list[int] = field(default_factory=list)
    complete_bin_map: bool = False
    data_kind: str = 'HISTORICAL'
    # Preserved account evidence. None is unknown, not an empty reward/bitmap
    # or zero fee balance. Tuples keep the existing pure-quote clone safe.
    reward_mints: tuple[str, str] | None = None
    protocol_fee_x: int | None = None
    protocol_fee_y: int | None = None
    bin_array_bitmap: tuple[int, ...] | None = None
    last_updated_at: int | None = None

    def __post_init__(self):
        self.reward_mints = _optional_tuple('reward_mints', self.reward_mints, 2)
        self.bin_array_bitmap = _optional_tuple('bin_array_bitmap', self.bin_array_bitmap, 16)

    @property
    def effective_function_type(self) -> FunctionType | None:
        """Resolve the recorded mode; missing reward evidence stays unknown.

        This classifies account state only. It does not enable LO execution in
        the existing MM-only quote/replay engine.
        """
        _program_integer('function_type', self.parameters.function_type, 8)
        try:
            raw = FunctionType(self.parameters.function_type)
        except ValueError as exc:
            raise Unsupported(f'unknown pool function type {self.parameters.function_type}') from exc
        if raw != FunctionType.UNDETERMINED:
            return raw
        if self.reward_mints is None:
            return None
        _validate_reward_mints(self.reward_mints)
        return (FunctionType.LIMIT_ORDER if all(m == DEFAULT_PUBKEY for m in self.reward_mints)
                else FunctionType.LIQUIDITY_MINING)

    def validate(self):
        if self.program_id != PROGRAM_ID:
            raise ValidationError('unexpected DLMM program')
        if {self.token_x_mint, self.token_y_mint} != {SOL_MINT, USDC_MINT}:
            raise ValidationError('dataset is not canonical wrapped SOL / USDC')
        expected = (9, 6) if self.token_x_mint == SOL_MINT else (6, 9)
        if (self.decimals_x, self.decimals_y) != expected:
            raise ValidationError('canonical mint decimal mismatch')
        if not 1 <= self.bin_step <= 10000:
            raise ValidationError('invalid bin step')
        if self.parameters.min_bin_id > self.active_id or self.active_id > self.parameters.max_bin_id:
            raise ValidationError('active bin outside supported bounds')
        _validate_reward_mints(self.reward_mints)
        for k in ('protocol_fee_x', 'protocol_fee_y'):
            _program_integer(k, getattr(self, k), 64, optional=True)
        _program_integer('last_updated_at', self.last_updated_at, 64, signed=True, optional=True)
        if self.bin_array_bitmap is not None:
            if not isinstance(self.bin_array_bitmap, tuple) or len(self.bin_array_bitmap) != 16:
                raise ValidationError('bin_array_bitmap must be an immutable tuple of 16 u64 words')
            for word in self.bin_array_bitmap:
                _program_integer('bin_array_bitmap word', word, 64)
        for k, b in self.bins.items():
            if k != b.bin_id:
                raise ValidationError('bin key/id mismatch')
            b.validate()

    @classmethod
    def from_dict(cls, data: dict) -> 'PoolState':
        d = dict(data)
        d['parameters'] = FeeParameters(**d['parameters'])
        d['variable'] = VariableState(**d['variable'])
        d['bins'] = {int(k): Bin(**v) for k, v in d['bins'].items()}
        out = cls(**d)
        out.validate()
        return out

    def to_dict(self) -> dict:
        # Financial state leaves are immutable integers/strings/tuples. Avoid
        # dataclasses.asdict's recursive deepcopy for every historical swap.
        d = {k: v for k, v in vars(self).items() if k not in ('parameters','variable','bins','known_bin_arrays')}
        d['parameters'] = vars(self.parameters).copy()
        d['variable'] = vars(self.variable).copy()
        d['bins'] = {i: vars(b).copy() for i,b in self.bins.items()}
        d['known_bin_arrays'] = list(self.known_bin_arrays)
        return d

    def economic_hash(self) -> str:
        return economic_state_hash(self.to_dict())


def economic_state_hash(data: dict) -> str:
    """Versioned continuity projection, not full account/mutation parity.

    New preserved fields participate even when the MM engine cannot update
    them. Missing legacy fields normalize to unknown, never observed zero.
    MM fee-growth rounding remains outside the original continuity scope.
    """
    # Slot/provenance labels are checked separately. The same economic state at
    # END_SLOT N and BEFORE_INSTRUCTION N+1 must have the same continuity hash.
    excluded={'slot','program_version','binary_sha256','idl_hash','data_kind'}
    d={k:v for k,v in data.items() if k not in excluded and k!='bins'}
    for name in POOL_PRESERVED_ACCOUNT_FIELDS:
        d.setdefault(name, None)
    bins = {}
    for i, b in data['bins'].items():
        projected = {k:v for k,v in b.items() if k not in ('fee_x_stored','fee_y_stored')}
        for name in BIN_LIMIT_ORDER_FIELDS:
            projected.setdefault(name, None)
        bins[str(i)] = projected
    d['bins'] = bins
    return digest({'schema': ECONOMIC_STATE_HASH_SCHEMA, 'state': d})

@dataclass
class Event:
    order: Order
    block_time_ms: int
    tx_signature: str
    pool: str
    kind: str
    program_id: str = PROGRAM_ID
    program_version: str | None = None
    binary_sha256: str | None = None
    idl_hash: str | None = None
    clock_unix_timestamp: int | None = None
    clock_source: str | None = None
    observed: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    pre_state_hash: str | None = None
    post_state_hash: str | None = None
    raw_sha256: str | None = None
    data_kind: str = 'HISTORICAL'

    @classmethod
    def from_dict(cls, data: dict) -> 'Event':
        d = dict(data)
        d['order'] = Order(**d['order'])
        return cls(**d)

    @property
    def id(self) -> str:
        return digest({'signature': self.tx_signature, 'order': asdict(self.order), 'pool': self.pool})
