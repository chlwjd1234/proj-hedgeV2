"""Frozen raw-program oracle tests; normal pytest does not require solders."""
import base64
from copy import deepcopy
from dataclasses import replace
import gzip
import hashlib
import importlib.metadata
import json
from pathlib import Path

import pytest

from mdlmm import program_parity as parity
from mdlmm.errors import Unavailable, ValidationError

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / 'tests/fixtures/dlmm_program_cases_v1.json.gz'
PROGRAM = ROOT / 'tests/fixtures/dlmm_d296_program.so.gz'
with gzip.open(CASES, 'rt') as stream:
    # Retain IDs only during collection. All verification loads and hashes the
    # corpus afresh through the production loader in the module fixture.
    MODEL_CASE_IDS = tuple(case['case_id'] for case in json.load(stream)['cases']
                           if case['execution_status'] == 'EXECUTED'
                           and case['instruction']['name'] in parity.SUPPORTED_SWAPS)


@pytest.fixture(scope='module')
def bundle():
    return parity.load_bundle(CASES, PROGRAM)


def _case(bundle, case_id):
    return next(case for case in bundle.cases if case['case_id'] == case_id)


def _change_post_account(case, label, offset, replacement):
    address = next(address for address, name in case['account_labels'].items() if name == label)
    account = case['post_accounts'][address]
    raw = bytearray(base64.b64decode(account['data_base64']))
    raw[offset:offset + len(replacement)] = replacement
    account['data_base64'] = base64.b64encode(raw).decode()
    account['data_sha256'] = hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('case_id', MODEL_CASE_IDS)
def test_successful_four_variant_original_account_and_event_parity(bundle, case_id):
    report = parity.check_model_case(bundle, _case(bundle, case_id))
    assert report['status'] == 'PASS' and report['modeled'] is True
    assert report['checked_bin_count'] == 140
    assert report['checked_token_account_count'] == 6
    assert report['raw_original_mints'] != report['synthetic_model_mint_labels']
    assert report['raw_execution_bytes_modified'] is False
    assert report['historical_gate'] == 'UNAVAILABLE'
    assert report['full_historical_exactness'] is False


def test_corpus_integrity_and_scope_counts(bundle):
    assert len(bundle.cases) == 141
    assert len(MODEL_CASE_IDS) == 91
    assert sum(case['execution_status'] == 'EXECUTED' for case in bundle.cases) == 120
    assert sum(case['execution_status'] == 'ERROR' for case in bundle.cases) == 21
    assert bundle.case_json_sha256 == parity.CASE_JSON_SHA256
    assert hashlib.sha256(bundle.program_bytes).hexdigest() == parity.PROGRAM_SHA256
    assert len(bundle.case_sources) == 141


def test_failed_and_unsupported_instructions_are_explicitly_not_modeled(bundle):
    results = [parity.check_model_case(bundle, case) for case in bundle.cases
               if case['case_id'] not in MODEL_CASE_IDS]
    assert len(results) == 50
    assert all(row['status'] == 'UNAVAILABLE' and row['modeled'] is False for row in results)
    assert sum(row['instruction_name'] == 'place_limit_order' for row in results) == 13
    assert sum('price_impact' in row['instruction_name'] for row in results) == 25
    wrong_host = parity.check_model_case(bundle, _case(bundle, 'reject_wrong_host_mint'))
    assert 'failed program case' in wrong_host['reason']


def test_model_scope_reports_oracle_and_runtime_writes(bundle):
    report = parity.check_model_case(bundle, _case(bundle, 'smoke_x_y_exact_in'))
    excluded = {item['label']: item for item in report['unmodeled_accounts']}
    assert excluded['oracle']['account_changed']
    assert 'outside PoolState' in excluded['oracle']['reason']
    assert excluded['user']['account_changed']
    assert report['scope'] == parity.MODEL_SCOPE


@pytest.mark.parametrize('label,offset,replacement,match', [
    ('bin_array_2', 56 + 96, bytes(16), 'fee_y_stored'),
    ('bin_array_2', 56 + 141, b'\x01', 'BinArray.*raw post-account mismatch'),
    ('lb_pair', 39, b'\x01', 'LbPair.*raw post-account mismatch'),
    ('user_y', 108, b'\x00', 'SPL token account.*raw post-account mismatch'),
    ('user_y', 64, bytes(8), 'SPL token account.*raw post-account mismatch'),
])
def test_original_raw_post_bytes_are_an_independent_oracle(bundle, label, offset, replacement, match):
    case = deepcopy(_case(bundle, 'smoke_x_y_exact_in'))
    _change_post_account(case, label, offset, replacement)
    with pytest.raises(ValidationError, match=match):
        parity.check_model_case(bundle, case)


def test_input_fee_exactout_sdk_protocol_scalar_cannot_replace_actual_counter(bundle):
    original = _case(bundle, 'dust_exactout_m0_out1')
    report = parity.check_model_case(bundle, original)
    assert report['sdk_protocol_fee_scalar'] == 9
    assert report['protocol_fee'] == report['rounding_protocol_fee'] == 0
    assert report['unallocated_output_surplus'] == 9
    wrong = deepcopy(original)
    _change_post_account(wrong, 'lb_pair', 216, (9).to_bytes(8, 'little'))
    with pytest.raises(ValidationError, match='LbPair.*raw post-account mismatch'):
        parity.check_model_case(bundle, wrong)


