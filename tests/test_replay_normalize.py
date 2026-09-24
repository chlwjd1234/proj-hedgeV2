import base64,struct
from dataclasses import asdict
import pytest
from mdlmm.model import *
from mdlmm.mechanics import quote_exact_in
from mdlmm.replay import Replay,ordered_events,compare_states,compact_trace,expand_traces
from mdlmm.codec import b58encode,discriminator
from mdlmm.normalize import normalize_block,EVENT_CPI_TAG,account_keys
from mdlmm.errors import *


def make_event(state,slot=11):
    q=quote_exact_in(state,100000,True,1001,slot=slot)
    return Event(Order(slot,0,1,2),1001000,'signature',state.pool,'SWAP',
                 program_version=state.program_version,binary_sha256=state.binary_sha256,idl_hash=state.idl_hash,
                 clock_unix_timestamp=1001,clock_source='SYNTHETIC_FIXTURE',
                 payload={'amount_in':100000,'swap_for_y':True},
                 observed={'amount_out':q.amount_out,'fee':q.fee,'protocol_fee':q.protocol_fee,
                           'start_bin':q.start_bin,'end_bin':q.end_bin},data_kind='SYNTHETIC')


def test_replay_and_compact_trace_parity(state,registry):
    e=make_event(state);engine=Replay(state,registry)
    row=engine.step(e)
    compressed=compact_trace(row)
    rebuilt=list(expand_traces([compressed],state))[0]
    assert PoolState.from_dict(rebuilt['post']).to_dict()==PoolState.from_dict(row['post']).to_dict()
    assert len(str(compressed))<len(str(row))
    compressed['post_economic_hash']='bad'
    with pytest.raises(ValidationError):list(expand_traces([compressed],state))

@pytest.mark.parametrize('field',['amount_out','fee','protocol_fee','start_bin','end_bin'])
def test_observed_parity_errors_fail(state,registry,field):
    e=make_event(state);e.observed[field]+=1
    with pytest.raises(ValidationError):Replay(state,registry).step(e)

@pytest.mark.parametrize('field',['program_version','binary_sha256','idl_hash'])
def test_unknown_version_not_current_default(state,registry,field):
    e=make_event(state);setattr(e,field,None)
    with pytest.raises(Unavailable):Replay(state,registry).step(e)


def test_snapshot_cannot_seed_past(state,registry):
    with pytest.raises(ValidationError):Replay(state,registry).step(make_event(state,10))


def test_clock_required_and_partial_explicit(state,registry):
    e=make_event(state);e.clock_unix_timestamp=None;e.clock_source=None
    with pytest.raises(Unavailable):Replay(state,registry).step(e)
    engine=Replay(state,registry,allow_partial_clock=True);engine.step(e)
    assert engine.report()['partial_clock_events']==1


def test_order_determinism_no_signature_sort(state):
    a=make_event(state);b=make_event(state,12)
    a.tx_signature='zzz';b.tx_signature='aaa'
    assert list(ordered_events([a,b]))==[a,b]
    with pytest.raises(ValidationError):list(ordered_events([b,a]))
    with pytest.raises(ValidationError):list(ordered_events([a,a]))


def make_block(pool):
    fields=b'\0'*64+struct.pack('<iiQQBQQ',0,0,100,90,1,10,1)+(1).to_bytes(16,'little')+struct.pack('<Q',0)
    # Pool bytes must match address in keys.
    from mdlmm.codec import b58decode
    fields=b58decode(pool)+fields[32:]
    event=EVENT_CPI_TAG+discriminator('event','Swap')+fields
    tx={'signatures':['zzz'],'message':{'accountKeys':[PROGRAM_ID,pool],
        'instructions':[{'programIdIndex':0,'accounts':[1],'data':b58encode(discriminator('global','swap'))}]}}
    meta={'err':None,'fee':5000,'innerInstructions':[{'index':0,'instructions':[
        {'programIdIndex':0,'accounts':[1],'data':b58encode(event)}]}],
        'logMessages':['Program data: '+base64.b64encode(event[8:]).decode()]}
    return {'blockTime':1000,'transactions':[{'transaction':tx,'meta':meta}]}


def test_actual_order_index_and_cpi_log_dedup():
    pool=b58encode(bytes(range(32)));block=make_block(pool)
    r=normalize_block(20,block,{pool},raw_hash='raw')
    assert len(r['events'])==1 and not r['blockers']
    assert r['events'][0]['order']=={'slot':20,'transaction_index':0,'instruction_index':0,'inner_instruction_index':0,'event_index':0}


def test_failed_transaction_no_state_events():
    pool=b58encode(bytes(range(32)));block=make_block(pool);block['transactions'][0]['meta']['err']={'InstructionError':[0,'x']}
    out=normalize_block(20,block,{pool},raw_hash='raw')
    assert out['events']==[] and len(out['failed_transactions'])==1


def test_missing_log_location_not_pass():
    pool=b58encode(bytes(range(32)));block=make_block(pool);block['transactions'][0]['meta']['innerInstructions']=[]
    out=normalize_block(20,block,{pool},raw_hash='raw')
    assert not out['events'] and out['blockers']


def test_version0_address_lookup_resolution():
    tx={'message':{'accountKeys':['a'],'addressTableLookups':[{}]}}
    assert account_keys(tx,{'loadedAddresses':{'writable':['b'],'readonly':['c']}})==['a','b','c']
    with pytest.raises(Unavailable):account_keys(tx,{})


def test_economic_continuity_is_not_broken_by_slot_metadata(state):
    from copy import deepcopy
    other=deepcopy(state)
    other.slot+=1;other.program_version='metadata only';other.binary_sha256='metadata only'
    assert other.economic_hash()==state.economic_hash()
    other.active_id+=1
    assert other.economic_hash()!=state.economic_hash()
