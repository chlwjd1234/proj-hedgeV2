"""Turn a fully evidenced archival export plan into a sealed D0/D1 bundle.

The plan supplies starting/checkpoint account captures, raw full blocks,
per-slot raw Clock captures, program-version evidence and optional independently
observed per-instruction liquidity transitions. No historical state is inferred
from current RPC snapshots or OHLCV. Unknown mutations stop construction.
"""
from __future__ import annotations
import base64,hashlib
from dataclasses import asdict
from pathlib import Path
from .codec import IdlCodec,decode_clock
from .dataset import Dataset,ref_file
from .errors import Unavailable,ValidationError
from .model import Event,Order,CLOCK_ID
from .normalize import normalize_block
from .replay import VersionRegistry,ordered_events
from .util import read_json,read_jsonl,write_json,write_jsonl,safe_path,hash_file,iso_ms


def prepare_historical(plan_path:Path,output_name='dataset.json')->dict:
    plan=read_json(plan_path);root=plan_path.parent
    if plan.get('data_kind')!='HISTORICAL':
        raise ValidationError('archival preparation requires an explicitly HISTORICAL plan')
    if (root/output_name).exists():
        raise ValidationError('sealed dataset already exists; prepare in a new export directory')
    registry=VersionRegistry(read_json(safe_path(root,plan['program_registry']))['entries'],root)
    codecs={};segments=[];total=0
    for spec in plan['segments']:
        sid=spec['id']
        if not sid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in sid):
            raise ValidationError('unsafe segment identifier')
        target=root/'normalized'/sid;target.mkdir(parents=True,exist_ok=False)
        coverage=read_json(safe_path(root,spec['coverage']))
        # Input refs inside coverage are relative to the bundle root, not the
        # coverage file directory. The collector/export relocation is explicit.
        def load(desc):
            path=safe_path(root,desc['path'])
            if not path.exists() or hash_file(path)!=desc['sha256']:
                raise ValidationError('archival input hash mismatch: '+desc['path'])
            return read_json(path)
        clocks={}
        clock_descriptor=ref_file(root,spec['clocks'])
        for row in load(clock_descriptor)['clocks']:
            raw=load(row['capture'])
            if raw.get('pubkey')!=CLOCK_ID or not raw.get('source_receipt'):
                raise Unavailable('raw historical Clock receipt missing')
            c=decode_clock(base64.b64decode(raw['account']['data'][0],validate=True))
            if c['slot']!=row['slot']:raise ValidationError('Clock evidence slot mismatch')
            clocks[c['slot']]=c['unix_timestamp']
        transitions={}
        if spec.get('observed_transitions'):
            for row in read_jsonl(safe_path(root,spec['observed_transitions'])):
                event=Event.from_dict(row)
                if event.kind!='OBSERVED_LIQUIDITY':
                    raise ValidationError('only independently evidenced liquidity mutations may replace raw aggregate events')
                key=(event.tx_signature,event.order.instruction_index,event.order.inner_instruction_index)
                if key in transitions:raise ValidationError('duplicate observed instruction transition')
                transitions[key]=event
        last_by_slot={};stream_hash=hashlib.sha256();n=0;resolved=set()
        def generate():
            nonlocal n
            for desc in coverage['blocks']:
                saved=load(desc);slot=saved['slot']
                if slot not in clocks:raise Unavailable(f'historical Clock missing at slot {slot}')
                binding=registry.resolve(slot)
                if binding['idl_file'] not in codecs:
                    codecs[binding['idl_file']]=IdlCodec.load(safe_path(root,binding['idl_file']))
                normalized=normalize_block(slot,saved['block'],{spec['pool']},raw_hash=saved.get('raw_sha256'),
                         idl=codecs[binding['idl_file']],registry_entry=binding,clock_unix_timestamp=clocks[slot],
                         require_canonical_swaps=True)
                replacement=[];replaced_outer=set()
                for blocker in normalized['blockers']:
                    if blocker.get('resolution_policy') != 'OBSERVED_STATE_OR_SVM':
                        raise Unavailable('canonical normalization must be repaired before state replacement: '+str(blocker)[:350])
                    key=(blocker['signature'],blocker.get('instruction'),blocker.get('inner_instruction'))
                    if key not in transitions:
                        raise Unavailable('unresolved raw instruction: '+str(blocker)[:350])
                    event=transitions[key]
                    if event.order.slot!=slot or event.pool!=spec['pool']:
                        raise ValidationError('replacement transition slot/pool mismatch')
                    replacement.append(event);resolved.add(key)
                    replaced_outer.add((event.tx_signature,event.order.instruction_index))
                rows=[]
                for raw_event in normalized['events']:
                    event=Event.from_dict(raw_event)
                    outer=(event.tx_signature,event.order.instruction_index)
                    if outer in replaced_outer:
                        if event.kind=='SWAP':
                            raise Unavailable('mixed swap+liquidity CPI outer instruction requires a more granular SVM export')
                        # Aggregate add/composition events are accounted once by
                        # the verified instruction transition, never twice.
                        continue
                    if event.kind not in ('SWAP','FEE_PARAMETERS'):
                        raise Unavailable('raw event requires independent state transition: '+event.kind)
                    rows.append(event)
                rows.extend(replacement)
                for event in sorted(rows,key=lambda e:e.order):
                    n+=1;stream_hash.update((event.id+'\n').encode());last_by_slot[slot]=event.id
                    yield event
        events_path=target/'events.jsonl.gz'
        write_jsonl(events_path,(asdict(e) for e in ordered_events(generate())))
        unused=set(transitions)-resolved
        if unused:raise ValidationError('unused observed transition cannot silently enter bundle')
        if not n:raise Unavailable('no financial events in archival segment')
        checkpoints=[]
        for cp in spec['checkpoints']:
            slot=cp['slot']
            if slot not in last_by_slot:
                raise Unavailable('checkpoint must follow a financial event at the same END_SLOT boundary')
            checkpoints.append({'event_id':last_by_slot[slot],'capture':ref_file(root,cp['capture'])})
        write_json(target/'checkpoints.json',{'checkpoints':checkpoints},exclusive=True)
        coverage.update(event_count=n,event_id_stream_sha256=stream_hash.hexdigest(),clocks=clock_descriptor,
                        resolved_mutations=[list(k) for k in sorted(resolved)])
        coverage.pop('event_ids',None)
        write_json(target/'coverage.json',coverage,exclusive=True)
        segments.append({'id':sid,'pool':spec['pool'],'start_ms':iso_ms(spec['start']), 'end_ms':iso_ms(spec['end']),
             'snapshot':ref_file(root,spec['snapshot']),
             **{key:ref_file(root,str((target/file).relative_to(root))) for key,file in
                [('events','events.jsonl.gz'),('checkpoints','checkpoints.json'),('coverage','coverage.json')]}})
        total+=n
    meta={'schema_version':1,'data_kind':'HISTORICAL','reference':ref_file(root,plan['reference']),
          'program_registry':ref_file(root,plan['program_registry']),'segments':segments,'pools':plan['pools'],
          'preparation_plan':ref_file(root,plan_path.name)}
    if plan.get('windows'):meta['windows']=plan['windows']
    target=root/output_name;write_json(target,meta,exclusive=True)
    # Full replay remains the separate D0 gate. Sealing is not certification.
    Dataset(target)
    return {'status':'PASS','scope':'ARCHIVAL_NORMALIZATION_NOT_REPLAY_APPROVAL',
            'dataset':str(target),'segments':len(segments),'events':total,
            'canonical_swap_policy':'ONE_VERIFIED_SWAP2EVT_PAIR_PER_ACTUAL_INSTRUCTION',
            'historical_gate':'UNAVAILABLE','next_stage':'d0'}
