# Meteora DLMM Hedged Research — v0.1.0

독립적인 **SOL/USDC DLMM historical-mechanics / inventory-matched-surplus 연구 패키지**입니다.
Uniswap v3 repository를 import, 변경, submodule 처리하지 않습니다. 실거래 주문·서명·자금 전송 기능은 없습니다.

## 먼저 구분할 것

**코드 실행 성공, historical mechanics 검증, 양의 전략 수익은 서로 다른 상태입니다.**

| 계층 | 구현 범위 | 아직 주장하지 않는 것 |
|---|---|---|
| D0 | 실제 LbPair/BinArray/PositionV2 prefix decoder, raw RPC 수집, 상태 replay, 독립 checkpoint 검증 | 모든 프로그램 버전·instruction에 대한 EXACT replay |
| D1 | 기존 MM share 귀속, 원장·benchmark·markout·capacity·4×7일 pilot | 새 유동성을 실제 추가했을 때의 시장 반응, 실증 수익성 |
| D2 | 분석해 및 D1 실제 재고 경로 기반 static short·collateral accounting | 비용 없는 hedge의 실거래 순수익 |
| D3 | 시간별 terms, L2 taker fill, funding, margin, band/boundary/latency 인터페이스 | 실제 주문, queue 증거 없는 maker 체결, 완전한 청산 시뮬레이션 |

이 배포의 **실제 SOL/USDC RPC transaction 검증, historical 4주 수익 검증, 공식 Node SDK 실행 parity 및 LiteSVM 실행은 UNAVAILABLE**입니다. 작성 환경은 네트워크 연결과 해당 입력을 확보하지 못했습니다. 합성 데이터로 전체 경로를 실행하며, 공개 SDK의 실제 계정 fixture도 별도로 검사하지만 **그 fixture는 SOL/USDC가 아닙니다**. 최신 실행 결과는 `validation/authoring_report.json`과 `docs/VALIDATION.md`를 확인하십시오.

## 설치와 첫 실행

Linux/macOS, Python 3.11 이상. Python 3.12 권장. Core는 표준 라이브러리만 사용합니다.

```bash
cd meteora_dlmm_hedged_research
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
./run_pipeline.sh preflight
python -m pytest -q
```

Windows에서는 WSL 또는 Linux 서버에서 shell runner를 사용하십시오. 패키지 CLI는 `mdlmm --root "$PWD" ...`로 호출할 수도 있지만, repository 경로를 확실히 지정하는 `./run_pipeline.sh`가 기본입니다.

### 자격증명 없이 전 계층 연결 확인

```bash
./run_pipeline.sh all --demo
./scripts/extract_all.sh
./scripts/extract_d0.sh
./scripts/extract_d1.sh
./scripts/extract_d2.sh
./scripts/extract_d3_smoke.sh
```

`--demo`는 4×7일, 2 pools, 2 widths, 1,344개 **합성** swap을 생성합니다. D0 `replay_gate=PASS`이더라도 `historical_gate=UNAVAILABLE`이며 D1 `economic_evidence=false`, D3는 연구 gate로 건너뜁니다. 이는 의도한 동작입니다. 기본 생성 자료의 부피와 속도는 시스템에 따라 달라집니다.

### 실제 계정·transaction sample 검사

```bash
export SOLANA_RPC_URL='https://YOUR_ARCHIVAL_SOLANA_RPC_ENDPOINT'
./run_pipeline.sh source-sync
./scripts/install_sdk.sh
./run_pipeline.sh d0
./scripts/extract_d0.sh
```

`source-sync`는 mutable main이 아니라 `reference/source_lock.json`의 정확한 commit/blob을 받습니다. Node.js 20 이상과 npm이 필요합니다. 최초 SDK 설치는 transitive lock을 생성하고 이후 `npm ci`로 재사용합니다. **첫 npm resolve까지 사전에 고정된 전체 lockfile이 배포된 것은 아닙니다.** 실제 package lock과 dependency receipt는 run fingerprint에 연결됩니다.

위 D0 명령은 `data/pilot/dataset.json`이 없으면 현재 pool/account/실제 transaction sample을 수집합니다. 현재 sample이 성공해도 과거 D1 gate가 열리지 않습니다.

### Historical D0 → D1

```bash
./run_pipeline.sh prepare-historical --input data/pilot/archival-plan.json
./run_pipeline.sh d0 --dataset data/pilot/dataset.json
./scripts/extract_d0.sh
./run_pipeline.sh d1 --dataset data/pilot/dataset.json
./scripts/extract_d1.sh
./run_pipeline.sh d2
./scripts/extract_d2.sh
./run_pipeline.sh d3-smoke
./scripts/extract_d3_smoke.sh
```

