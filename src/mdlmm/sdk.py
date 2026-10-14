from __future__ import annotations
import subprocess,json
from pathlib import Path
from .errors import Unavailable,Unsupported,ValidationError
from .util import write_json,read_json,canonical,sha256
from .model import PoolState, TOKEN_PROGRAM
from .mechanics import quote_exact_in
from .codec import account_bytes, b58decode, find_program_address
from .swap_quote import BitmapExtension, SwapIntent, SwapMode, quote_swap


def _requests(requests: list[dict | SwapIntent]) -> tuple[list[dict], list[SwapIntent | None]]:
    if not isinstance(requests, list) or not requests:
        raise Unavailable('SDK oracle requires nonempty requests')
    wire, intents = [], []
    for request in requests:
        if isinstance(request, SwapIntent):
            intent = request
        elif isinstance(request, dict) and 'mode' in request:
            intent = SwapIntent.from_request(request)
        else:
            # Preserve the old two-key MM exact-in oracle. Extra keys cannot
            # quietly select a different instruction or host path.
            if not isinstance(request, dict) or set(request) != {'amount_in', 'swap_for_y'}:
                raise Unsupported('SDK oracle supports only amount_in/swap_for_y MM exact-in requests or typed SwapIntent')
            checked = SwapIntent.from_request({'mode': 'EXACT_IN', 'min_amount_out': '0',
                                               'host_fee_present': False, **request})
            wire.append({'amount_in': str(checked.amount_in), 'swap_for_y': checked.swap_for_y})
            intents.append(None)
            continue
        if intent.host_fee_present:
            raise Unsupported('pinned SDK oracle has no host/referral quote argument')
        wire.append(intent.to_request())
        intents.append(intent)
    return wire, intents


def _bitmap_evidence(capture: dict, state: PoolState) -> tuple[BitmapExtension | None, bool]:
    address, _ = find_program_address([b'bitmap', b58decode(state.pool)], state.program_id)
    if capture.get('bitmap_extension_address', address) != address:
        raise ValidationError('bitmap extension PDA parity failed')
    raw = capture.get('raw_accounts', {})
    if address in raw:
        if raw[address] is None:
            return None, True
        return BitmapExtension.from_bytes(account_bytes(raw[address])), True
    return None, address in capture.get('absent_accounts', [])


def _identity(answer: dict, wire: list[dict]):
    quotes = answer.get('quotes')
    if not isinstance(quotes, list) or len(quotes) != len(wire):
        raise ValidationError('SDK request/response quote count differs')
    for index, (item, request) in enumerate(zip(quotes, wire, strict=True)):
        if (type(item.get('request_index')) is not int or item['request_index'] != index
                or item.get('request') != request):
            raise ValidationError('SDK response request identity/order differs')


