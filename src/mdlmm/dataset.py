"""Content-locked input bundles and independent account/transaction evidence."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from .codec import account_bytes,decode_lb_pair,decode_bin_array,decode_clock,build_pool_state,discriminator,IdlCodec
from .errors import Unavailable,Unsupported,ValidationError
from .model import PoolState,Event,PROGRAM_ID,CLOCK_ID
from .normalize import normalize_block
from .replay import VersionRegistry,compare_states
from .util import read_json,read_jsonl,safe_path,hash_file,digest,write_json,iso_ms


def ref_file(root:Path,relative:str)->dict:
    path=safe_path(root,relative)
    if not path.is_file():
        raise Unavailable(f'input file missing: {relative}')
    return {'path':relative,'sha256':hash_file(path)}


class Dataset:
    def __init__(self,path:Path):
        if not path.is_file():
            raise Unavailable(f'historical dataset not supplied: {path}')
        self.path,self.root=path.resolve(),path.resolve().parent
        self.meta=read_json(path)
        if self.meta.get('schema_version')!=1:
            raise Unsupported('unknown dataset schema version')
        self.kind=self.meta.get('data_kind')
        if self.kind not in ('HISTORICAL','SYNTHETIC'):
            raise Unavailable('CURRENT snapshots alone are not a historical research dataset')
        self.files={str(self.path):hash_file(self.path)}
        self.reference_path=self.file(self.meta['reference'])
        self.registry_path=self.file(self.meta['program_registry'])
        self.registry=VersionRegistry(read_json(self.registry_path)['entries'],self.root,synthetic=self.kind=='SYNTHETIC')
        self.segments=self.meta.get('segments',[])
        if not self.segments:
            raise Unavailable('no replay segments in dataset')
        if len({s['id'] for s in self.segments})!=len(self.segments):
            raise ValidationError('duplicate segment IDs')
        for s in self.segments:
            for k in ('snapshot','events','checkpoints','coverage'):
                self.file(s[k])
        for e in read_json(self.registry_path)['entries']:
            for key in ('binary_file','idl_file','binding_evidence_file'):
                if key in e:
                    p=safe_path(self.root,e[key]);self.files[str(p)]=hash_file(p)

    def file(self,ref:dict)->Path:
        if not isinstance(ref,dict) or not ref.get('path') or not ref.get('sha256'):
            raise ValidationError('every input requires path and sha256')
        p=safe_path(self.root,ref['path'])
        if not p.is_file():
            raise Unavailable(f'dataset input missing: {ref["path"]}')
        if hash_file(p)!=ref['sha256']:
            raise ValidationError(f'dataset input hash mismatch: {ref["path"]}')
        self.files[str(p)]=ref['sha256']
        return p

    def recheck(self):
        for name,h in self.files.items():
            if hash_file(Path(name))!=h:
                raise ValidationError('input mutated during run: '+name)

    @property
    def fingerprint(self):
        return digest(sorted(self.files.items()))

    def snapshot(self,ref:dict,*,boundary:str='END_SLOT')->PoolState:
        p=self.file(ref);capture=read_json(p)
        if capture.get('boundary')!=boundary:
            raise Unsupported('account capture has wrong requested boundary: '+boundary)
        state=PoolState.from_dict(capture['state'])
        if state.data_kind!=self.kind or state.slot!=capture['slot']:
            raise ValidationError('capture state/kind/slot mismatch')
        if self.kind=='HISTORICAL':
            raw=capture.get('raw_accounts')
            if not raw:
                raise Unavailable('historical account parity requires raw account bytes, not self-generated decoded JSON')
            if capture.get('source_kind') not in ('ARCHIVAL_RPC','GEYSER_ACCOUNT_CAPTURE','SVM_VALIDATED_CAPTURE','RECORDED_FINALIZED_RPC'):
                raise Unavailable('historical capture source/provenance unresolved')
            pair=decode_lb_pair(account_bytes(raw[state.pool]))
            arrays=[]
            for address,account in raw.items():
                if account is None or account.get('owner')!=PROGRAM_ID:
                    continue
                b=account_bytes(account)
                if b[:8]==discriminator('account','BinArray'):
                    arrays.append(decode_bin_array(b))
            decoded=build_pool_state(state.pool,pair,arrays,slot=state.slot,
                                     complete_bin_map=state.complete_bin_map,data_kind=self.kind)
            compare_states(state,decoded)
            if not capture.get('raw_sha256') or not capture.get('source_receipt'):
                raise Unavailable('historical capture needs raw source receipt')
            if capture.get('raw_accounts_sha256')!=digest(raw):
                raise ValidationError('raw account-set digest mismatch/missing')
            clock_raw=raw.get(CLOCK_ID)
            if clock_raw is None:
                raise Unavailable('historical account capture must include Clock')
            import base64
            clock=decode_clock(base64.b64decode(clock_raw['data'][0],validate=True))
            if clock['slot']!=state.slot or clock['unix_timestamp']*1000!=capture['timestamp_ms']:
                raise ValidationError('capture Clock/slot/timestamp mismatch')
        return state

    def events(self,segment:dict):
        for row in read_jsonl(self.file(segment['events'])):
            event=Event.from_dict(row)
            if self.kind=='HISTORICAL' and event.kind=='OBSERVED_LIQUIDITY':
                self.validate_liquidity_evidence(event)
            yield event

    def validate_liquidity_evidence(self,event:Event):
        """Both instruction boundaries must decode to the claimed state patch."""
        from .replay import economic_projection
        payload=event.payload
        before_ref=payload.get('pre_accounts_evidence')
        after_ref=payload.get('post_accounts_evidence')
        if not isinstance(before_ref,dict) or not isinstance(after_ref,dict):
            raise Unavailable('liquidity evidence must be hash-bound raw capture references, not a label')
        before=self.snapshot(before_ref,boundary='BEFORE_INSTRUCTION')
        after=self.snapshot(after_ref,boundary='AFTER_INSTRUCTION')
        for desc in (before_ref,after_ref):
            capture=read_json(self.file(desc))
            if capture.get('instruction_order')!=event.order.__dict__ or capture.get('tx_signature')!=event.tx_signature:
                raise ValidationError('liquidity raw evidence not bound to exact instruction')
        if event.pre_state_hash!=before.economic_hash() or event.post_state_hash!=after.economic_hash():
            raise ValidationError('liquidity pre/post state hash mismatch')
        expected=[vars(b) for i,b in after.bins.items()
                  if i not in before.bins or vars(b)!=vars(before.bins[i])]
        # Compare relevant reserve/supply changes, not fee-growth metadata that
        # the direct MM economic model deliberately excludes.
        fields=('bin_id','amount_x','amount_y','price','liquidity_supply')
        projection=lambda rows: sorted([{k:r[k] for k in fields} for r in rows],key=lambda r:r['bin_id'])
        if projection(expected)!=projection(payload.get('bins_after',[])):
            raise ValidationError('liquidity normalized bin delta differs from raw account bytes')
        if not payload.get('token_flow_evidence'):
            raise Unavailable('liquidity flow requires independent transaction transfer evidence')
        flow=read_json(self.file(payload['token_flow_evidence']))
        if flow.get('tx_signature')!=event.tx_signature or flow.get('instruction_order')!=event.order.__dict__:
            raise ValidationError('liquidity token flow evidence belongs to a different instruction')
        for token in ('x','y'):
            if flow.get('external_flow_'+token)!=payload.get('external_flow_'+token):
                raise ValidationError('liquidity observed token transfer mismatch')

    def checkpoints(self,segment:dict)->dict[str,PoolState]:
        result={}
        for row in read_json(self.file(segment['checkpoints']))['checkpoints']:
            if row['event_id'] in result:
                raise ValidationError('duplicate checkpoint event ID')
            state=self.snapshot(row['capture'])
            result[row['event_id']]=state
        if not result:
            raise Unavailable('no independent account checkpoints')
        return result

    def verify_coverage(self,segment:dict,events)->dict:
        import hashlib,sqlite3,tempfile
        coverage=read_json(self.file(segment['coverage']))
        if coverage.get('missing') or not coverage.get('complete'):
            raise Unavailable('historical coverage is incomplete')
        legacy_ids=coverage.get('event_ids')
        count=0;last_id=None;swaps=0;patch_keys=set();rolling=hashlib.sha256()
        with tempfile.TemporaryDirectory(prefix='mdlmm_coverage_') as tmp:
            index=sqlite3.connect(str(Path(tmp)/'events.sqlite'))
            index.execute('CREATE TABLE swaps (id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0)')
            clocks={}
            if self.kind=='HISTORICAL':
                if 'clocks' not in coverage:
                    raise Unavailable('historical coverage requires captured per-slot Clock evidence')
                for row in read_json(self.file(coverage['clocks']))['clocks']:
                    raw=read_json(self.file(row['capture']))
                    import base64
                    c=decode_clock(base64.b64decode(raw['account']['data'][0],validate=True))
                    if raw.get('pubkey')!=CLOCK_ID or c['slot']!=row['slot'] or not raw.get('source_receipt'):
                        raise ValidationError('historical Clock capture binding invalid')
                    if c['slot'] in clocks and clocks[c['slot']]!=c['unix_timestamp']:
                        raise ValidationError('conflicting Clock captures')
                    clocks[c['slot']]=c['unix_timestamp']
            with index:
                for event in events:
                    last_id=event.id
                    if self.kind=='HISTORICAL' and (event.order.slot not in clocks or event.clock_unix_timestamp!=clocks[event.order.slot]):
                        raise Unavailable('normalized event lacks matching raw historical Clock')
                    rolling.update((last_id+'\n').encode())
                    if legacy_ids is not None and (count>=len(legacy_ids) or legacy_ids[count]!=last_id):
                        raise ValidationError('coverage and ordered event inventory disagree')
                    count+=1
                    if event.kind=='SWAP':
                        swaps+=1
                        try:
                            index.execute('INSERT INTO swaps(id,payload_hash) VALUES(?,?)',
                              (last_id,digest({'payload':event.payload,'observed':event.observed})))
                        except sqlite3.IntegrityError as exc:
                            raise ValidationError('duplicate normalized swap') from exc
                    elif event.kind=='OBSERVED_LIQUIDITY':
                        patch_keys.add((event.tx_signature,event.order.instruction_index,event.order.inner_instruction_index))
            if not count:
                raise Unavailable('empty event sample')
            if legacy_ids is not None and len(legacy_ids)!=count:
                raise ValidationError('event inventory count mismatch')
            if legacy_ids is None and (coverage.get('event_count')!=count or coverage.get('event_id_stream_sha256')!=rolling.hexdigest()):
                raise ValidationError('streamed event count/hash mismatch')
            base={'events':count,'swaps':swaps,'last_event_id':last_id}
            if self.kind=='SYNTHETIC':
                if coverage.get('source_kind')!='SYNTHETIC_GENERATOR':
                    raise ValidationError('synthetic coverage must be labeled')
                index.close()
                return {**base,'scope':'SYNTHETIC_TEST_EVENT_COVERAGE'}
            mode=coverage.get('source_kind')
            if mode=='POOL_ADDRESS_SIGNATURE_SCAN':
                from .history import verify_signature_coverage
                if coverage.get('pool')!=segment['pool']:
                    raise ValidationError('coverage belongs to another pool')
                verify_signature_coverage(coverage,self.file)
            elif mode=='FINALIZED_FULL_BLOCK_SCAN':
                if not coverage.get('blocks') or not coverage.get('produced_slots') or not coverage.get('slot_list_receipt'):
                    raise Unavailable('coverage needs blocks and independent produced-slot receipt')
                slot_receipt=read_json(self.file(coverage['slot_list_receipt']))
                slot_list=slot_receipt.get('result',slot_receipt.get('produced_slots'))
                if slot_list!=coverage['produced_slots']:
                    raise ValidationError('produced-slot list receipt mismatch')
            else:
                raise Unavailable('historical coverage requires full-block or closed pool-address scan')
            seen_slots=[]
            resolved={tuple(r) for r in coverage.get('resolved_mutations',[])}
            idl_cache={}
            for block_ref in coverage['blocks']:
                saved=read_json(self.file(block_ref));slot=saved['slot'];seen_slots.append(slot)
                binding=self.registry.resolve(slot)
                idl_name=binding['idl_file']
                if idl_name not in idl_cache:
                    idl_cache[idl_name]=IdlCodec.load(safe_path(self.root,idl_name))
                decoded=normalize_block(slot,saved['block'],{segment['pool']},raw_hash=saved.get('raw_sha256'),
                                        idl=idl_cache[idl_name],registry_entry=binding,clock_unix_timestamp=clocks.get(slot))
                with index:
                    for row in decoded['events']:
                        event=Event.from_dict(row)
                        if event.kind!='SWAP':continue
                        found=index.execute('SELECT payload_hash,seen FROM swaps WHERE id=?',(event.id,)).fetchone()
                        if found is None or found[1]:
                            raise ValidationError('extra/duplicate raw swap relative to normalized events')
                        if found[0]!=digest({'payload':event.payload,'observed':event.observed}):
                            raise ValidationError('normalized swap differs from raw transaction')
                        index.execute('UPDATE swaps SET seen=1 WHERE id=?',(event.id,))
                for blocker in decoded['blockers']:
                    key=(blocker['signature'],blocker.get('instruction'),blocker.get('inner_instruction'))
                    if key not in patch_keys or key not in resolved:
                        raise Unavailable('unresolved historical mutation: '+str(blocker)[:300])
            if seen_slots!=coverage['produced_slots']:
                raise ValidationError('produced block missing/reordered')
            if index.execute('SELECT COUNT(*) FROM swaps WHERE seen=0').fetchone()[0]:
                raise ValidationError('normalized swap has no independently decoded raw event')
            index.close()
            return {**base,'scope':'RAW_BLOCK_RENORMALIZATION','blocks':len(seen_slots)}


def seal_descriptor(root:Path,meta:dict,output='dataset.json')->Path:
    """Attach hashes to an explicit provider/export descriptor; no validation upgrade."""
    d=dict(meta)
    for k in ('reference','program_registry'):
        if isinstance(d[k],str):
            d[k]=ref_file(root,d[k])
    segments=[]
    for s in d['segments']:
        s=dict(s)
        for k in ('snapshot','events','checkpoints','coverage'):
            if isinstance(s[k],str):
                s[k]=ref_file(root,s[k])
        segments.append(s)
    d['segments']=segments
    path=root/output
    write_json(path,d,exclusive=True)
    return path
