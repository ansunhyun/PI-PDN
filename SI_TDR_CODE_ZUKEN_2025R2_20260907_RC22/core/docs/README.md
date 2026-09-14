LG SI-TDR FullBatch 사용자 안내
===============================

상태: 2026-08-15 FullBatch provisional customer contract

이 코드 패키지는 Heaven/EDEN이 DCG Job에 staging한 외부 요청과 설계 입력을
사용하여 SIWave SYZ, AEDT Circuit TDR, PCB Capture 및 최종 결과 publish를
한 번에 수행합니다. 설정 항목은 다음 문서에서 확인하십시오.

  core\docs\SI-TDR_User_Config_Guide.html


1. 코드 패키지 구조
-------------------

<CODE_ROOT>/
  main.py                         유일한 root Python entrypoint
  requirements.txt
  core/
    config.json                   SI-TDR 관리자 Config
    runtime.py
    channel/
    preprocess/
    docs/
      README.md
      SI-TDR_User_Config_Guide.html

고객 code ZIP에는 `sample/run.json`이나 `sample/channel.csv`가 없습니다.
저장소의 `SI_TDR/sample/run.json`과 `channel.csv`는 개발/회귀 fixture이며 고객
제품 실행 입력이 아닙니다. 고객 전달 Case는 각 Case의 README와
VALIDATION-SUMMARY를 따르십시오.


2. EDEN Job 준비
----------------

Heaven이 생성하는 외부 요청 JSON은 DCIR과 같은 Request/CAE 포맷입니다.
파일명은 `input.json`, `run.json` 등으로 고정하지 않습니다. EDEN은 다음
폴더들을 파일 하나가 아니라 폴더 단위로 DCG Job에 staging합니다.

<JOB>/
  <임의-이름-request>.json
  Spec/                         Spec file CSV 모음
  Stackup/                      STK 모음
  cadFile/                      Zuken PCB/ZIP 또는 ANF/CMP 입력
  BOM/                          CSV/XLS/XLSX 모음
  Setup/
    SWS/                        SIWave option 파일
    SFSDF/                      SYZ frequency definition 파일
    TDR/                        TDR option JSON

외부 요청의 다음 값이 staged 파일을 정확히 선택합니다.

  CAE.SOC.Spec                  Spec file 이름
  CAE.PCB.Stackup               Stackup 이름
  CAE.PCB.cadFile               CAD/ANF 입력 이름
  CAE.PCB.BOM                   BOM 이름

Spec file 이름은 `channel.csv`나 `spec.csv`로 고정하지 않습니다. 프로그램은
외부 요청 JSON의 parent를 Job root로 사용하고 Job-local 파일만 resolve합니다.
중앙 저장소 fallback은 사용하지 않으며 resolved path와 SHA-256을 실행
snapshot과 manifest에 기록합니다.

`core\config.json`은 관리자가 소유합니다. 다음 항목을 확인하십시오.

  aedtVersion                     2025.2
  isZuken                         true면 Zuken design data 입력
  DF_path / export*               Zuken 실행 환경
  preprocessing.dcShort           DCIR과 동일한 고정 정책
  BOM.schemaVersion              2
  BOM.colKey                     Designator
  BOM.arrayResistanceSourceColumns Site Specification > Technical Spec > Description
  analysisOptions.defaults / analysisOptions.syz / analysisOptions.tdr
  tdrReport.yAxisMarginOhm        Spec Target 기준 여유값, 기본 30 ohm

SYZ Profile의 `.sfsdf`는 frequency row/grid를 선택합니다. `sweepMode`,
interpolating의 `convergence/maxInterpPts`, Exact DC, Causality, Passivity는
같은 관리자 SYZ Profile에 명시하며 외부 Heaven JSON에는 넣지 않습니다.

외부 Heaven JSON을 수정하여 관리자 값을 추가하지 마십시오.


3. 기본 FullBatch 실행
---------------------

