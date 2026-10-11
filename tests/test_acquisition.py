import base64,struct,json
from pathlib import Path
from copy import deepcopy
import pytest
from mdlmm.codec import *
from mdlmm.network import *
from mdlmm.history import collect_pool_history,verify_signature_coverage
from mdlmm.model import *
from mdlmm.util import write_json,read_json,hash_file,D
from mdlmm.errors import *
from mdlmm.archive import join_funding_oracles


def test_rpc_writes_prohibited():
    rpc=SolanaRPC(Http())
    for method in ('sendTransaction','requestAirdrop','signTransaction'):
        with pytest.raises(ValidationError):rpc.call(method,[])


def test_raw_content_store_hash_bound(tmp_path):
    store=RawStore(tmp_path)
    h=store.put(b'{"real":"source bytes"}',{'source':'TEST_NOT_MARKET_DATA'})
    assert store.get(h)=={'real':'source bytes'}
    assert store.put(b'{"real":"source bytes"}',{})==h
    with pytest.raises(ValidationError):store.get('../../secret')


def test_frozen_capture_handles_bitmap_absence_and_rewards(tmp_path):
    """Synthetic RPC test; it is deliberately not a mainnet validation receipt."""
    raw=bytearray(account_bytes(json.loads((Path(__file__).parent/'fixtures/official_lb_pair.json').read_text())))
    raw[88:120]=b58decode(SOL_MINT);raw[120:152]=b58decode(USDC_MINT)
    pool='FJbEo74c2W4QLBBVUfUvi8VBWXtMdJVPuFpq2f6UV1iB';slot=500
    pair={'owner':PROGRAM_ID,'data':[base64.b64encode(raw).decode(),'base64'],'lamports':1,'executable':False,'rentEpoch':0}
    array=bytearray(10136);array[:8]=discriminator('account','BinArray');array[24:56]=b58decode(pool)
    ba={'owner':PROGRAM_ID,'data':[base64.b64encode(array).decode(),'base64'],'lamports':1,'executable':False,'rentEpoch':0}
    clock={'owner':'Sysvar1111111111111111111111111111111111111','data':[base64.b64encode(struct.pack('<QqQQq',slot,0,0,0,1000)).decode(),'base64'],
           'lamports':1,'executable':False,'rentEpoch':0}
    key=find_program_address([b'bin_array',b58decode(pool),(0).to_bytes(8,'little')],PROGRAM_ID)[0]
    bitmap=find_program_address([b'bitmap',b58decode(pool)],PROGRAM_ID)[0]
    class FakeRPC:
        http=type('H',(),{'last_raw_hash':'TEST_SOURCE_NOT_CHAIN'})()
        def account(self,address):return {'value':pair}
        def program_accounts(self,filters):return {'context':{'slot':slot},'value':[{'pubkey':key,'account':ba}]}
        def call(self,method,params):
            assert method=='getMultipleAccounts'
            values=[None if x==bitmap else pair if x==pool else clock if x==CLOCK_ID else ba for x in params[0]]
            return {'context':{'slot':slot},'value':values}
    cap=capture_pool(FakeRPC(),pool)
    assert cap['slot']==500 and cap['state']['data_kind']=='CURRENT'
    assert cap['raw_accounts'][bitmap] is None and cap['bitmap_extension_address']==bitmap
    assert cap['timestamp_ms']==1000000 and len(cap['state']['bins'])==70


def test_pool_address_history_pagination_and_block_index(tmp_path):
    class FakeRPC:
        http=type('H',(),{'last_raw_hash':'synthetic'})()
        def signatures(self,address,before,limit):
            return [{'slot':20,'signature':'newer'},{'slot':12,'signature':'b'},
                    {'slot':11,'signature':'a'},{'slot':9,'signature':'older'}]
        def block(self,slot):
            sig='b' if slot==12 else 'a'
            return {'transactions':[{'transaction':{'signatures':[sig]}}]}
    report=collect_pool_history(FakeRPC(),'POOL',10,15,tmp_path,max_pages=2)
    assert report['complete'] and report['produced_slots']==[11,12]
    assert verify_signature_coverage(report,lambda x:tmp_path/x['path'])==[11,12]
    report['produced_slots']=[12]
    with pytest.raises(ValidationError):verify_signature_coverage(report,lambda x:tmp_path/x['path'])


