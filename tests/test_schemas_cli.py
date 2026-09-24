from pathlib import Path
from dataclasses import asdict
import json,subprocess,sys
import pytest
import jsonschema
from mdlmm.model import Event,Order
from mdlmm.util import Run,read_json

ROOT=Path(__file__).parents[1]

@pytest.mark.parametrize('path',sorted((ROOT/'schemas').glob('*.schema.json')))
def test_json_schemas_are_well_formed(path):
    jsonschema.Draft202012Validator.check_schema(read_json(path))

def test_normalized_event_matches_machine_schema():
    e=Event(Order(42,1,2,3),1000,'SYNTHETIC_SIG','SYNTHETIC_POOL','SWAP',data_kind='SYNTHETIC')
    jsonschema.validate(asdict(e),read_json(ROOT/'schemas/event.schema.json'))

def test_manifest_matches_machine_schema(tmp_path):
    r=Run(tmp_path,'test',{});r.finish({'status':'PASS'})
    jsonschema.validate(read_json(r.path/'manifest.json'),read_json(ROOT/'schemas/manifest.schema.json'))

def test_cli_preflight_and_extractor_have_structured_receipts():
    p=subprocess.run([str(ROOT/'run_pipeline.sh'),'preflight'],cwd=ROOT,capture_output=True,text=True)
    assert p.returncode==0 and 'STATUS="PASS"' in p.stdout
    out=subprocess.run([str(ROOT/'scripts/extract_preflight.sh')],cwd=ROOT,capture_output=True,text=True)
    assert out.returncode==0 and 'HISTORICAL_GATE="UNAVAILABLE"' in out.stdout

def test_cli_missing_historical_bundle_fails_closed(tmp_path):
    p=subprocess.run([str(ROOT/'run_pipeline.sh'),'d1','--dataset',str(tmp_path/'absent.json')],cwd=ROOT,capture_output=True,text=True)
    assert p.returncode==3 and 'STATUS="UNAVAILABLE"' in p.stdout
    assert 'ECONOMIC_EVIDENCE=false' in p.stdout
