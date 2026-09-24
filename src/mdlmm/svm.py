"""Optional deployed-binary black-box harness, entirely LOCAL to LiteSVM.

A replay case supplies all pre-state accounts, raw transaction, programs, Clock,
expected post accounts and binary provenance. No live data substitution, airdrop,
new signatures or transaction submission to any network is performed.
"""
from __future__ import annotations
import base64
from pathlib import Path
from .errors import Unavailable, ValidationError
from .util import read_json, hash_file, safe_path, sha256


def replay_case(case_path:Path)->dict:
    try:
        from solders.litesvm import LiteSVM
        from solders.pubkey import Pubkey
        from solders.account import Account
        from solders.transaction import VersionedTransaction
        from solders.transaction_metadata import FailedTransactionMetadata
        import solders
    except ImportError as exc:
        raise Unavailable('optional solders/LiteSVM dependency not installed') from exc
    case=read_json(case_path)
    root=case_path.parent
    required=('programs','pre_accounts','post_accounts','clock','transaction_base64','provenance')
    if any(k not in case for k in required):
        raise Unavailable('SVM case lacks complete explicit replay inputs')
    if case['provenance'].get('data_kind') not in ('HISTORICAL','SYNTHETIC'):
        raise ValidationError('SVM case data kind unresolved')
    if not case['programs'] or not case['post_accounts']:
        raise Unavailable('empty SVM comparison is not a validation pass')
    svm=LiteSVM().with_sigverify(False).with_blockhash_check(False)
    # Signature validation is deliberately disabled for archival message replay;
    # this is not evidence about historical signature verification/runtime flags.
    program_hashes={}
    for program in case['programs']:
        path=safe_path(root,program['file'])
        if not path.exists():
            raise Unavailable(f'deployed binary missing: {program["file"]}')
        if hash_file(path)!=program['sha256']:
            raise ValidationError('SVM program binary hash mismatch')
        if case['provenance']['data_kind']=='HISTORICAL' and not program.get('historical_binding_evidence'):
            raise Unavailable('current binary cannot stand in for historical deployed binary')
        svm.add_program_from_file(Pubkey.from_string(program['program_id']),path)
        program_hashes[program['program_id']]=program['sha256']
    for address,raw in case['pre_accounts'].items():
        d=base64.b64decode(raw['data_base64'],validate=True)
        if raw.get('data_sha256') and sha256(d)!=raw['data_sha256']:
            raise ValidationError('SVM pre-account hash mismatch')
        svm.set_account(Pubkey.from_string(address),Account(raw['lamports'],d,Pubkey.from_string(raw['owner']),
                                                           raw.get('executable',False),raw.get('rent_epoch',0)))
    clock=svm.get_clock()
    for key in ('slot','epoch_start_timestamp','epoch','leader_schedule_epoch','unix_timestamp'):
        if key not in case['clock']:
            raise Unavailable('incomplete historical Clock')
        setattr(clock,key,case['clock'][key])
    svm.set_clock(clock)
    tx=VersionedTransaction.from_bytes(base64.b64decode(case['transaction_base64'],validate=True))
    result=svm.send_transaction(tx)  # local in-memory virtual machine ONLY
    if isinstance(result,FailedTransactionMetadata):
        raise ValidationError(f'local SVM transaction failed: {result}')
    matched=[]
    for address,expected in case['post_accounts'].items():
        actual=svm.get_account(Pubkey.from_string(address))
        if expected is None:
            if actual is not None and (actual.lamports or actual.data):
                raise ValidationError('expected closed SVM account still populated')
        else:
            if actual is None:
                raise ValidationError('expected post account missing')
            expected_bytes=base64.b64decode(expected['data_base64'],validate=True)
            if bytes(actual.data)!=expected_bytes or actual.lamports!=expected['lamports'] or str(actual.owner)!=expected['owner']:
                raise ValidationError(f'SVM post-account mismatch: {address}')
        matched.append(address)
    # Even matching accounts do not prove feature-set/builtin equivalence. EXACT
    # is intentionally not auto-promoted from a successful test invocation.
    return {'status':'PASS','validation_level':'ACCOUNT_PARITY','scope':'FULL_BYTES_OF_LISTED_POST_ACCOUNTS',
            'accounts':matched,'program_hashes':program_hashes,'solders_version':getattr(solders,'__version__','unknown'),
            'historical_runtime_feature_parity':'UNVERIFIED','signature_verification_disabled':True,
            'full_historical_exactness':False,'data_kind':case['provenance']['data_kind']}