EDEN에 DCIR과 호환되는 Python 실행 경로를 등록한 뒤 code root에서 실행합니다.

  python.exe main.py <JOB>\<임의-이름-request>.json

이 한 줄이 다음 순서를 수행합니다.

  외부 입력/Profile resolve
  -> Reference SIW/AEDB 전처리
  -> strict per-sNp Batch Config와 Channel Path/Port 생성
  -> SIWave SYZ, exact sNp, 최종 SIW/SIWZ
  -> AEDT Circuit TDR, native Report JPG, Tdr_waveform.csv
  -> Job당 PCB 전체 top.png/bottom.png + sNp Batch별 route.png
  -> final evidence/JSON/artifact 검증
  -> atomic outputs publish

요청 파일명은 고정하지 않습니다. `main.py` 뒤에 전달한 실제 JSON 경로가
실행 요청입니다.


4. 보조 진단 명령
-----------------

라이선스를 사용하지 않고 staged 입력과 Profile만 확인합니다.

  python.exe main.py <JOB>\<임의-이름-request>.json --preflight-only

Zuken 또는 ANF/CMP부터 reference SIW/AEDB까지만 smoke합니다.

  python.exe main.py <JOB>\<임의-이름-request>.json --prepare-reference-only

두 명령은 진단/smoke용이며 FullBatch 성공을 뜻하지 않습니다. 실제 제품
결과를 만들 때는 옵션 없는 기본 FullBatch 명령을 사용합니다.


5. Spec file과 Profile 선택
---------------------------

Spec file은 `CAE.SOC.Spec`가 선택한 staged CSV입니다. 한 Differential Channel은
인접한 P/M 두 행으로 작성하고 다음 조합이 하나의 sNp Batch가 됩니다.

  Function + Version + Designator + Group + Direction

`Group`은 AEDT native TDR Report 제목이고 `Channel_Name`은 개별 Trace 이름입니다.
Target/Min/Max는 Spec file이 진실원입니다.

현재 내부 계약은 다음 optional Spec 컬럼을 지원합니다.

  SYZ_Option                     sNp/SYZ Batch의 관리자 Profile ID
  TDR_Option                     TDR item/report group의 관리자 Profile ID

같은 해석 단위 안의 선택값은 일치해야 합니다. 빈 값은 명시된 관리자 기본
Profile을 사용합니다. 이 두 컬럼을 고객 최종 UI/Spec에 노출할지는 고객
feedback 후 확정합니다.

일반 Channel Path Series R/C/L은 종류와 값에 관계없이 Short 처리합니다.
RefDes가 `AR`로 시작하면 BOM/Part List에서 우선순위로 파일 단위 선택한 column의 명시적 OHM 값을
저항으로 사용합니다. Pin Pair는 실제 EDB가 4핀이면 `1-4/2-3`, 8핀이면
`1-8/2-7/3-6/4-5`로 자동 구성합니다. CMP 모델과 무관하게 이 R-only 모델을
재적용하고, 저장 후 다시 연 AEDB의 전체 Pair/RLC 상태를 확인합니다. BOM path,
size, mtime, SHA-256과 선택 row/token이 실행 증거에 고정됩니다.


6. FullBatch 성공과 결과
------------------------

FullBatch 성공은 다음을 모두 만족한 경우뿐입니다.

  모든 요청 Batch 성공
  sNp/SIW/SIWZ 및 AEDT project/archive/results 내부 증거 검증
  native Report JPG, Tdr_waveform.csv, top.png/bottom.png 검증
  Web JSON 5종과 내부 상대경로/allowlist 검증
  별도 publish staging 검증
  actual device/volume 확인 후 최종 outputs atomic publish
  rename 후 final tree 재검증과 terminal published manifest
  process exit code 0

현재 EDEN-05 provisional `/3` public outputs는 다음 파일만 포함합니다.

