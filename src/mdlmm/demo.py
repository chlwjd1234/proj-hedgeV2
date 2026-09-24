"""Deterministic synthetic integration dataset. Never financial market evidence."""
from __future__ import annotations
import csv,math,hashlib
from pathlib import Path
from dataclasses import asdict
from .codec import b58encode
from .dataset import ref_file
from .mechanics import price_from_id,Q64,quote_exact_in
from .model import PoolState,Bin,FeeParameters,VariableState,PROGRAM_ID,Event,Order
from .util import D,dec,iso_ms,utc_iso,write_json,write_jsonl,sha256
from .regimes import DAY


def generate(root:Path,*,hours_per_event:int=1,reference_step_ms:int=60000)->Path:
    root.mkdir(parents=True,exist_ok=True)
    target=root/'dataset.json'
    if target.exists():
        return target
    if hours_per_event<1 or 24%hours_per_event:
        raise ValueError('hours_per_event must divide 24')
    start=iso_ms('2025-01-01T00:00:00Z')
    days=100
    def price_at(t):
        day=(t-start)/DAY
        base=100*math.exp(0.02*math.sin(day*0.8)+0.012*math.sin(day*1.7))
        if 55<=day<62:base*=math.exp(0.012*(day-55))
        elif 70<=day<77:base*=math.exp(-0.018*(day-70))
        elif 85<=day<92:base*=math.exp(0.08*math.sin((day-85)*math.pi/2))
        return base
    with (root/'reference.csv').open('w',newline='',encoding='utf8') as f:
        out=csv.writer(f);out.writerow(['ts_ms','available_ms','price','source','symbol'])
        for t in range(start,start+days*DAY+1,reference_step_ms):
            out.writerow([t,t,f'{price_at(t):.10f}','SYNTHETIC_EXTERNAL_SOL_REFERENCE','SOL/USDC'])
    bh=sha256(b'SYNTHETIC_NOT_AN_ONCHAIN_BINARY')
    ih=sha256(b'SYNTHETIC_NOT_AN_OFFICIAL_IDL_FILE')
    entry={'program_id':PROGRAM_ID,'start_slot':0,'end_slot':10**15,'version':'0.12.0',
           'binary_sha256':bh,'idl_sha256':ih,'model':'MM_V012','provenance':'SYNTHETIC_FIXTURE'}
    write_json(root/'program_registry.json',{'entries':[entry]})
    windows=[{'label':label,'start':utc_iso(start+day*DAY),'end':utc_iso(start+(day+7)*DAY)}
             for label,day in zip(('low_vol_sideways','uptrend','downtrend_stress','high_vol_roundtrip'),(40,55,70,85))]
    pools=[b58encode(hashlib.sha256(('SYNTHETIC_SOL_USDC_POOL_'+str(i)).encode()).digest()) for i in range(2)]
    segments=[]
    for wi,w in enumerate(windows):
        a,b=iso_ms(w['start']),iso_ms(w['end'])
        for pi,pool in enumerate(pools):
            sid=f'w{wi}_p{pi}';folder=root/sid;folder.mkdir(exist_ok=True)
            step=(10,40)[pi]
            price=price_at(a)
            active=round(math.log(price/1000)/math.log1p(step/10000))
            bins={}
            # Full initialized arrays, generous enough for both RV width rules.
            for index in range((active-150)//70,(active+150)//70+1):
                for i in range(index*70,index*70+70):
                    p=price_from_id(i,step)
                    x=int(100*1e9) if i>active else int(3*1e9) if i==active else 0
                    y=int(10000*1e6) if i<active else int(300*1e6) if i==active else 0
                    supply=p*x+Q64*y
                    bins[i]=Bin(i,x,y,p,supply)
            slot=1_000_000+wi*1_000_000+pi*100000
            params=FeeParameters(2000,30,600,5000,40000,350000,1000,0,pi%2,0,-443636,443636)
            state=PoolState(pool,active,step,params,VariableState(0,0,active,a//1000-1),bins,
                            program_version='0.12.0',binary_sha256=bh,idl_hash=ih,slot=slot,
                            known_bin_arrays=sorted({i//70 for i in bins}),complete_bin_map=True,data_kind='SYNTHETIC')
            initial={'slot':slot,'timestamp_ms':a,'boundary':'END_SLOT','state':state.to_dict(),
                     'source_kind':'SYNTHETIC_GENERATOR','provenance':'NO_REAL_MARKET_DATA'}
            write_json(folder/'initial.json',initial)
            events=[];checks=[]
            for n,t in enumerate(range(a+1000,b,hours_per_event*3600000),1):
                direction=(n%12)<6
                amount=5_000_000_000 if direction else 500_000_000
                pre_hash=state.economic_hash()
                q=quote_exact_in(state,amount,direction,t//1000,slot=slot+n)
                state=q.state;state.slot=slot+n;state.variable.last_update_timestamp=t//1000
                e=Event(Order(slot+n,0,1,2),t,f'SYNTHETIC_SIGNATURE_{sid}_{n}',pool,'SWAP',
                        program_version='0.12.0',binary_sha256=bh,idl_hash=ih,
                        clock_unix_timestamp=t//1000,clock_source='SYNTHETIC_FIXTURE',
                        observed={'amount_out':q.amount_out,'fee':q.fee,'protocol_fee':q.protocol_fee,
                                  'start_bin':q.start_bin,'end_bin':q.end_bin,'amount_left':0},
                        payload={'amount_in':amount,'swap_for_y':direction},pre_state_hash=pre_hash,
                        post_state_hash=state.economic_hash(),raw_sha256=sha256(f'SYNTHETIC_{sid}_{n}'.encode()),
                        data_kind='SYNTHETIC')
                events.append(asdict(e))
                if n%(24//hours_per_event)==0 or t+hours_per_event*3600000>=b:
                    cp=folder/f'checkpoint_{n}.json'
                    write_json(cp,{'slot':state.slot,'timestamp_ms':t,'boundary':'END_SLOT','state':state.to_dict(),
                                   'source_kind':'SYNTHETIC_GENERATOR','provenance':'SELF_CONSISTENCY_NOT_CHAIN_PARITY'})
                    checks.append({'event_id':e.id,'capture':ref_file(root,str(cp.relative_to(root)))})
            write_jsonl(folder/'events.jsonl.gz',events)
            write_json(folder/'checkpoints.json',{'checkpoints':checks})
            write_json(folder/'coverage.json',{'complete':True,'missing':[],
                       'source_kind':'SYNTHETIC_GENERATOR','event_ids':[Event.from_dict(e).id for e in events]})
            segments.append({'id':sid,'pool':pool,'start_ms':a,'end_ms':b,
                'snapshot':ref_file(root,f'{sid}/initial.json'),'events':ref_file(root,f'{sid}/events.jsonl.gz'),
                'checkpoints':ref_file(root,f'{sid}/checkpoints.json'),'coverage':ref_file(root,f'{sid}/coverage.json')})
    meta={'schema_version':1,'data_kind':'SYNTHETIC','reference':ref_file(root,'reference.csv'),
          'program_registry':ref_file(root,'program_registry.json'),'windows':windows,'segments':segments,
          'pools':[{'pool':p,'selection_time_ms':start,'selection_rule':'SYNTHETIC_FIXED',
                    'metadata_source':'SYNTHETIC_GENERATOR'} for p in pools],
          'warning':'This tests software integration only. No real transactions, pool returns, or economic success are asserted.'}
    write_json(target,meta,exclusive=True)
    return target