def test_exhausted_history_before_start_is_not_complete(tmp_path):
    class FakeRPC:
        http=type('H',(),{'last_raw_hash':'synthetic'})()
        def signatures(self,*a):return []
    report=collect_pool_history(FakeRPC(),'POOL',10,15,tmp_path)
    assert report['complete'] is False


def test_funding_oracle_is_not_implicitly_spot():
    rates={'coverage_start_ms':0,'coverage_end_ms':3600000,'events':[{'time':3600000,'fundingRate':'.001'}]}
    with pytest.raises(Unavailable):join_funding_oracles(rates,[])
    with pytest.raises(ValidationError):join_funding_oracles(rates,[{'time_ms':3600000,'symbol':'SOL','kind':'SPOT','source':'spot','oracle_price':100}])
    joined=join_funding_oracles(rates,[{'time_ms':3600000,'symbol':'SOL','kind':'HYPERLIQUID_ORACLE','source':'raw','oracle_price':100}])
    assert joined['events'][0]['rate']=='0.001' and joined['zero_funding_imputed'] is False


def test_gzip_financial_artifact_determinism(tmp_path):
    from mdlmm.util import write_jsonl
    a=tmp_path/'one.jsonl.gz';b=tmp_path/'two.jsonl.gz'
    write_jsonl(a,[{'x':1}]);write_jsonl(b,[{'x':1}])
    assert a.read_bytes()==b.read_bytes()


def test_large_current_capture_avoids_getmultipleaccounts_100_key_limit_and_labels_nonatomic():
    raw=bytearray(account_bytes(json.loads((Path(__file__).parent/'fixtures/official_lb_pair.json').read_text())))
    raw[88:120]=b58decode(SOL_MINT);raw[120:152]=b58decode(USDC_MINT)
    pool='FJbEo74c2W4QLBBVUfUvi8VBWXtMdJVPuFpq2f6UV1iB'
    pair={'owner':PROGRAM_ID,'data':[base64.b64encode(raw).decode(),'base64'],'lamports':1,'executable':False,'rentEpoch':0}
    arrays=[]
    for idx in range(101):
        a=bytearray(10136);a[:8]=discriminator('account','BinArray')
        a[8:16]=idx.to_bytes(8,'little',signed=True);a[24:56]=b58decode(pool)
        key=b58encode((idx+1).to_bytes(32,'little'))
        arrays.append({'pubkey':key,'account':{'owner':PROGRAM_ID,'data':[base64.b64encode(a).decode(),'base64'],
                                                'lamports':1,'executable':False,'rentEpoch':0}})
    bitmap=find_program_address([b'bitmap',b58decode(pool)],PROGRAM_ID)[0]
    clock={'owner':'Sysvar1111111111111111111111111111111111111',
           'data':[base64.b64encode(struct.pack('<QqQQq',501,0,0,0,1000)).decode(),'base64'],
           'lamports':1,'executable':False,'rentEpoch':0}
    class RPC:
        http=type('H',(),{'last_raw_hash':'synthetic'})()
        def account(self,address):return {'value':pair}
        def program_accounts(self,filters):return {'context':{'slot':500},'value':arrays}
        def multiple_accounts(self,keys,min_context_slot=None):
            assert len(keys)<100 and min_context_slot==500
            values=[None if x==bitmap else pair if x==pool else clock if x==CLOCK_ID else pair for x in keys]
            return {'context':{'slot':501},'value':values}
    cap=capture_pool(RPC(),pool)
    assert cap['atomic_current_snapshot'] is False
    assert cap['historical_seed_allowed'] is False
    assert cap['context_slot_span']==1
    assert cap['source_kind']=='CURRENT_RPC_NONATOMIC_VALIDATION_VECTOR'
    assert len(cap['state']['bins'])==101*70