<JOB>\outputs\
  title.json
  request.json
  setting.json
  result_detail.json
  result.json
  top.png
  bottom.png
  <ref>.siw
  <ref>.aedb\...
  <Batch>\<Batch>_TDR.jpg
  <Batch>\<Batch>_TDR.csv
  <Batch>\<Batch>_PCB_Capture.png
  <Batch>\<Batch>_Array_Model_Result.json  (Array 적용 Batch에만 생성)
  <Batch>\touchstone\<Batch>.s{N}p
  <Batch>\<Batch>.siw
  <Batch>\<Batch>.siwaveresults\...
  <Batch>\<Batch>.siwz
  <Batch>\<Batch>.aedt
  <Batch>\<Batch>.aedb\...          (AEDT Circuit 프로젝트 자기 DB)
  <Batch>\<Batch>.aedtresults\...
  <Batch>\<Batch>.aedtz

공개 sNp/SIWZ/AEDB/AEDT/AEDT results/AEDTZ는 strict completion evidence의 exact
Job-contained source만 사용하고 file 또는 recursive tree manifest를 staging copy와
대조합니다. Generated Run Config, completion record, PCB capture evidence,
temp/lock/cache는 계속 `work\` 내부에만 둡니다. Web JSON은 EDEN-06 전까지
Batch JPG/CSV/PNG와 top/bottom만 참조하고 reproduction artifact는 publish manifest의
별도 category로 추적합니다.

입력, Batch, solver, capture 또는 publish가 실패하면 non-zero로 종료하고 새
outputs를 공개하지 않습니다. 실행 전에 `<JOB>\outputs`가 이미 있으면 자동
삭제나 덮어쓰기 없이 충돌 실패하며 기존 outputs를 보존합니다.
Copy/export/validation 실패는 attempt manifest와 partial staging을 보존합니다.
Rename 후 final 검증 실패 tree는 `work\publish_attempts\<id>\failed_published_tree\`로
same-volume quarantine합니다. Quarantine 실패 시 outputs를 삭제하지 않고
`unsafeExposedOutputs=true`와 non-zero를 남기므로 성공 결과로 회수하면 안 됩니다.
Live auditor는 정확히 하나의 terminal `published` manifest만 성공 후보로 인정합니다.

TDR Pass/Fail 판정 규칙은 아직 확정되지 않았으므로 결과는
`not_evaluated`로 기록합니다.


7. 로그와 오류 확인
-------------------

기본 로그는 외부 요청 JSON과 같은 Job의 `logs\`에 생성됩니다.

  SI_TDR_<timestamp>.log
  SI_TDR_<timestamp>_progress.log

먼저 progress 로그의 마지막 FAIL 단계와 process exit code를 확인한 뒤 같은
시각의 전체 로그를 확인하십시오. 내부 record와 directory 생성 위치는 다음
경로에서 확인합니다.

  <JOB>\work\reference_preprocess\
  <JOB>\work\fullbatch\
  <JOB>\work\publish_attempts\
  <JOB>\work\fullbatch_orchestration\directory_snapshots.json

프로그램은 누락된 Pin, Net, Profile, 부품값이나 결과 파일을 임의로 추정하여
성공 처리하지 않습니다.


8. 현재 고객 환경 확인 항목
----------------------------

- 고객 원본 SFSDF와 SIWave 2025.2 parser-command/grid 적용 증거
- same-layer Reference/GND 자동 판별
- PCB top/bottom 캡처의 2025.2 가독성·clip/overlap visual sidecar (배너 없음)
- path component-only visibility는 이번 release의 non-blocking follow-up
- 고객 Zuken Design Force 변환
- EDEN/DCG folder staging 및 결과 회수
- EDEN-04 공개 artifact의 실제 2025.2 restore 및 EDEN-06 outputs/result mapping Gate

Pre/Local/Post와 HFSS workflow는 현재 고객 FullBatch CLI가 아니며 FullBatch
live Gate 이후 별도로 검토합니다.