def oracle(root:Path,capture_path:Path,output:Path,requests:list[dict | SwapIntent])->dict:
    wire, intents = _requests(requests)
    typed = any(intent is not None for intent in intents)
    script=root/'scripts/sdk/oracle.cjs'
    if not (root/'scripts/sdk/node_modules/@meteora-ag/dlmm').exists():
        raise Unavailable('official Node SDK not installed; run scripts/install_sdk.sh')
    idl=root/'external/dlmm-sdk/ts-client/src/dlmm/idl/idl.json'
    if not idl.exists():
        raise Unavailable('pinned full IDL missing; run source-sync')
    capture=read_json(capture_path);state=PoolState.from_dict(capture['state'])
    extension, extension_known = _bitmap_evidence(capture, state) if typed else (None, False)
    if capture.get('pool') != state.pool:
        raise ValidationError('capture pool and state pool differ')
    output.mkdir(parents=True,exist_ok=True)
    request_path=output/'sdk_requests.json'
    write_json(request_path,wire,exclusive=True)
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
    if result.returncode != 0:
        raise ValidationError('SDK process exited unsuccessfully despite PASS payload')
    if answer.get('sdk_version') != '1.9.14':
        raise ValidationError('SDK version differs from pinned oracle')
    if answer['idl_canonical_sha256']!=sha256(canonical(read_json(idl))):
        raise ValidationError('npm SDK IDL differs from pinned source IDL')
    _identity(answer, wire)
    pair=answer['pair']
    if int(pair['activeId'])!=state.active_id or int(pair['binStep'])!=state.bin_step:
        raise ValidationError('SDK active-bin/bin-step parity failed')
    pmap={'baseFactor':'base_factor','filterPeriod':'filter_period','decayPeriod':'decay_period',
          'reductionFactor':'reduction_factor','variableFeeControl':'variable_fee_control',
          'maxVolatilityAccumulator':'max_volatility_accumulator','protocolShare':'protocol_share',
          'baseFeePowerFactor':'base_fee_power_factor','collectFeeMode':'collect_fee_mode',
          'functionType':'function_type','minBinId':'min_bin_id','maxBinId':'max_bin_id'}
    for sdk_key,own_key in pmap.items():
        if int(pair['parameters'][sdk_key])!=getattr(state.parameters,own_key):
            raise ValidationError('SDK fee parameter parity failed: '+own_key)
    for sdk_key, own_key in (('status', 'status'), ('pairType', 'pair_type'),
                             ('activationType', 'activation_type'), ('activationPoint', 'activation_point'),
                             ('tokenMintXProgramFlag', 'token_x_program_flag'),
                             ('tokenMintYProgramFlag', 'token_y_program_flag')):
        if int(pair[sdk_key]) != getattr(state, own_key):
            raise ValidationError('SDK eligibility-field decode parity failed: ' + own_key)
    if pair['tokenXMint'] != state.token_x_mint or pair['tokenYMint'] != state.token_y_mint:
        raise ValidationError('SDK pair token identities differ from supplied state')
    for sdk_key, own_key in (('volatilityAccumulator', 'volatility_accumulator'),
                             ('volatilityReference', 'volatility_reference'),
                             ('indexReference', 'index_reference'),
                             ('lastUpdateTimestamp', 'last_update_timestamp')):
        if int(pair['vParameters'][sdk_key]) != getattr(state.variable, own_key):
            raise ValidationError('SDK variable parameter parity failed: ' + own_key)
    if state.reward_mints is not None:
        if tuple(reward['mint'] for reward in pair['rewardInfos']) != state.reward_mints:
            raise ValidationError('SDK reward-mint decode parity failed')
    if state.bin_array_bitmap is not None:
        if tuple(map(int, pair['binArrayBitmap'])) != state.bin_array_bitmap:
            raise ValidationError('SDK internal bitmap decode parity failed')
    for sdk_key, own_key in (('amountX', 'protocol_fee_x'), ('amountY', 'protocol_fee_y')):
        if getattr(state, own_key) is not None and int(pair['protocolFee'][sdk_key]) != getattr(state, own_key):
            raise ValidationError('SDK protocol counter decode parity failed')
    if state.last_updated_at is not None and int(pair['lastUpdatedAt']) != state.last_updated_at:
        raise ValidationError('SDK last_updated_at decode parity failed')
    clock_map = {'slot': 'slot', 'epochStartTimestamp': 'epoch_start_timestamp', 'epoch': 'epoch',
                 'leaderScheduleEpoch': 'leader_schedule_epoch', 'unixTimestamp': 'unix_timestamp'}
    for sdk_key, own_key in clock_map.items():
        if int(answer['clock'][sdk_key]) != capture['clock'][own_key]:
            raise ValidationError('SDK raw Clock and supplied quote Clock differ')
    if typed:
        for side in ('x', 'y'):
            mint = answer['token_mints'][side]
            if (mint['address'] != getattr(state, 'token_' + side + '_mint')
                    or mint['decimals'] != getattr(state, 'decimals_' + side)):
                raise ValidationError('SDK mint/decimal decode parity failed')
            if capture['raw_accounts'][mint['address']]['owner'] != TOKEN_PROGRAM:
                raise Unsupported('typed SDK parity supports SPL token accounts without transfer extensions')
        sdk_ext = answer.get('bitmap_extension')
        if extension is None:
            if sdk_ext is not None:
                raise ValidationError('SDK unexpected bitmap extension')
        else:
            account = sdk_ext['account'] if sdk_ext else {}
            if (account.get('lbPair') != state.pool
                    or tuple(tuple(map(int, row)) for row in account.get('positiveBinArrayBitmap', [])) != extension.positive
                    or tuple(tuple(map(int, row)) for row in account.get('negativeBinArrayBitmap', [])) != extension.negative):
                raise ValidationError('SDK extension bitmap decode parity failed')
    bin_count=0
    array_indices = []
    lo_fields = (('openOrderAmount', 'open_order_amount'),
                 ('processedOrderRemainingAmount', 'processed_order_remaining_amount'),
                 ('totalProcessingOrderAmount', 'total_processing_order_amount'),
                 ('limitOrderAskSide', 'limit_order_ask_side'),
                 ('fulfilledOrderAmountX', 'fulfilled_order_amount_x'),
                 ('fulfilledOrderAmountY', 'fulfilled_order_amount_y'),
                 ('limitOrderFeeAskSide', 'limit_order_fee_ask_side'),
                 ('limitOrderFeeBidSide', 'limit_order_fee_bid_side'), ('orderAge', 'order_age'))
    for array in answer['bin_arrays']:
        index=int(array['account']['index'])
        if index in array_indices or array['account']['lbPair'] != state.pool:
            raise ValidationError('SDK duplicate or foreign BinArray')
        array_indices.append(index)
        if len(array['account']['bins']) != 70:
            raise ValidationError('SDK incomplete BinArray')
        for j,b in enumerate(array['account']['bins']):
            own=state.bins.get(index*70+j)
            if own is None:
                raise ValidationError('SDK decoded bin missing from supplied state')
            fields = (('amountX','amount_x'),('amountY','amount_y'),('price','price'),
                      ('liquiditySupply','liquidity_supply'), *lo_fields)
            for sdk_key,own_key in fields:
                if getattr(own, own_key) is not None and int(b[sdk_key])!=getattr(own,own_key):
                    raise ValidationError('SDK bin parity failed: ' + own_key)
            bin_count+=1
    if typed and set(array_indices) != set(state.known_bin_arrays):
        raise ValidationError('SDK raw BinArray coverage differs from supplied state')
    comparisons=[]
    for index, item in enumerate(answer['quotes']):
        intent = intents[index]
        if intent is None:
            request = wire[index]
            q=quote_exact_in(state,int(request['amount_in']),request['swap_for_y'],capture['clock']['unix_timestamp'])
            expected={'consumedInAmount':q.amount_in, 'outAmount':q.amount_out,
                      'fee':q.fee-q.protocol_fee, 'protocolFee':q.protocol_fee}
            detail={'amount_in':q.amount_in,'amount_out':q.amount_out,
                    'total_trader_fee':q.fee,'lp_user_fee':q.fee-q.protocol_fee,
                    'protocol_fee':q.protocol_fee}
        else:
            q=quote_swap(state,intent,capture['clock']['unix_timestamp'],slot=capture['clock']['slot'],
                         bitmap_extension=extension,extension_known=extension_known)
            expected={'outAmount':q.amount_out, 'fee':q.sdk_fee, 'protocolFee':q.sdk_protocol_fee}
            if intent.mode == SwapMode.EXACT_IN:
                expected.update(consumedInAmount=q.amount_in, minOutAmount=q.amount_out)
                if item['quote']['feeOnInput'] != q.fills[0].quote.fee_on_input:
                    raise ValidationError('SDK fee-token mode parity failed')
            else:
                # SDK slippage=0 maxInAmount is the quote amount, not the
                # actual instruction's independently enforced input cap.
                expected.update(inAmount=q.amount_in, maxInAmount=q.amount_in)
            expected_arrays = [next(a['public_key'] for a in answer['bin_arrays']
                                    if int(a['account']['index']) == i)
                               for i in q.reference.visited_bin_arrays]
            if item['quote']['binArraysPubkey'] != expected_arrays:
                raise ValidationError('SDK selected bin-array traversal parity failed')
            detail={'intent':intent.to_request(), 'amount_in':q.amount_in,'amount_out':q.amount_out,
                    'trading_fee':q.trading_fee,'fee_token':q.fee_token,
                    'mm_lp_fee':q.fee_split.mm_lp,'mm_protocol_fee':q.fee_split.mm_protocol,
                    'lo_maker_fee':q.fee_split.lo_maker,'lo_protocol_fee':q.fee_split.lo_protocol,
                    'unallocated_trading_fee':q.fee_split.unallocated_trading_fee,
                    'sdk_user_fee':q.sdk_fee,'sdk_protocol_fee_scalar':q.sdk_protocol_fee,
                    'rounding_output_delta':q.rounding_output_delta,'output_token':q.output_token,
                    'sdk_protocol_rounding_delta':q.sdk_protocol_rounding_delta,
                    'bin_ids':[f.quote.bin_id for f in q.fills],
                    'source_economic_hash':q.reference.source_economic_hash}
        observed={k:int(item['quote'][k]) for k in expected}
        mismatch={k:{'expected':expected[k],'observed':observed[k]}
                  for k in expected if expected[k]!=observed[k]}
        if mismatch:
            raise ValidationError('official SDK quote parity failed: '+json.dumps({
                'request_index':index,'request':wire[index],'mismatch':mismatch},
                sort_keys=True,separators=(',',':')))
        comparisons.append(detail)
    if not comparisons or not bin_count:
        raise Unavailable('empty SDK parity comparison')
    return {'status':'PASS','validation_level':'SDK_PARITY','bins':bin_count,'quotes':comparisons,
            'scope':('MM_LIMIT_ORDER_EXACT_IN_OUT_QUOTE_AND_BIN_DECODE_ONLY' if typed
                     else 'MM_EXACT_IN_QUOTE_AND_BIN_DECODE_ONLY'),
            'state_mutation_parity':'UNAVAILABLE',
            'not_claimed':(['ACCOUNT_STATE_MUTATION','HOST_REFERRAL_FEE_SPLIT',
                            'EXACT_OUT_ROUNDING_ACCOUNT_ALLOCATION','HISTORICAL_REPLAY_PARITY'] if typed else
                          ['LIMIT_ORDER_FILL_AND_STATE_MUTATION','EXACT_OUT','HOST_REFERRAL_FEE_SPLIT',
                           'REWARD_PROTOCOL_BITMAP_COUNTER_MUTATION']),
            'sdk_version':'1.9.14','idl_canonical_sha256':answer['idl_canonical_sha256']}
