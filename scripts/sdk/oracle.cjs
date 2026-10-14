/* Official SDK oracle, no network calls and no real transaction submission. */
'use strict';
const fs = require('fs');
const crypto = require('crypto');
const path = require('path');
function normalize(x) {
  if (typeof x === 'bigint') return x.toString();
  if (x && typeof x.toBase58 === 'function') return x.toBase58();
  if (x && x.constructor && x.constructor.name === 'BN') return x.toString(10);
  if (Buffer.isBuffer(x)) return x.toString('hex');
  if (Array.isArray(x)) return x.map(normalize);
  if (x && typeof x === 'object') return Object.fromEntries(Object.keys(x).sort().map(k => [k,normalize(x[k])]));
  return x;
}
async function main() {
  const pkg = require('@meteora-ag/dlmm');
  let pkgDir=path.dirname(require.resolve('@meteora-ag/dlmm'));
  while (!fs.existsSync(path.join(pkgDir,'package.json'))) {
    const parent=path.dirname(pkgDir);
    if (parent===pkgDir) throw new Error('UNAVAILABLE: npm package identity missing');
    pkgDir=parent;
  }
  const installed=JSON.parse(fs.readFileSync(path.join(pkgDir,'package.json'),'utf8'));
  if (installed.name!=='@meteora-ag/dlmm' || installed.version!=='1.9.14')
    throw new Error('official SDK version mismatch');
  const {PublicKey} = require('@solana/web3.js');
  const BN = require('bn.js');
  const DLMM =
    (pkg && typeof pkg.create === 'function')
      ? pkg
      : (pkg.default?.default || pkg.default || pkg.DLMM);
  if (!DLMM || typeof DLMM.create !== 'function') throw new Error('UNAVAILABLE: unsupported official SDK export');
  const capture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const requests = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const raw = capture.raw_accounts;
  if (capture.bitmap_extension_address) {
    const [expected]=PublicKey.findProgramAddressSync(
      [Buffer.from('bitmap'),new PublicKey(capture.pool).toBuffer()],new PublicKey(capture.state.program_id));
    if (expected.toBase58()!==capture.bitmap_extension_address) throw new Error('PDA parity failed');
  }
  if (!raw || !capture.clock) throw new Error('UNAVAILABLE: raw snapshot/Clock missing');
  const absent = new Set(capture.absent_accounts || []);
  absent.add('11111111111111111111111111111111'); // zero-key inactive rewards
  const get = (pubkey) => {
    const key = pubkey.toBase58();
    if (!(key in raw)) {
      if (absent.has(key)) return null;
      throw new Error('UNAVAILABLE: frozen snapshot missing requested account ' + key);
    }
    const r=raw[key];
    if (r === null) return null;
    if (r.data[1] !== 'base64') throw new Error('UNAVAILABLE: unsupported encoding');
    return {data:Buffer.from(r.data[0], 'base64'),lamports:r.lamports,
            executable:r.executable,owner:new PublicKey(r.owner),rentEpoch:r.rentEpoch};
  };
  // Proxy denies ALL methods except local, deterministic data access. The SDK
  // cannot quietly fetch a newer account or time while computing a frozen quote.
  const allowed = {
    commitment:'finalized',rpcEndpoint:'http://offline-frozen.invalid',
    getMultipleAccountsInfo: async(keys)=>keys.map(get),
    getMultipleAccountsInfoAndContext:async(keys)=>({context:{slot:capture.slot},value:keys.map(get)}),
    getAccountInfo:async(key)=>get(key),
    getAccountInfoAndContext:async(key)=>({context:{slot:capture.slot},value:get(key)}),
    getSlot:async()=>capture.slot,
    getBlockTime:async()=>capture.clock.unix_timestamp,
  };
  const connection = new Proxy(allowed,{get:(target,key)=>{
    if (key in target) return target[key];
    throw new Error('UNAVAILABLE: SDK requested unimplemented offline method '+String(key));
  }});
  const oldNow=Date.now;
  Date.now=()=>Number(capture.clock.unix_timestamp)*1000;
  try {
    const dlmm=await DLMM.create(connection,new PublicKey(capture.pool));
    const arrays=[];
    const binDisc=crypto.createHash('sha256').update('account:BinArray').digest().subarray(0,8);
    for (const [address,r] of Object.entries(raw)) {
      if (r===null || r.owner!==capture.state.program_id) continue;
      const bytes=Buffer.from(r.data[0],'base64');
      if (bytes.subarray(0,8).equals(binDisc)) arrays.push({publicKey:new PublicKey(address),account:dlmm.program.coder.accounts.decode('binArray',bytes)});
    }
    const outputs=[];
    const amount = (value, name) => {
      if (typeof value === 'number' && Number.isSafeInteger(value)) value=String(value);
      if (typeof value !== 'string' || !/^[0-9]{1,20}$/.test(value))
        throw new Error('invalid integer wire amount '+name);
      const result=new BN(value,10);
      if (result.gt(new BN('18446744073709551615'))) throw new Error('u64 overflow '+name);
      if (name!=='min_amount_out' && result.isZero()) throw new Error('zero-size swap constraint '+name);
      return result;
    };
    for (const [requestIndex,q] of requests.entries()) {
      if (typeof q.swap_for_y !== 'boolean') throw new Error('swap direction must be boolean');
      const typed=Object.prototype.hasOwnProperty.call(q,'mode');
      if (typed && q.host_fee_present !== false)
        throw new Error('UNAVAILABLE: pinned SDK quote has no host-account argument');
      const expectedKeys=typed
        ? (q.mode==='EXACT_IN'
            ? ['mode','swap_for_y','host_fee_present','amount_in','min_amount_out']
            : q.mode==='EXACT_OUT'
              ? ['mode','swap_for_y','host_fee_present','out_amount','max_in_amount'] : [])
        : ['amount_in','swap_for_y'];
      if (Object.keys(q).sort().join(',')!==expectedKeys.sort().join(','))
        throw new Error('unsupported or incomplete quote request');
      const out=(typed && q.mode==='EXACT_OUT')
        ? dlmm.swapQuoteExactOut(amount(q.out_amount,'out_amount'),q.swap_for_y,new BN(0),arrays)
        : dlmm.swapQuote(amount(q.amount_in,'amount_in'),q.swap_for_y,new BN(0),arrays,false);
      if (typed && q.mode==='EXACT_OUT' && out.inAmount.gt(amount(q.max_in_amount,'max_in_amount')))
        throw new Error('SDK quote violates actual maximum input constraint');
      if (typed && q.mode==='EXACT_IN' && out.outAmount.lt(amount(q.min_amount_out,'min_amount_out')))
        throw new Error('SDK quote violates actual minimum output constraint');
      outputs.push({request_index:requestIndex,request:q,quote:normalize(out)});
    }
    const npmIdlHash=crypto.createHash('sha256').update(JSON.stringify(normalize(pkg.IDL))).digest('hex');
    console.log(JSON.stringify({status:'PASS',sdk_version:installed.version,idl_canonical_sha256:npmIdlHash,
       pair:normalize(dlmm.lbPair),bin_arrays:arrays.map(x=>({public_key:x.publicKey.toBase58(),account:normalize(x.account)})),
       bitmap_extension:normalize(dlmm.binArrayBitmapExtension),
       token_mints:{x:normalize(dlmm.tokenX.mint),y:normalize(dlmm.tokenY.mint)},
       quotes:outputs,clock:normalize(dlmm.clock),scope:'OFFICIAL_SDK_ON_EXACT_FROZEN_ACCOUNT_BYTES'}));
  } finally {Date.now=oldNow;}
}
main().catch(e=>{console.log(JSON.stringify({status:String(e).includes('UNAVAILABLE:')?'UNAVAILABLE':'FAIL',reason:String(e)}));process.exitCode=3;});
