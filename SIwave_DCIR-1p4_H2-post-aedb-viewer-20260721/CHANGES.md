# SIwave_DCIR-1p4_H2 Pre/Post 분리 변경 사항

- 기준 버전: SIwave_DCIR-1p4_H2 (2026-07-03 수령본)
- 이전 전달 기준: SIwave_DCIR-1p4_H2-presplit (2026-07-04)
- 이번 변경 파일: `main.py`, `core/post_stage.py`, `core/post_processing.py`, `tests/test_post_stage.py`, `README.md`, `CHANGES.md`

## 5차 변경 (2026-07-17): 최신 Local SIW 기반 Post AEDB/Viewer 재생성

- Pre의 case별 `export_edb`를 제거했습니다. Pre는 case SIW와 schema 3 `preprocessing_result.json`만 만들며, manifest의 `Edb_Path`/`Edb_Folder`는 Post target contract로 유지합니다.
- Post는 각 case의 최신 완료 `NNNN[_simulation-name]/NNNN.siw`를 열어 기존 `outputs/<case>.aedb`를 제거하고 신규 export합니다.
- `edb.def` 생성 timeout, SIWave export, AEDT import/solve, Field/Mesh/FitView/ZoomView 누락을 오류로 처리합니다. 부분 AEDB와 stale Viewer 산출물이 성공 결과로 재사용되지 않습니다.
- PyEDB가 case AEDB를 닫은 뒤 AEDT가 같은 AEDB를 import하도록 순서를 수정했습니다.
- 모든 case AEDB를 먼저 export하고 SIWave를 닫은 뒤 AEDT Viewer 단계로 진입하도록 2단계화했습니다. 연속 Full/Post 회귀에서 확인된 SIWave/AEDT 동시 COM/license stall을 제거합니다.
- Local SIW의 V/I·Source가 신규 AEDB와 AEDT Viewer용 DCIR 재해석에 반영됩니다.
- FullBatch의 중복 후처리 블록을 제거하고 독립 Post와 같은 `run_standalone_post` 파이프라인을 호출합니다.
- `result_detail.json`에 `changeHistory`와 `postInfo`를 기록합니다. `postInfo`에는 최신 Local SIW 사용, 산출물 소유권, case별 EDB/Viewer 상태가 포함됩니다.
- FullBatch가 Viewer 생성 전에 JSON을 써서 점이 포함된 net의 실제 파일명과 JSON 참조가 달라지던 문제도 공통 Post 경로로 통합해 해결했습니다.

### 5차 변경 검증

- 순수 Python: 단위 테스트 13건, 전체 `compileall` 통과.
- fresh Pre: 5개 manifest record와 case SIW 5개 생성, case AEDB 0개, `Project_File=Pre`, `Edb_Folder=Post` 확인.
- 독립 Post: 5/5 case, 신규 AEDB 5개, Viewer 필수 파일 20/20개, 기존 기준 수치 불일치 0건.
- Local delta: D1V0 최신 SIW를 `I 6.38 -> 6.50`, `V 1.05 -> 1.08`로 변경한 뒤 신규 AEDB source 값과 Field 전압 범위 변경을 확인.
- FullBatch와 그 결과의 Post 재실행을 실제 수행하여 stale AEDB/Viewer 교체, 5/5 `viewerArtifacts` 완료, JSON 경로 20/20개 존재를 확인.
- fresh Full의 완료 SIWave 결과에 최종 2단계 Post를 재개해 511.7초, 5/5 case, 수치 불일치 0, 최종 로그 오류 0을 확인.
- 상세 명령, test-only config, 출력 경로, 비교 기준은 `../../docs/customer-samples/dcir-post-aedb-viewer-regression-2026-07-17.md`에 기록했습니다.

## 4차 변경 (2026-07-13): 독립 Post 결과 감지 진단 보강

- 고객 환경에서 Post가 Local 완료 결과를 찾지 못하고 `0/5 cases`로 종료된 상황에 대응했습니다.
- batch의 숫자 회차 폴더(`0000`)와 Local GUI의 simulation 이름 포함 회차 폴더(`0000_DCIR - ...`)를 모두 인식하도록 결과 탐색을 보완했습니다.
- 회차 폴더 내부 파일명이 표준 `NNNN.*`와 다르더라도 확장자별 파일이 하나이면 해당 파일을 사용합니다.
- 각 case의 결과 감지 성공 여부, 최신 회차, 실패 원인, 조회한 `.siwaveresults` 경로를 실행 로그에 출력합니다.
- `result_detail.json/changeHistory`에도 조회한 프로젝트 파일과 결과 폴더를 기록합니다.
- 완료 결과가 0건이면 불필요하게 AEDT를 실행하지 않고, JSON과 상세 진단 기록을 저장한 뒤 Post 실패로 종료합니다.
- 이번 변경은 진단 보강입니다. 고객 결과 구조에 대한 호환 수정은 `changeHistory[].Error`와 실제 결과 폴더 구조 확인 후 반영합니다.
- 순수 Python 상태 복원 테스트 6건 및 전체 Python 문법 검사를 통과했습니다.

## 3차 변경 (2026-07-10): 독립 Post 1차 분리

