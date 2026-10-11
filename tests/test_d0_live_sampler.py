from mdlmm.model import PROGRAM_ID
from mdlmm.validation import (_transaction_dlmm_invocations,_activity_probe,
                              _scan_pool_for_dlmm,_select_live_pools)
from mdlmm.codec import b58encode

POOL='Pool111111111111111111111111111111111111111'
OTHER='Other11111111111111111111111111111111111111'

def tx_result(signature,slot,pool=POOL,invoke=True,failed=False):
    keys=[pool,PROGRAM_ID,OTHER]
    program_index=1 if invoke else 2
    return {'slot':slot,'transaction':{'signatures':[signature],'message':{'accountKeys':keys,'instructions':[
        {'programIdIndex':program_index,'accounts':[0],'data':b58encode(b'12345678')}]}},
        'meta':{'err':{'x':1} if failed else None,'innerInstructions':[],'loadedAddresses':{'writable':[],'readonly':[]}}}

def test_transaction_filter_requires_real_dlmm_invocation_and_pool_account():
    assert _transaction_dlmm_invocations(tx_result('a',1),POOL)==1
    assert _transaction_dlmm_invocations(tx_result('a',1,invoke=False),POOL)==0
    assert _transaction_dlmm_invocations(tx_result('a',1,failed=True),POOL)==0
    assert _transaction_dlmm_invocations(tx_result('a',1),OTHER)==0

def test_activity_probe_counts_success_and_failure():
    class RPC:
        def signatures(self,pool,limit):
            return [{'slot':3,'signature':'a','err':None},{'slot':2,'signature':'b','err':{'x':1}}]
    x=_activity_probe(RPC(),POOL,20)
    assert x['signature_rows']==2 and x['successful_signatures']==1 and x['failed_signatures']==1
    assert x['latest_success_slot']==3

def test_scan_pool_skips_address_touch_only_and_keeps_actual_dlmm_slots():
    rows=[{'slot':5,'signature':'u','err':None},{'slot':4,'signature':'d1','err':None},
          {'slot':3,'signature':'bad','err':{'x':1}},{'slot':2,'signature':'d2','err':None}]
    class RPC:
        def signatures(self,pool,before=None,limit=25):
            return rows if before is None else []
        def transaction(self,sig):
            return tx_result(sig,{'u':5,'d1':4,'d2':2}[sig],invoke=sig!='u')
    x=_scan_pool_for_dlmm(RPC(),POOL,{'sample_blocks_per_pool':2,'sample_signature_page_size':25,'sample_signature_max_pages':2})
    assert x['target_reached'] and x['selected_unique_blocks']==2
    assert x['selected_slots']==[4,2]
    assert x['successful_signatures']==3 and x['failed_signatures']==1
    assert x['dlmm_invoking_transactions']==2 and x['dlmm_instruction_count']==2

def test_selection_does_not_use_address_order_when_activity_and_invocation_disagree(monkeypatch):
    discovery={'pools':[{'pool':'A','status':0},{'pool':'B','status':0},{'pool':'C','status':0}]}
    activity={'A':(0,-1),'B':(20,20),'C':(10,10)}
    scans={
        'B':{'target_reached':False,'minimum_reached':False,'selected_unique_blocks':0},
        'C':{'target_reached':True,'minimum_reached':True,'selected_unique_blocks':1},
    }
    monkeypatch.setattr('mdlmm.validation._activity_probe',lambda rpc,pool,limit:{
        'pool':pool,'signature_rows':activity[pool][0],'successful_signatures':activity[pool][0],
        'failed_signatures':0,'latest_success_slot':activity[pool][1]})
    monkeypatch.setattr('mdlmm.validation.rank_current_api_pools',lambda *a,**k: (_ for _ in ()).throw(__import__('mdlmm.errors',fromlist=['Unavailable']).Unavailable('offline')))
    monkeypatch.setattr('mdlmm.validation._scan_pool_for_dlmm',lambda rpc,pool,cfg:scans[pool])
    RPC=type('RPC',(),{'http':object})
    out=_select_live_pools(RPC(),discovery,{'sample_pool_count':1,'candidate_pool_scan_limit':2,
                           'pool_activity_signature_limit':20,'sample_blocks_per_pool':1},[])
    assert out['candidate_pools']==['B','C']
    assert out['selected_pools']==['C'] and out['selection_complete']