`archival-plan.json`은 임의 JSON으로 PASS시킬 수 있는 설정 파일이 아닙니다. raw starting/checkpoint account bytes, 해당 slot의 Clock, full transaction blocks, binary/IDL/기간 결합 증거, 외부 SOL reference가 필요합니다. **일반 archival transaction RPC만으로 과거 account state와 Clock이 자동 생성되지는 않습니다.** 필요한 export 계약은 [DATA_CONTRACT](docs/DATA_CONTRACT.md), 실제 명령은 [SERVER_RUNBOOK](docs/SERVER_RUNBOOK.md)에 있습니다. `configs/examples/`는 예시이며 실제 증거가 아닙니다.

## 연구 semantics

D1은 고정된 existing MM liquidity shares를 귀속하는 oracle입니다. Spot은 entry 시 bin당 동일 reference-value 배분으로 정의됩니다. 공식 `addLiquidityByStrategy` 체결을 재현했다고 주장하지 않습니다. 빈 bin에 가상의 기존 share를 만들지 않습니다. Width는 진입 전에 완료된 RV30의 1일 표준편차에 대한 multiplier `[1,2]`로 고정합니다. 최초 진입 후 7일 passive 보유하고 명시한 entry/exit 비용 시나리오를 적용합니다. H1/EV rerange 최적화는 이번 baseline에 없습니다.

핵심 결과:

`inventory_matched_surplus = principal PnL + LP fee effects − explicit LP costs − inventory-matched self-financing benchmark PnL`

SOL로 받은 fee의 이후 가격 손익은 별도 계정이며 benchmark에서도 제거합니다. LP price PnL을 이미 사용한 뒤 LVR를 다시 차감하지 않습니다. `realized/unrealized`는 명시한 SOL weighted-average cost basis이며 HODL 대비 IL과 다릅니다.

기본 `configs/research.toml`의 비용은 **illustrative scenario**입니다. `cost_evidence_mode=SCENARIO_NOT_HISTORICAL`이므로 양의 합계가 나와도 D3 연구 gate를 열지 않습니다. 문서화된 보수적 비용 bounds를 사용하려면 설정과 일치하는 source-linked JSON 및 SHA256이 필요합니다.

## Evidence levels / 종료 코드

`EXACT`, `SDK_PARITY`, `ACCOUNT_PARITY`, `PARTIAL_RECONSTRUCTION`, `UNVERIFIED`는 허용된 이름입니다. 이 버전은 full historical `EXACT`를 자동 부여하지 않습니다. 직접 replay의 `ACCOUNT_PARITY` 범위는 MM reserve/supply/price, active bin, fee parameters/reference state이며 oracle/reward/full bytes/fee-growth rounding은 제외됩니다.

| exit code | 의미 |
|---:|---|
| 0 | 해당 scope의 PASS |
| 2 | FAIL: 불일치, 잘못된 입력, 내부 오류 |
| 3 | UNAVAILABLE: 입력/의존성/증거/gate 부족 |

`all`은 D0가 통과하지 않아도 나머지 단계의 차단 사유와 D2 분석 oracle을 기록하지만, 경제 결과를 만들어내지 않습니다. D3 gate skip은 `all`의 필수 실행 실패가 아닙니다.

## 파일 구조

```text
configs/             고정 정책, illustrative 비용, 입력 예시
src/mdlmm/           decoder / mechanics / replay / ledger / pilot / perp
scripts/             stage extractors, frozen SDK oracle
reference/           upstream commit/blob lock
schemas/             interchange JSON schemas (추가 semantic checks는 Python)
tests/               단위·회귀·합성 통합·공개 계정 fixture
validation/          작성 환경의 실제 테스트·실행 receipt
runs/<stage>_<id>/   새 immutable manifests와 결과 (Git 제외)
data/raw/            내용 주소 기반 raw source와 receipt (Git 제외)
external/            검증된 공식 소스 다운로드 (Git 제외)
```

각 manifest는 Git SHA, code/config/schema/test fingerprint, config, software, source lock, data hashes, pool/program/reference/time range, parent manifest, artifact hashes를 담습니다. Run 중 source가 바뀌면 FAIL입니다. API credential은 manifest에 기록하지 않습니다. `*_latest.json`은 이동 가능한 pointer이고, 각 run manifest와 payload는 덮어쓰지 않습니다.

자세한 설명: [Architecture](docs/ARCHITECTURE.md), [Research specification](docs/RESEARCH_SPEC.md), [Source audit](docs/SOURCE_AUDIT.md), [Validation](docs/VALIDATION.md).