- 실행 인자에 `--stage post`를 추가했습니다.
- Post는 Pre/CAD 변환을 다시 실행하지 않고 `outputs/preprocessing_result.json`에서 case 상태를 복원합니다.
- 각 case의 `.siwaveresults/NNNN/` 중 `.finished`, `.siw`, `.ced`가 모두 있는 최신 완료 회차를 사용합니다.
- 최신 회차 SIW에서 실제 V/I 설정을 읽고 CED에서 결과 전압을 읽어 기존 summary와 Pass/Fail을 재구성합니다.
- 기존 Web 파일(`title.json`, `request.json`, `result.json`, `setting.json`, `result_detail.json`) 형식을 유지합니다.
- `result_detail.json`에는 Pre 대비 V/I 변경 및 회차 이력을 `changeHistory`로 추가합니다.
- Pre/CAD 변환과 SIWave DCIR 해석은 다시 수행하지 않습니다.
- Viewer 생성을 위해 Pre 단계에서 export한 case AEDB를 AEDT/HFSS 3D Layout으로 Import하고 Viewer용 해석을 새로 수행합니다. 최신 Local V/I는 이 Viewer용 해석에 다시 적용하지 않으며, 고객 확인 후 후속 적용합니다.
- Pre manifest에 상대 파일명, EDB 경로, IC/DCDC pin, GND net을 추가했습니다. 기존 manifest도 파일 basename과 CED 정보로 읽을 수 있습니다.
- 순수 Python 상태 복원 테스트 5건을 추가했습니다.

### 3차 변경 검증

- 환경: 로컬 AEDT/SIWave 2024 R2, PyAEDT 0.17.2
- 입력: `DCIR/test-run/input-pre/55UB85.json`과 Pre/Local 산출물 5 case
- 명령: `python main.py target.json --stage post`
- 결과: 5/5 case 완료, 최신 회차 선택(D1V0=`0001`, 나머지=`0000`), FullBatch 수치 5/5 일치
- Viewer: FitView/ZoomView/Field/Mesh 참조 20개 모두 생성 및 JSON 경로 일치
- 총 실행 시간: 401.4초
- 고객 실행 설정은 `core/config.json`의 AEDT 2025.2를 유지했습니다. 검증용 복사본에서만 2024.2로 변경하여 실행했습니다.

## 2차 변경 (2026-07-07): 해석 실패 시 진단 로그 보강

`--stage full` 해석 실패(returncode 4294967295) 보고에 대응하여, `siwave_ng.exe` 실행이 실패한 경우 아래 정보를 로그에 남기도록 보강했습니다. 동작 변경은 없으며 실패 시 로그 출력만 추가됩니다.

- 실행한 전체 명령행 (동일 조건 수동 재현용)
- returncode
- siwave_ng.exe의 stdout / stderr 출력 (라이선스 오류 등 solver 자체 메시지)

위 로그는 **실패한 경우(returncode ≠ 0)에만** 남기며, 실패 시에는 solver 출력 전체를 기록합니다. 정상 해석 시에는 기존과 동일하게 아무것도 출력하지 않습니다. (1.4.1에도 solver 출력 로깅 코드가 있으나 주석 처리되어 있는데, 성공 시에도 매 케이스 solver 진행 출력 전체가 로그에 쌓이는 구조였기 때문으로 추정됩니다. 실패 시로 한정하여 이 우려 없이 진단 정보를 확보합니다.)

수정 위치: `run_dcir_case()`의 solve 실행 블록 1곳.

## 1차 변경 (2026-07-04): Pre 분리

### 변경 내용

실행 인자에 `--stage`를 추가하여 Pre 구간만 수행할 수 있도록 분리했습니다.

| 실행 | 동작 |
| --- | --- |
| `python main.py target.json` | 기존과 동일 (전체 수행, `--stage full` 기본값) |
| `python main.py target.json --stage pre` | 해석 Run 직전까지 수행 후 종료 |

`--stage pre` 실행 시:

- Step 0~5 (초기화, 설정, ECAD 변환, EDB/SIWave 수정, ref 생성)는 기존과 동일하게 수행됩니다.
- Step 6에서 case별 SIW 생성(소스 배치, simulation name 설정 포함)까지 수행하고, `siwave_ng.exe` 해석 실행과 결과 추출은 건너뜁니다. 이 당시의 case AEDB 생성은 5차 변경에서 Post로 이동했습니다.
- case 정보는 기존과 동일하게 `outputs/preprocessing_result.json`으로 출력됩니다.
- Step 8 Post-Processing은 수행하지 않습니다.

### 수정 위치 (main.py)

1. argparse에 `--stage {pre, full}` 인자 추가 (Initialize 구간)
2. `run_dcir_case()`에 `run_solve` 파라미터 추가, solve/결과 추출 블록을 `if run_solve:`로 게이트
3. Step 6 호출부에서 `run_solve=(STAGE != "pre")` 전달
4. Step 8 Post-Processing을 stage로 게이트

### 검증 방법 제안

1. 기존 sample 입력으로 `--stage full` 실행 → 기존 버전과 동일하게 동작하는지 확인 (회귀)
2. 동일 입력으로 `--stage pre` 실행 → `outputs/`에 case별 SIW와 `preprocessing_result.json`이 생성되고 해석 및 case AEDB 생성은 수행되지 않는지 확인
3. pre 산출물 siw를 SIWave에서 열어 소스 배치/시뮬레이션 설정 상태 확인
