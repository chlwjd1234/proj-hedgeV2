from pathlib import Path
import json,tomllib,pytest
from mdlmm.demo import generate
from mdlmm.dataset import Dataset
from mdlmm.validation import validate_dataset
from mdlmm.pilot import pilot
from mdlmm.static import static_trace
from mdlmm.errors import ValidationError,Unavailable
from mdlmm.util import write_json,hash_file,read_jsonl,dec


@pytest.fixture(scope='module')
def integration(tmp_path_factory):
    root=tmp_path_factory.mktemp('pipeline')
    path=generate(root/'data',hours_per_event=12,reference_step_ms=300000)
    with (Path(__file__).parents[1]/'configs/synthetic.toml').open('rb') as f:cfg=tomllib.load(f)
    cfg['pilot'].update(reference_max_age_ms=600000,daily_reference_max_age_ms=600000,
                        markout_max_delay_ms=300000,capacity_samples_per_case=1)
    d=Dataset(path);d0=root/'d0';d0.mkdir()
    result=validate_dataset(d,d0)
    return root,d,cfg,d0,result


def test_full_d0_synthetic_event_accounting(integration):
    root,d,cfg,d0,result=integration
    assert result['status']=='PASS' and result['events_validated']==112
    assert result['historical_evidence'] is False
    assert len(result['checks'])==72


def test_full_d1_and_static_inventory_oracle(integration):
    root,d,cfg,d0,result=integration
    d1=root/'d1';d1.mkdir();r=pilot(d,d0,result,d1,cfg)
    assert r['cases_pass']==16
    assert r['economic_evidence'] is False
    assert r['edge_assessment']=='SYNTHETIC_NOT_ECONOMIC_EVIDENCE' and not r['d3_eligible']
    assert abs(dec(r['max_nav_residual']))<dec('1e-30')
    cases=list(read_jsonl(d1/'case_results.jsonl'))
    assert len(cases)==16 and all(x['attribution_mode']=='FIXED_EXISTING_MM_SHARES_NOT_ADDITIONAL_LIQUIDITY' for x in cases)
    d2=root/'d2';d2.mkdir();s=static_trace(d1,d2)
    assert s['status']=='PASS' and s['rows']==224 and s['cases']==16


def test_incomplete_coverage_not_pass(integration,tmp_path):
    import shutil
    root,d,cfg,d0,result=integration
    folder=tmp_path/'bad';shutil.copytree(d.root,folder)
    meta=json.loads((folder/'dataset.json').read_text());ref=meta['segments'][0]['coverage']
    coverage=json.loads((folder/ref['path']).read_text());coverage['complete']=False
    write_json(folder/ref['path'],coverage);ref['sha256']=hash_file(folder/ref['path'])
    write_json(folder/'dataset.json',meta)
    data=Dataset(folder/'dataset.json');out=tmp_path/'out';out.mkdir()
    result=validate_dataset(data,out)
    assert result['status']=='UNAVAILABLE' and result['historical_gate']!='PASS'


def test_input_tamper_detected(integration,tmp_path):
    import shutil
    _,d,_,_,_=integration
    folder=tmp_path/'tamper';shutil.copytree(d.root,folder)
    with (folder/'reference.csv').open('a') as f:f.write('tamper\n')
    with pytest.raises(ValidationError):Dataset(folder/'dataset.json')
