from __future__ import annotations
from dataclasses import replace
import pytest
from mdlmm.model import *
from mdlmm.mechanics import Q64,price_from_id
from mdlmm.util import sha256

@pytest.fixture
def state():
    p=FeeParameters(1000,30,600,5000,40000,350000,1000,min_bin_id=-100,max_bin_id=100)
    bins={i:Bin(i,1_000_000_000 if i>=0 else 0,1_000_000_000 if i<=0 else 0,
                price_from_id(i,10),10**24) for i in range(-3,4)}
    return PoolState('TEST_POOL',0,10,p,VariableState(0,0,0,1000),bins,
                     slot=10,program_version='0.12.0',binary_sha256=sha256(b'fixture binary'),
                     idl_hash=sha256(b'fixture idl'),complete_bin_map=True,data_kind='SYNTHETIC')

@pytest.fixture
def registry(tmp_path,state):
    from mdlmm.replay import VersionRegistry
    return VersionRegistry([{'start_slot':0,'end_slot':100000,'program_id':PROGRAM_ID,
         'version':state.program_version,'binary_sha256':state.binary_sha256,'idl_sha256':state.idl_hash,
         'model':'MM_V012','provenance':'SYNTHETIC_FIXTURE'}],tmp_path,synthetic=True)
