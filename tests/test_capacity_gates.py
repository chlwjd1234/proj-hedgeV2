from pathlib import Path
from copy import deepcopy
import pytest
from mdlmm.capacity import *
from mdlmm.model import Status,Check,aggregate,checked
from mdlmm.util import Run,write_json,read_json,hash_file,verify_run,source_fingerprint,safe_path
from mdlmm.replay import VersionRegistry
from mdlmm.regimes import explicit_windows,Window,DAY,validate_nonoverlap


@pytest.mark.parametrize('capital',[1,1000,10000,100000])
@pytest.mark.parametrize('direction',[True,False])
def test_capacity_per_bin_not_tvl(state,capital,direction):
    h=state.economic_hash()
    r=perturbation(state,100000,direction,state.variable.last_update_timestamp,1000,-1,1,capital)
    assert r['status']=='PASS'
    assert r['classification'] in ('SMALL_PRICE_TAKER','MODERATE_PERTURBATION','LARGE_COUNTERFACTUAL')
    assert 0<=r['active_bin_lp_share']<=1
    assert not r['endogenous_routing_arbitrage_validated']
    assert state.economic_hash()==h


def test_unavailable_is_never_pass():
    def bad():raise Unavailable('missing')
    assert checked('missing',bad).status==Status.UNAVAILABLE
    assert aggregate([])==Status.UNAVAILABLE
    assert aggregate([Check('unit',Status.PASS),Check('live',Status.UNAVAILABLE)])==Status.UNAVAILABLE
    assert aggregate([Check('bad',Status.FAIL),Check('missing',Status.UNAVAILABLE)])==Status.FAIL


def test_immutable_run_tamper_detection(tmp_path):
    run=Run(tmp_path,'test',{})
    run.finish({'status':'PASS','scope':'SYNTHETIC'})
    verify_run(run.path)
    with pytest.raises(FileExistsError):run.finish({'status':'PASS'})
    write_json(run.path/'result.json',{'status':'FAIL'})
    with pytest.raises(ValidationError):verify_run(run.path)


def test_fingerprint_ignores_node_modules_not_core(tmp_path):
    (tmp_path/'src').mkdir();(tmp_path/'src/a.py').write_text('x=1')
    one=source_fingerprint(tmp_path)
    p=tmp_path/'scripts/sdk/node_modules/fake';p.mkdir(parents=True);(p/'index.js').write_text('large dependency')
    assert source_fingerprint(tmp_path)==one
    (tmp_path/'src/a.py').write_text('x=2')
    assert source_fingerprint(tmp_path)!=one


def test_path_escape_rejected(tmp_path):
    with pytest.raises(ValidationError):safe_path(tmp_path,'../oops')


def test_registry_missing_binary_is_unavailable(tmp_path):
    from mdlmm.model import PROGRAM_ID
    with pytest.raises(Unavailable):
        VersionRegistry([{'start_slot':1,'end_slot':2,'program_id':PROGRAM_ID,'version':'0.12.0'}],tmp_path)


def test_windows_exact_nonoverlap():
    specs=[{'label':str(i),'start':f'2025-02-{1+i*7:02d}T00:00:00Z','end':f'2025-02-{8+i*7:02d}T00:00:00Z'} for i in range(3)]
    specs.append({'label':'3','start':'2025-02-22T00:00:00Z','end':'2025-03-01T00:00:00Z'})
    assert len(explicit_windows(specs))==4
    specs[-1]['start']='2025-02-21T00:00:00Z'
    with pytest.raises(ValidationError):explicit_windows(specs)


def test_source_change_during_run_is_a_failed_receipt(tmp_path):
    p=tmp_path/'src/a.py';p.parent.mkdir();p.write_text('v=1')
    run=Run(tmp_path,'audit',{})
    p.write_text('v=2')
    path=run.finish({'status':'PASS','economic_evidence':True,'d3_eligible':True})
    result=read_json(path/'result.json')
    assert result['status']=='FAIL'
    assert result['reason']=='SOURCE_CHANGED_DURING_RUN'
    assert not result['economic_evidence'] and not result['d3_eligible']
    verify_run(path)


def test_documented_cost_mode_needs_sourced_evidence(tmp_path):
    from mdlmm.config import cost_evidence
    with pytest.raises(Unavailable):
        cost_evidence(tmp_path,{'research':{'cost_evidence_mode':'CONSERVATIVE_DOCUMENTED_BOUND'}})


def test_automatic_regime_selection_does_not_call_an_uptrend_a_downtrend():
    from mdlmm.reference import Reference
    from mdlmm.regimes import select_windows
    ref=Reference()
    try:
        ref.insert([{'ts_ms':(day+1)*DAY-1,'available_ms':(day+1)*DAY-1,
                     'price':str(100+day),'source':'SYNTHETIC_MONOTONE','symbol':'SOL/USDC'}
                    for day in range(70)])
        with pytest.raises(Unavailable,match='regime labels'):
            select_windows(ref)
    finally:
        ref.close()


def test_fingerprint_ignores_generated_setuptools_metadata(tmp_path):
    src=tmp_path/'src';src.mkdir();(src/'module.py').write_text('VALUE=1')
    before=source_fingerprint(tmp_path)
    egg=src/'research.egg-info';egg.mkdir();(egg/'SOURCES.txt').write_text('machine specific generated paths')
    assert source_fingerprint(tmp_path)==before
