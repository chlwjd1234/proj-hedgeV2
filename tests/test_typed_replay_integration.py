"""Typed Replay integration gates, independent of historical account provenance.

The existing 91 SBF vectors separately test the exact fee/mutation formulas;
these tests guard integration, event binding, fee semantics and failure atomicity.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from mdlmm.codec import discriminator
from mdlmm.errors import Unavailable, Unsupported, ValidationError
from mdlmm.mechanics import Q64
from mdlmm.model import (Bin, BIN_LIMIT_ORDER_FIELDS, Event, Order,
                         FeeParameters, PoolState, VariableState, PROGRAM_ID)
from mdlmm.replay import Replay, VersionRegistry, compact_trace, expand_traces
from mdlmm.lo_replay_adapter import compare_represented_lo_states
from mdlmm.swap_quote import SwapIntent
from mdlmm.swap_transition import PROGRAM_SHA256, simulate_swap

POOL = 'So11111111111111111111111111111111111111112'
IDL_SHA = '8' * 64


def source():
    zeros = {k: 0 for k in BIN_LIMIT_ORDER_FIELDS}
    bins = {i: Bin(i,0,0,0,0,**zeros) for i in range(70)}
    bins[0] = Bin(0,1_000_000,1_000_000,Q64,1_000_000*Q64,**zeros)
    bitmap=[0]*16
    bitmap[8]=1
    return PoolState(POOL,0,10,
                     FeeParameters(10000,30,600,5000,0,350000,2000,
                                   function_type=2,collect_fee_mode=0),
                     VariableState(0,0,0,1000),bins,slot=10,
                     program_version='0.12.0',binary_sha256=PROGRAM_SHA256,
                     idl_hash=IDL_SHA,data_kind='SYNTHETIC',
                     known_bin_arrays=[0],bin_array_bitmap=bitmap,
                     protocol_fee_x=0,protocol_fee_y=0,last_updated_at=0)


def registry(*,model='LO_V012',binary=PROGRAM_SHA256):
    row={'start_slot':0,'end_slot':100,'program_id':PROGRAM_ID,
         'provenance':'SYNTHETIC_FIXTURE','model':model,'version':'0.12.0',
         'binary_sha256':binary,'idl_sha256':IDL_SHA}
    return VersionRegistry([row],Path('.'),synthetic=True)


def event(state, *, amount_in=100000, host=True):
    intent=SwapIntent.exact_in(amount_in,True,host_fee_present=host)
    modeled=simulate_swap(state,intent,1001,slot=11,extension_known=True)
    scalars=modeled.event_amounts()
    observed={('start_bin' if k=='start_bin_id' else 'end_bin' if k=='end_bin_id' else k):v
              for k,v in scalars.items() if k not in ('swap_for_y','fee_bps')}
    e=Event(Order(11,0,0,3),1001000,'fixture_signature',state.pool,'SWAP',
            program_version='0.12.0',binary_sha256=PROGRAM_SHA256,idl_hash=IDL_SHA,
            clock_unix_timestamp=1001,clock_source='SYNTHETIC_FIXTURE',
            data_kind='SYNTHETIC',observed=observed,
            payload={'canonical_pair_verified':True,'normalization_mode':'CANONICAL_PAIRED',
                     'instruction_name':'swap','instruction_args':{'amount_in':amount_in,'min_amount_out':0},
                     'exact_out':False,'swap_for_y':True,'host_fee_present':host,
                     'instruction_accounts':{'lb_pair':state.pool,'bin_array_bitmap_extension':PROGRAM_ID},
                     'compatibility_swap':{'fee_bps':modeled.fee_bps,
                                           'fee':modeled.base_trading_fee+modeled.rounding_protocol_fee}})
    return e,modeled


def test_lo_registered_replay_carries_program_parity_and_fee_recipient_partition():
    state=source()
    e,expected=event(state)
    replay=Replay(state,registry())
    row=replay.step(e)
    assert replay.state.to_dict()==expected.post_state.to_dict()
    assert replay.report()['mechanics_scope'].startswith('LO_V012_')
    assert replay.report()['full_program_byte_parity']=='UNAVAILABLE'
    assert replay.report()['typed_swap_count']==1
    assert row['state_continuity']=='PASS'
    assert row['swap_evidence_scope'].startswith('LIMIT_ORDER_')
    assert sum(f['lp_base']+f['lp_variable'] for f in row['fees'])==expected.mm_fee
    assert sum(f['lo_participant'] for f in row['fees'])==expected.limit_order_fee
    assert sum(f['host'] for f in row['fees'])==expected.host_fee
    assert sum(f['protocol'] for f in row['fees'])==expected.protocol_fee
    before=source()
    compressed=compact_trace(row)
    restored=list(expand_traces([compressed],before))[0]
    assert PoolState.from_dict(restored['post']).to_dict()==expected.post_state.to_dict()


@pytest.mark.parametrize('field',('amount_in','amount_out','mm_fee','host_fee'))
def test_bad_observed_fee_or_amount_never_advances_state(field):
    state=source();e,_=event(state)
    e.observed[field]+=1
    engine=Replay(state,registry())
    before=deepcopy(engine.state.to_dict())
    with pytest.raises(ValidationError,match='Swap2Evt'):
        engine.step(e)
    assert engine.state.to_dict()==before
    assert engine.event_count==0


def test_missing_canonical_input_does_not_enable_typed_swap():
    state=source();e,_=event(state)
    e.payload['canonical_pair_verified']=False
    with pytest.raises(Unavailable,match='canonical'):
        Replay(state,registry()).step(e)


def test_wrong_program_model_or_missing_bitmap_witness_fails_closed():
    state=source();e,_=event(state)
    with pytest.raises(Unsupported, match='binary'):
        registry(binary='0'*64).resolve(11)
    e.payload['instruction_accounts']['bin_array_bitmap_extension']='not_the_program_sentinel'
    with pytest.raises(Unavailable,match='bitmap'):
        Replay(state,registry()).step(e)


def test_full_represented_checkpoint_catches_fee_growth_and_processed_lo_fields():
    a=source();b=deepcopy(a)
    assert compare_represented_lo_states(a,b)['equal']
    b.bins[0].fee_x_stored+=1
    with pytest.raises(ValidationError,match='bins'):
        compare_represented_lo_states(a,b)
    b=deepcopy(a);b.bins[0].processed_order_remaining_amount+=1
    with pytest.raises(ValidationError,match='bins'):
        compare_represented_lo_states(a,b)


def test_exactout_is_not_inferred_from_consumed_amount():
    state=source(); e,_=event(state)
    e.payload['instruction_name']='swap_exact_out'
    e.payload['exact_out']=True
    # Bounded exact-out uses actual out_amount/max_in_amount; never synthesize
    # from an observed exact-in receipt.
    with pytest.raises((Unavailable,ValidationError)):
        Replay(state,registry()).step(e)