def test_raw_cpi_economics_cannot_be_replaced_by_sdk_fee_scalar(bundle):
    case = deepcopy(_case(bundle, 'dust_exactout_m0_out1'))
    rich = next(event for event in case['events'] if event['name'] == 'Swap2Evt')
    # 8-byte CPI tag + discriminator + preceding Swap2Evt Borsh fields.
    raw = bytearray(base64.b64decode(rich['raw_cpi_data_base64']))
    raw[137:145] = (9).to_bytes(8, 'little')
    rich['raw_cpi_data_base64'] = base64.b64encode(raw).decode()
    rich['data']['protocol_fee'] = 9
    with pytest.raises(ValidationError, match='Swap2Evt protocol_fee'):
        parity.check_model_case(bundle, case)


def test_decoded_event_metadata_must_agree_with_original_cpi(bundle):
    case = deepcopy(_case(bundle, 'dust_exactout_m0_out1'))
    next(event for event in case['events'] if event['name'] == 'Swap2Evt')['data']['protocol_fee'] = 9
    with pytest.raises(ValidationError, match='metadata contradicts.*CPI bytes'):
        parity.check_model_case(bundle, case)


def test_recorded_accounts_cannot_override_original_transaction(bundle):
    case = deepcopy(_case(bundle, 'smoke_x_y_exact_in'))
    case['instruction']['accounts'][9]['pubkey'] = case['instruction']['accounts'][4]['pubkey']
    with pytest.raises(ValidationError, match='original transaction bytes'):
        parity.check_model_case(bundle, case)


def test_actual_constraints_come_from_transaction_not_amount_annotation(bundle):
    case = deepcopy(_case(bundle, 'smoke_x_y_exact_in'))
    case['instruction']['amount'] = 1
    report = parity.check_model_case(bundle, case)
    assert report['actual_intent']['amount_in'] == '8000000000'
    assert report['amount_in'] == 8_000_000_000


def test_capture_decoded_snapshots_do_not_supply_expected_state(bundle):
    case = deepcopy(_case(bundle, 'smoke_x_y_exact_in'))
    case['decoded_pre'] = {'fabricated': True}
    case['decoded_post'] = {'fabricated': True}
    case['changes'] = []
    assert parity.check_model_case(bundle, case)['status'] == 'PASS'


def test_original_event_cpi_tag_is_required(bundle):
    case = deepcopy(_case(bundle, 'smoke_x_y_exact_in'))
    event = case['events'][0]
    raw = bytearray(base64.b64decode(event['raw_cpi_data_base64']))
    raw[0] ^= 1
    event['raw_cpi_data_base64'] = base64.b64encode(raw).decode()
    with pytest.raises(ValidationError, match='event-CPI tag'):
        parity.check_model_case(bundle, case)


def test_bundle_and_program_integrity_are_required(tmp_path):
    bad_cases = tmp_path / 'cases.json'
    bad_cases.write_text('{"schema":"MDLMM_PROGRAM_CASES_V1"}')
    with pytest.raises(ValidationError, match='pinned v1 JSON hash'):
        parity.load_bundle(bad_cases, PROGRAM)
    bad_program = tmp_path / 'program.so'
    bad_program.write_bytes(b'not the program')
    with pytest.raises(ValidationError, match='d296 binary binding'):
        parity.load_bundle(CASES, bad_program)


@pytest.mark.parametrize('installed_version', [None, '0.26.0'])
def test_explicit_svm_requirement_never_silently_skips(monkeypatch, installed_version):
    def version(name):
        if installed_version is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return installed_version
    monkeypatch.setattr(parity.importlib.metadata, 'version', version)
    with pytest.raises(Unavailable, match='solders==0.27.1'):
        parity._require_solders()


def test_aggregate_counts_exclude_not_modeled_and_failed_cases(bundle):
    selected = replace(bundle, cases=tuple(_case(bundle, name) for name in (
        'smoke_x_y_exact_in', 'reject_wrong_host_mint', 'xy_new_open_real_place')))
    report = parity.verify_bundle(selected)
    assert report['status'] == report['program_vector_status'] == 'PASS'
    assert report['model_case_count'] == 1
    assert len(report['not_modeled_cases']) == 2
    assert report['svm_reexecution_count'] == 0
    assert report['svm_reexecution_status'] == 'NOT_REQUESTED'
    assert report['historical_gate'] == 'UNAVAILABLE'
    assert report['full_historical_exactness'] is False


def test_cli_model_verification_reports_frozen_scope(capsys):
    assert parity.main(['--cases', str(CASES), '--program', str(PROGRAM)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['model_case_count'] == 91 and report['case_count'] == 141
    assert report['errors'] == []
    assert report['program_vector_status'] == 'PASS'
    assert report['svm_reexecution_status'] == 'NOT_REQUESTED'
    assert report['full_historical_exactness'] is False
