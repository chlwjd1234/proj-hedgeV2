from __future__ import annotations
import subprocess,json
from pathlib import Path
from .errors import Unavailable,ValidationError
from .util import write_json,read_json,canonical,sha256
from .model import PoolState
from .mechanics import quote_exact_in


def oracle(root:Path,capture_path:Path,output:Path,requests:list[dict])->dict:
    script=root/'scripts/sdk/oracle.cjs'
    if not (root/'scripts/sdk/node_modules/@meteora-ag/dlmm').exists():
        raise Unavailable('official Node SDK not installed; run scripts/install_sdk.sh')
    idl=root/'external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json'
    if not idl.exists():
        raise Unavailable('pinned full IDL missing; run source-sync')
    output.mkdir(parents=True,exist_ok=True)
    request_path=output/'sdk_requests.json'
    write_json(request_path,requests,exclusive=True)
    try:
        result=subprocess.run(['node',str(script),str(capture_path.resolve()),str(request_path.resolve())],
                              cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=120)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise Unavailable('official SDK process unavailable/timed out') from exc
    lines=[x for x in result.stdout.splitlines() if x.strip().startswith('{')]
    if not lines:
        raise Unavailable('SDK oracle returned no structured result: '+result.stderr[-300:])
    answer=json.loads(lines[-1]);write_json(output/'sdk_result.json',answer,exclusive=True)
    if answer['status']=='UNAVAILABLE':raise Unavailable(answer['reason'])
    if answer['status']!='PASS':raise ValidationError(answer.get('reason','SDK oracle failed'))
    if answer['idl_canonical_sha256']!=sha256(canonical(read_json(idl))):
        raise ValidationError('npm SDK IDL differs from pinned source IDL')
    capture=read_json(capture_path);state=PoolState.from_dict(capture['state']);pair=answer['pair']
    if int(pair['activeId'])!=state.active_id or int(pair['binStep'])!=state.bin_step:
        raise ValidationError('SDK active-bin/bin-step parity failed')
    pmap={'baseFactor':'base_factor','filterPeriod':'filter_period','decayPeriod':'decay_period',
          'reductionFactor':'reduction_factor','variableFeeControl':'variable_fee_control',
          'maxVolatilityAccumulator':'max_volatility_accumulator','protocolShare':'protocol_share',
          'baseFeePowerFactor':'base_fee_power_factor','collectFeeMode':'collect_fee_mode'}
    for sdk_key,own_key in pmap.items():
        if int(pair['parameters'][sdk_key])!=getattr(state.parameters,own_key):
            raise ValidationError('SDK fee parameter parity failed: '+own_key)
    bin_count=0
    for array in answer['bin_arrays']:
        index=int(array['account']['index'])
        for j,b in enumerate(array['account']['bins']):
            own=state.bins[index*70+j]
            for sdk_key,own_key in (('amountX','amount_x'),('amountY','amount_y'),('price','price'),('liquiditySupply','liquidity_supply')):
                if int(b[sdk_key])!=getattr(own,own_key):
                    raise ValidationError('SDK bin parity failed')
            bin_count+=1
    comparisons=[]
    for item in answer['quotes']:
        q=quote_exact_in(state,int(item['amount_in']),item['swap_for_y'],capture['clock']['unix_timestamp'])
        sdkq=item['quote']
        # Official TS SDK swapQuote() exposes fee and protocolFee separately
        # after splitFee().  Our q.fee is TOTAL trader fee, while
        # q.protocol_fee is the protocol component.  Hence SDK `fee` is the
        # LP/user component q.fee - q.protocol_fee.
        expected={
            'consumedInAmount':q.amount_in,
            'outAmount':q.amount_out,
            'fee':q.fee-q.protocol_fee,
            'protocolFee':q.protocol_fee,
        }
        observed={k:int(sdkq[k]) for k in expected}
        mismatch={k:{'expected':expected[k],'observed':observed[k]}
                  for k in expected if expected[k]!=observed[k]}
        if mismatch:
            raise ValidationError('official SDK quote parity failed: '+json.dumps({
                'amount_in':int(item['amount_in']),
                'swap_for_y':bool(item['swap_for_y']),
                'mismatch':mismatch,
                'python_total_trader_fee':q.fee,
                'python_lp_user_fee':q.fee-q.protocol_fee,
                'python_protocol_fee':q.protocol_fee,
            },sort_keys=True,separators=(',',':')))
        comparisons.append({
            'amount_in':q.amount_in,'amount_out':q.amount_out,
            'total_trader_fee':q.fee,'lp_user_fee':q.fee-q.protocol_fee,
            'protocol_fee':q.protocol_fee,
        })
    if not comparisons or not bin_count:
        raise Unavailable('empty SDK parity comparison')
    return {'status':'PASS','validation_level':'SDK_PARITY','bins':bin_count,'quotes':comparisons,
            'sdk_version':'1.9.14','idl_canonical_sha256':answer['idl_canonical_sha256']}
