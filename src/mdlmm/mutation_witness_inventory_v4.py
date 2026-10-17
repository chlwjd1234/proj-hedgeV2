"""Reconcile V3 immutable historical block-boundary witness inventory with V4 scope scan.

No RPC calls. This never upgrades 24 sampled blocks to instruction-level replay
parity. Generates a typed evidence-gap queue and per-mutation proof status.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import subprocess

V3_HASH='8143722621547895c098c636c19b7e4ca93cb4249739c95caf051bb00f0720df'
ROOT_COMMIT='9e89c64d3a553790d43ee39e8e819b7da49ecd5e'
STATE_ROLES={'lb_pair','bin_array','bin_array_lower','bin_array_upper',
             'bin_array_bitmap_extension','position','limit_order'}
IGNORED_SENTINELS={'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'}


def require(test,message):
    if not test: raise ValueError(message)


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for piece in iter(lambda:stream.read(2**20),b''):h.update(piece)
    return h.hexdigest()


def capture_verify(root,slot,address):
    path=root/'accounts'/str(slot)/f'{address}.json'
    require(path.is_file(),f'missing V3 account capture {slot} {address}')
    rec=load(path)
    require(rec['requested_slot']==slot and rec['context_slot']==slot and
            rec['pubkey']==address, 'archived account/captured slot mismatch')
    a=rec.get('account')
    if a is not None:
        raw=base64.b64decode(a['data_base64'],validate=True)
        require(hashlib.sha256(raw).hexdigest()==a['data_sha256'],
                f'archive account checksum mismatch: {slot} {address}')
        require(len(raw)==a['space'], 'archive account data-size mismatch')
    return rec


def executable_plan(block,by_slot):
    slot=int(block['slot'])
    pool=block['pool']
    swaps=block['swaps']
    mutations=block['known_target_mutations']
    unknown=block['unresolved_mutations']
    all_mutations=[]
    needed={}
    risk=Counter()
    for rec in mutations:
        name=rec['name']
        roles=rec.get('roles') or {}
        declared=[]
        for role,info in roles.items():
            if role not in STATE_ROLES:continue
            address=info.get('address')
            if address in IGNORED_SENTINELS or not isinstance(address,str):continue
            # The pool itself is an important baseline even if its instruction role is read-only.
            if role!='lb_pair' and not info.get('writable'): continue
            declared.append({'role':role,'address':address})
        for address in rec.get('remaining_account_addresses',[]):
            if address not in IGNORED_SENTINELS and isinstance(address,str):
                declared.append({'role':'UNDECODED_REMAINING_ACCOUNT','address':address})
        covered=[];missing=[]
        for record in declared:
            address=record['address']
            if address in by_slot.get(slot,{}):
                covered.append(record)
            else:
                missing.append(record)
                key=(address,record['role'])
                entry=needed.setdefault(key,{'slot':slot,'pool':pool,
                         'address':address,'roles':set(),'instructions':set(),
                         'before_swap':False,'unknown_remaining':False})
                entry['roles'].add(record['role'])
                entry['instructions'].add(name)
                entry['before_swap'] |= bool(rec.get('precedes_some_target_swap_in_block'))
                entry['unknown_remaining'] |= record['role']=='UNDECODED_REMAINING_ACCOUNT'
        if rec.get('precedes_some_target_swap_in_block'):risk['before_swap']+=1
        if rec.get('args_status')!='FULL_IDL_ARGS_PASS':risk['args_unparseable']+=1
        if missing:risk['witness_gap']+=1
        all_mutations.append({
            'instruction':name,
            'tx_index':rec['tx_index'], 'outer':rec['outer'],'inner':rec['inner'],
            'precedes_some_target_swap_in_block':rec.get('precedes_some_target_swap_in_block'),
            'same_transaction_as_swap':rec.get('same_tx_as_target_swap'),
            'args_status':rec.get('args_status'),
            'block_boundary_required_accounts':len(declared),
            'block_boundary_observed_accounts':len(covered),
            'block_boundary_missing_accounts':missing,
            'post_mutation_instruction_state':'NOT_AVAILABLE_FROM_BLOCK_BOUNDARY',
            'transition_model':'UNAVAILABLE',
        })
    queue=[]
    for v in needed.values():
        queue.append({**v,'roles':sorted(v['roles']),
                      'instructions':sorted(v['instructions'])})
    queue.sort(key=lambda x:(not x['before_swap'],x['unknown_remaining'],x['slot'],x['address']))
    # A successful normalization of swaps does not clear non-swap mutation blockers.
    if not swaps and not mutations and not unknown:
        status='NO_RELEVANT_ACTIVITY_RECORDED'
    elif mutations or unknown:
        status='INSTRUCTION_PRESTATE_UNAVAILABLE' if swaps else 'MUTATION_ONLY_BLOCK_BOUNDARY_EVIDENCE_INCOMPLETE'
    else:
        status='SWAP_ONLY_CANDIDATE_REQUIRES_INDEPENDENT_ACCOUNT_PARITY'
    return {'slot':slot,'pool':pool,'label':block['label'],'swaps':len(swaps),
            'recognized_target_mutations':len(mutations),'unresolved_calls':len(unknown),
            'mutation_order_before_swap':risk['before_swap'],
            'unparseable_mutations':risk['args_unparseable'],
            'mutations_missing_any_boundary_account':risk['witness_gap'],
            'known_mutations':all_mutations,'unresolved_names':[x.get('name',x.get('kind','UNKNOWN')) for x in unknown],
            'account_queue':queue,'historical_execution_status':status,
            'historical_gate':'UNAVAILABLE'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--v3',type=Path,required=True)
    p.add_argument('--corpus',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--queue-limit',type=int,default=100)
    args=p.parse_args(argv)
    require(args.queue_limit>0 and args.queue_limit<=500,'invalid acquisition queue bound')
    v3=args.v3.resolve();corpus=args.corpus.resolve();output=args.out.resolve()
    require(not output.exists(),'output exists; evidence must be immutable')
    require(sha(v3/'summary.json')==V3_HASH,'V3 historical evidence summary SHA256 changed')
    d=load(v3/'summary.json')
    require(d['schema']=='D1_MUTATION_GAP_WITNESS_V3' and
            d['frozen_canonical_swap_count']==127743 and
            d['full_blocks_reaudited']==24 and
            d['known_target_mutation_count_in_samples']==76 and
            d['archived_account_pairs_selected']==80,'V3 documented sample counts changed')
    c=load(corpus)
    require(c['economic_swaps']==127743,'V4 corpus canonical economic swap count changed')
    require(c['full_block_state_mutation_coverage']=='NOT_PROVEN',
            'V4 source contains unsupported historical coverage claim')
    require(c['gTFA_scan']['complete_page_scan'] is True,'V4 corpus is only a partial page scan')
    require(c['gTFA_scan']['canonical_transactions_missing_from_pool_touch_pages']==0,
            'frozen D1 canonical swaps missing in gTFA page scan')
    rows=load(v3/'account_boundary_witnesses.json')
    require(len(rows)==80,'V3 account pair inventory incomplete')
    witnessed=defaultdict(dict)
    for row in rows:
        slot=int(row['slot']);address=row['address']
        require(row['pre_slot']==slot-1 and row['end_slot']==slot, 'V3 account boundary drift')
        require(address not in witnessed[slot], 'duplicate sampled account pair')
        pre=capture_verify(v3,slot-1,address)
        post=capture_verify(v3,slot,address)
        actual=('CREATED_IN_SLOT' if pre.get('account') is None and post.get('account') is not None
                else 'CLOSED_IN_SLOT' if pre.get('account') is not None and post.get('account') is None
                else 'NOT_PRESENT_AT_BOTH_BOUNDARIES' if pre.get('account') is None and post.get('account') is None
                else 'UNCHANGED' if pre['account']['data_sha256']==post['account']['data_sha256']
                     else 'CHANGED')
        require(actual==row['witness']['status'],'V3 witness status contradicts raw account bytes')
        witnessed[slot][address]=row
    blocks=[];summ=Counter();missingqueue=[]
    for path in sorted((v3/'blocks').glob('*.json'),key=lambda p:int(p.stem)):
        b=load(path)
        br=executable_plan(b,witnessed)
        blocks.append(br)
        summ['blocks']+=1;summ['swaps']+=br['swaps']
        summ['target_mutations']+=br['recognized_target_mutations']
        summ['unresolved_calls']+=br['unresolved_calls']
        summ['mutation_before_swap']+=br['mutation_order_before_swap']
        summ['witness_gap_mutations']+=br['mutations_missing_any_boundary_account']
        summ['args_unparseable']+=br['unparseable_mutations']
        missingqueue.extend(br['account_queue'])
    require(summ['blocks']==24 and summ['swaps']==25 and summ['target_mutations']==76,
            'V3 block-level inventory/sample count mismatch')
    seen=set();queue=[]
    for x in sorted(missingqueue,key=lambda x:(not x['before_swap'],x['unknown_remaining'],x['slot'],x['address'])):
        k=(x['slot'],x['address'])
        if k in seen:continue
        seen.add(k)
        queue.append(x)
    output.mkdir(parents=True,exist_ok=False)
    with (output/'per_block.jsonl').open('x') as f:
        for b in blocks:f.write(json.dumps({k:v for k,v in b.items() if k not in ('known_mutations','account_queue')},sort_keys=True)+'\n')
    with (output/'missing_witness_worklist.jsonl').open('x') as f:
        for x in queue[:args.queue_limit]:f.write(json.dumps(x,sort_keys=True)+'\n')
    arg_fail=Counter();target_muts=Counter();captured_status=Counter()
    for b in blocks:
        for r in b['known_mutations']:
            target_muts[r['instruction']]+=1
            if r['args_status']!='FULL_IDL_ARGS_PASS':arg_fail[r['instruction']]+=1
    for row in rows:captured_status[row['witness']['status']]+=1
    segs=c['gTFA_scan']['segments'];scope=Counter();parse_fail=Counter()
    for row in segs.values():
        scope.update(row['metrics'])
        parse_fail.update(row.get('named_target_arg_failures',{}))
    summary={
        'schema':'D1_MUTATION_SCOPE_AND_BOUNDARY_EVIDENCE_V4',
        'V3_summary_sha256':V3_HASH,
        'corpus_summary_sha256':sha(corpus),
        'canonical_swaps':127743,
        'sampled_blocks':summ['blocks'],'sampled_canonical_swaps':summ['swaps'],
        'sampled_target_mutations':summ['target_mutations'],
        'sampled_unresolved_calls':summ['unresolved_calls'],
        'sampled_mutations_before_swap':summ['mutation_before_swap'],
        'sampled_mutations_with_missing_boundary_state':summ['witness_gap_mutations'],
        'sampled_args_unparseable':dict(arg_fail),
        'sampled_target_mutation_types':dict(target_muts),
        'v3_witness_pair_status':dict(captured_status),
        'gTFA_page_scan':{
            'target_non_swap_mutations_named':scope.get('known_non_swap_mutations',0),
            'ambiguous_instruction_rows':scope.get('ambiguous_instruction_rows',0),
            'target_known_discriminator_args_unparseable':scope.get('known_target_args_unparseable',0),
            'arg_decode_failure_types':dict(parse_fail)},
        'missing_account_pair_worklist_total':len(queue),
        'missing_account_pair_worklist_emitted':min(len(queue),args.queue_limit),
        'account_pair_worklist_sha256':sha(output/'missing_witness_worklist.jsonl'),
        'per_block_sha256':sha(output/'per_block.jsonl'),
        'historical_evidence_scope':'BLOCK_BOUNDARY_OBSERVATION_ONLY',
        'mutation_execution_parity':'NOT_TESTED',
        'intra_slot_state_after_non_swap':'UNAVAILABLE',
        'full_block_mutation_coverage':'NOT_PROVEN',
        'D1_HISTORICAL_GATE':'UNAVAILABLE',
        'block_eligibility_counts':dict(Counter(b['historical_execution_status'] for b in blocks))}
    (output/'summary.json').write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n')
    print('===== D1 V4 MUTATION RECONCILIATION =====')
    print('V3_SAMPLED_BLOCKS=',summ['blocks'])
    print('V3_SWAPS=',summ['swaps'])
    print('V3_TARGET_MUTATIONS=',summ['target_mutations'])
    print('V3_WITNESS_PAIR_STATUS=',json.dumps(dict(captured_status),sort_keys=True))
    print('MUTATIONS_BEFORE_SWAP=',summ['mutation_before_swap'])
    print('MUTATIONS_MISSING_ACCOUNT_WITNESS=',summ['witness_gap_mutations'])
    print('TARGET_ARGS_UNPARSEABLE=',json.dumps(dict(arg_fail),sort_keys=True))
    print('FULL_GFTA_KNOWN_NON_SWAP=',scope.get('known_non_swap_mutations',0))
    print('FULL_GFTA_ARG_DECODE_FAILURES=',scope.get('known_target_args_unparseable',0))
    print('FULL_GFTA_AMBIGUOUS_ROWS=',scope.get('ambiguous_instruction_rows',0))
    print('MISSING_ACCOUNT_PAIR_WORKLIST=',len(queue))
    print('BLOCK_EXECUTION_ELIGIBILITY=',json.dumps(summary['block_eligibility_counts'],sort_keys=True))
    print('HISTORICAL_INSTRUCTION_PARITY=UNAVAILABLE')
    print('D1_HISTORICAL_GATE=UNAVAILABLE')
    print('REPORT=',output/'summary.json')
    print('REPORT_SHA256=',sha(output/'summary.json'))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
