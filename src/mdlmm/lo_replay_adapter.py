"""Explicit, evidence-bounded DLMM LIMIT_ORDER replay adapter.

This adapter consumes validated canonical economic swaps and a full, separately
verified source PoolState. The quote and mutation are compared to the observed
Swap2Evt before any caller-owned replay state is advanced.

An event-parity PASS is NOT a full-account, oracle, reserve, SPL transfer or
historical block completeness claim; those require independent raw witnesses.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Mapping

from .errors import Unavailable, Unsupported, ValidationError
from .model import Event, PoolState, PROGRAM_ID, FunctionType
from .swap_quote import BitmapExtension, SwapIntent
from .swap_transition import PROGRAM_SHA256, simulate_swap
from .util import digest

MODEL = 'LO_V012'
SCOPE = 'LIMIT_ORDER_REPRESENTED_STATE_WITH_EVENT_PARITY_NOT_FULL_PROGRAM_BYTES'
OBSERVED_FIELDS = (
    'start_bin_id', 'end_bin_id', 'amount_in', 'amount_out',
    'amount_left', 'swap_for_y', 'fee_bps', 'mm_fee',
    'limit_order_fee', 'protocol_fee', 'host_fee',
    'fees_on_input', 'fees_on_token_x',
)
COMPARE_FIELDS = (
    'active_id', 'bin_step', 'parameters', 'variable', 'bins',
    'token_x_mint', 'token_y_mint', 'decimals_x', 'decimals_y',
    'status', 'pair_type', 'activation_type', 'activation_point',
    'token_x_program_flag', 'token_y_program_flag',
    'reward_mints', 'protocol_fee_x', 'protocol_fee_y',
    'bin_array_bitmap', 'last_updated_at', 'known_bin_arrays',
)


def compare_represented_lo_states(actual: PoolState, checkpoint: PoolState) -> dict:
    """Compare every represented bin/pair field, including excluded hash counters."""
    if actual.pool != checkpoint.pool or actual.program_id != checkpoint.program_id:
        raise ValidationError('LIMIT_ORDER checkpoint pool/program mismatch')
    left, right = actual.to_dict(), checkpoint.to_dict()
    differing = [name for name in COMPARE_FIELDS
                 if left.get(name) != right.get(name)]
    if differing:
        raise ValidationError('LIMIT_ORDER checkpoint represented fields differ: ' + ','.join(differing))
    return {'equal': True, 'scope': SCOPE, 'full_raw_bytes': False,
            'represented_fields': list(COMPARE_FIELDS),
            'represented_digest': digest({name: left.get(name) for name in COMPARE_FIELDS})}


def apply_limit_order_event(
    source: PoolState, event: Event, timestamp: int, *,
    bitmap_extensions: Mapping[str, BitmapExtension] | None = None,
) -> dict:
    """Atomic typed swap evaluation; does not advance the caller's Replay object.

    The bitmap extension account address is taken ONLY from the verified
    instruction accounts, never from a guessed PDA. An explicitly absent
    extension uses the DLMM program ID sentinel. A present extension requires
    an independently supplied decoded account with its raw-data SHA256.
    """
    if event.kind != 'SWAP' or event.pool != source.pool:
        raise ValidationError('typed LO replay requires a matching pool SWAP')
    if event.payload.get('canonical_pair_verified') is not True or \
            event.payload.get('normalization_mode') != 'CANONICAL_PAIRED':
        raise Unavailable('typed LO replay requires verified canonical Swap/Swap2Evt pairing')
    if source.binary_sha256 != PROGRAM_SHA256 or event.binary_sha256 != PROGRAM_SHA256:
        raise Unavailable('typed LO replay requires recorded pinned deployed-binary evidence')
    if source.effective_function_type != FunctionType.LIMIT_ORDER:
        raise Unsupported('typed LO replay only applies to effective LIMIT_ORDER pools')
    if event.clock_unix_timestamp != timestamp or event.clock_source not in (
            'CAPTURED_SYSVAR', 'ARCHIVAL_SYSVAR', 'SVM_CAPTURE', 'SYNTHETIC_FIXTURE'):
        raise Unavailable('LO replay requires explicit matching Clock sysvar evidence')
    roles = event.payload.get('instruction_accounts')
    if not isinstance(roles, dict) or roles.get('lb_pair') != source.pool:
        raise Unavailable('canonical swap instruction lacks identified LbPair account')
    extension_address = roles.get('bin_array_bitmap_extension')
    if not isinstance(extension_address, str) or not extension_address:
        raise Unavailable('bitmap-extension instruction account absent/ambiguous')
    extension = None
    if extension_address != PROGRAM_ID:
        extension = (bitmap_extensions or {}).get(extension_address)
        if not isinstance(extension, BitmapExtension) or extension.pool != source.pool \
                or not extension.account_sha256:
            raise Unavailable('present bitmap extension requires captured raw account bytes and hash')
    intent = SwapIntent.from_event(event)
    result = simulate_swap(source, intent, timestamp, slot=event.order.slot,
                           bitmap_extension=extension, extension_known=True)
    compat = event.payload.get('compatibility_swap')
    if not isinstance(compat, dict):
        raise Unavailable('canonical Swap compatibility event is missing')
    observed = dict(event.observed)
    if 'start_bin' not in observed or 'end_bin' not in observed:
        raise Unavailable('Swap2Evt missing start/end bins')
    observed['start_bin_id'] = observed.pop('start_bin')
    observed['end_bin_id'] = observed.pop('end_bin')
    observed['swap_for_y'] = event.payload.get('swap_for_y')
    if compat.get('fee_bps') is None:
        raise Unavailable('compatibility Swap fee_bps evidence missing')
    observed['fee_bps'] = compat['fee_bps']
    if event.data_kind == 'HISTORICAL':
        # Synthetic fixtures may supply only the minimal compatibility fields.
        # Factual history must retain independently matched common fields.
        for key in ('start_bin_id', 'end_bin_id', 'amount_in', 'amount_out',
                    'swap_for_y', 'protocol_fee', 'host_fee', 'fee_bps'):
            if key not in compat or compat[key] != observed.get(key):
                raise ValidationError('historical compatibility Swap disagrees with Swap2Evt: ' + key)
    actual = result.event_amounts()
    missing = [name for name in OBSERVED_FIELDS if name not in observed or name not in actual]
    if missing:
        raise Unavailable('Swap2Evt lacks mandatory parity fields: ' + ','.join(missing))
    differences = [name for name in OBSERVED_FIELDS if observed[name] != actual[name]]
    if differences:
        raise ValidationError('historical LIMIT_ORDER Swap2Evt mismatch: ' + ','.join(differences))
    # Compatibility fee includes program-applied exact-out protocol rounding;
    # the SDK-only protocol scalar must never be used for account mutation.
    if compat.get('fee') != result.base_trading_fee + result.rounding_protocol_fee:
        raise ValidationError('compatibility Swap fee / rounding mismatch')
    if any(field.input_conservation_residual or field.output_conservation_residual
           for field in result.fills):
        raise ValidationError('LO per-bin token conservation residual')
    if len(result.quote.fills) != len(result.fills):
        raise ValidationError('LO quote/transition fill count differs')
    fills, fees = [], []
    for step, quote_step in zip(result.fills, result.quote.fills):
        row = asdict(step.quote)
        row['volatility_accumulator'] = quote_step.volatility_accumulator
        row['bin_id'] = step.bin_id
        fills.append(row)
        recipients = step.recipient_fees
        if step.mm_base_fee + step.mm_variable_fee != recipients.mm_lp_fee:
            raise ValidationError('MM fee component allocation mismatch')
        if (step.mm_base_fee + step.mm_variable_fee + recipients.limit_order_fee
                + recipients.protocol_fee + recipients.host_fee != recipients.trading_fee):
            raise ValidationError('trading fee recipients do not conserve')
        fees.append({
            'bin_id': step.bin_id, 'token': step.fee_token,
            'lp_base': step.mm_base_fee, 'lp_variable': step.mm_variable_fee,
            'lp_composition': 0,
            'lo_participant': recipients.limit_order_fee,
            'protocol': recipients.protocol_fee, 'host': recipients.host_fee,
            'trader_total': recipients.trading_fee,
            'fee_scope': 'MM_LP_FEES_ONLY_FOR_POSITION_ATTRIBUTION',
        })
        if step.rounding_protocol_fee:
            # Exact-out rounding may be in output currency, distinct from the
            # trading-fee token. Never combine differently denominated amounts.
            fees.append({'bin_id': step.bin_id, 'token': result.output_token,
                         'lp_base': 0, 'lp_variable': 0, 'lp_composition': 0,
                         'lo_participant': 0, 'protocol': step.rounding_protocol_fee,
                         'host': 0, 'trader_total': step.rounding_protocol_fee,
                         'fee_scope': 'OUTPUT_TOKEN_PROTOCOL_ROUNDING_ONLY'})
    return {'post_state': result.post_state, 'fills': fills, 'fees': fees,
            'quote': result.quote.summary(), 'event_amounts': actual,
            'transition_summary': result.summary(), 'scope': SCOPE,
            'historical_prestate_validated': False,
            'program_binary_hash': PROGRAM_SHA256,
            'unmodeled_accounts': ('Oracle', 'SPL balances', 'token account metadata',
                                    'full account bytes', 'additional pool writes')}
