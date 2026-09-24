from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import StrEnum
from typing import Any, Callable
from .errors import ResearchError, Unavailable, ValidationError
from .util import digest

PROGRAM_ID = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'
SOL_MINT = 'So11111111111111111111111111111111111111112'
USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
CLOCK_ID = 'SysvarC1ock11111111111111111111111111111111'

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
    open_order_amount: int = 0
    processed_order_remaining_amount: int = 0
    total_processing_order_amount: int = 0
    limit_order_ask_side: int = 0

    def validate(self):
        if any(type(v) is not int for v in vars(self).values()):
            raise ValidationError('bin state must use integer token/program units')
        for k in ('amount_x', 'amount_y', 'price', 'liquidity_supply', 'fee_x_stored', 'fee_y_stored',
                  'open_order_amount', 'processed_order_remaining_amount', 'total_processing_order_amount'):
            if getattr(self, k) < 0:
                raise ValidationError(f'negative bin {k}')
        for k in ('amount_x', 'amount_y'):
            if getattr(self, k) >= 2**64:
                raise ValidationError(f'bin {k} overflows u64')
        if self.price >= 2**128 or self.liquidity_supply >= 2**128:
            raise ValidationError('bin u128 overflow')

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
        # All financial state leaves are immutable integers/strings. Avoid
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
    # Slot/provenance labels are checked separately. The same economic state at
    # END_SLOT N and BEFORE_INSTRUCTION N+1 must have the same continuity hash.
    excluded={'slot','program_version','binary_sha256','idl_hash','data_kind'}
    d={k:v for k,v in data.items() if k not in excluded and k!='bins'}
    d['bins']={str(i):{k:v for k,v in b.items() if k not in ('fee_x_stored','fee_y_stored')}
               for i,b in data['bins'].items()}
    return digest(d)

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
