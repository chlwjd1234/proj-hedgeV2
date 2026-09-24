import base64,struct
from pathlib import Path
import pytest
from mdlmm.codec import *
from mdlmm.util import read_json,git_blob_hash,hash_file
from mdlmm.errors import *

@pytest.mark.parametrize('raw',[b'',b'\0',b'\0'*32,bytes(range(32)),b'\0\0abc',bytes(range(256))])
def test_base58_roundtrip(raw):
    assert b58decode(b58encode(raw))==raw

@pytest.mark.parametrize('text',['0','O','I','l','!'])
def test_base58_invalid(text):
    with pytest.raises(ValidationError):b58decode(text)

def test_official_real_account_bytes_and_hash():
    path=Path(__file__).parent/'fixtures/official_lb_pair.json'
    meta=read_json(path.with_name('official_lb_pair_provenance.json'))
    assert git_blob_hash(path.read_bytes())==meta['git_blob_sha1']
    x=decode_lb_pair(account_bytes(read_json(path)))
    assert x['active_id']==0 and x['bin_step']==10
    assert x['parameters']['base_factor']==10000
    assert x['parameters']['base_fee_power_factor']==2
    assert x['parameters']['collect_fee_mode']==0
    assert x['token_y_mint']=='So11111111111111111111111111111111111111112'
    # This official fixture is NOT SOL/USDC; do not mislabel it as a live pilot.
    assert x['token_x_mint']=='FQuMNJ6UcrkA3xMLsmmLL8sk47RkZYod57mX3a8w1brg'
    with pytest.raises(ValidationError):
        build_pool_state('fixture',x,[],slot=0)

def test_byte_length_and_owner_fail_closed():
    with pytest.raises(Unsupported):decode_lb_pair(b'\0'*904)
    with pytest.raises(ValidationError):account_bytes({'owner':'bad','data':['','base64']})

def test_bin_array_negative_index_layout():
    raw=bytearray(discriminator('account','BinArray')+(-2).to_bytes(8,'little',signed=True)+b'\1'+b'\0'*7+b'\0'*32+b'\0'*(70*144))
    offset=56
    raw[offset:offset+8]=(123).to_bytes(8,'little')
    raw[offset+16:offset+32]=(1<<64).to_bytes(16,'little')
    raw[offset+112:offset+120]=(55).to_bytes(8,'little')
    data=decode_bin_array(bytes(raw))
    assert data['index']==-2 and data['bins'][0]['bin_id']==-140
    assert data['bins'][0]['amount_x']==123 and data['bins'][0]['price']==1<<64
    assert data['bins'][0]['open_order_amount']==55
    assert data['bins'][-1]['bin_id']==-71

def test_clock_exact_layout():
    raw=struct.pack('<QqQQq',42,10,2,3,999)
    assert decode_clock(raw)['unix_timestamp']==999
    with pytest.raises(ValidationError):decode_clock(raw+b'0')

def test_idl_generic_bounds_and_borsh():
    idl={'address':PROGRAM_ID,'events':[{'name':'Example','discriminator':list(discriminator('event','Example'))}],
         'types':[{'name':'Example','type':{'kind':'struct','fields':[
             {'name':'a','type':'u64'},{'name':'b','type':{'array':['i32',2]}},
             {'name':'c','type':{'option':'bool'}}]}}]}
    c=IdlCodec(idl)
    raw=discriminator('event','Example')+struct.pack('<QiiBB',123,-4,5,1,1)
    assert c.decode_named('events',raw)['data']=={'a':123,'b':[-4,5],'c':True}
    with pytest.raises(Unsupported):c.decode_named('events',raw+b'\0')
    with pytest.raises(ValidationError):c.value(Reader(b'\xff'*4),{'vec':'u8'})
    with pytest.raises(ValidationError):scalar_decode(Reader(b'\2'),'bool')

def test_event_decode_unknown_retained_as_unsupported():
    with pytest.raises(Unsupported):decode_event(b'notknown')


def test_pda_matches_published_official_artifact_addresses():
    from mdlmm.codec import find_program_address,_edwards_decompressible
    from mdlmm.model import PROGRAM_ID
    pool=b58decode('FJbEo74c2W4QLBBVUfUvi8VBWXtMdJVPuFpq2f6UV1iB')
    # Addresses come from the pinned official artifact directory (not target SOL/USDC).
    expected={-1:'Gai2jqUAaJv8S68ibe69beV5DkhdBxAc6zoV3YQpuSNX',0:'ERuqwiEFbQwNM8NhUMxUsvgGWbQpEyjbzhQLbBftCMPd',2:'6ipV2Sn7GCVgKG3Doqz1MpM33hranTMqxDmShj8Ro8cx'}
    for i,address in expected.items():
        actual,bump=find_program_address([b'bin_array',pool,i.to_bytes(8,'little',signed=True)],PROGRAM_ID)
        assert actual==address and not _edwards_decompressible(b58decode(actual))
    assert _edwards_decompressible(bytes([1])+bytes(31)) # identity is on curve too
