# Ansys SIwave PDN Automation

LGE MS Minerva용 AC PDN 임피던스 자동화 도구입니다. CAD와 BOM, Spec, InnerCap 입력을 검증하고 EDB/SIwave 모델을 구성한 뒤 Port와 VRM을 생성하여 PDN 해석 및 Touchstone 기반 후처리를 수행합니다.

## 실행 환경

- OS: Windows 11
- Python: 3.11
- 기준 AEDT/SIwave: 2025 R2
- 버전 입력: 2024 R1 이후 형식을 지원하며 2026 R1도 선택 가능
- 주요 라이브러리: PyAEDT 0.17.2, PyEDB 0.50.1, scikit-rf 1.7.0

AEDT 버전은 `core/config.json`의 `PDN.version`에서 설정합니다.

```json
{
  "PDN": {
    "version": "2025.2"
  }
}
```

## V2 전처리 S2P·RLC 생성

`pdn_s2p_core/`에 `Pmap_Gerneration/PDN_S2P_Core`의 알고리즘과 사양 근거 파일을 포함했다. 별도 Pmap 앱 실행이나 원본 폴더의 Python 설치 없이 현재 PDN 프로그램에서 호출한다. 원본 추적은 `PDN_S2P_SOURCE_MANIFEST.json`, 코어 상세 설명은 `PDN_S2P_CORE_README.md`를 참고한다. NumPy는 코어의 검증 환경에 맞춰 `1.26.4`를 사용한다.

Step 5의 모델 적용 전에, BOM/InnerCap에 등록되고 해석 전원 Net과 GND 사이에 연결된 커패시터만 처리한다. BOM의 Technical Spec과 제조사 정보, InnerCap의 품번·용량을 전달하며 동일 품번의 모든 위치를 매핑에 기록한다. 기존 라이브러리의 공식 S2P/RLC 쌍을 우선 사용하고, 없으면 제조사 확보·RLC 정규화·품질 검사·사양 대체 선정을 수행한다. 생성 결과의 위치별 실제 S2P 경로를 해석기에 적용하므로 기존 고정 PN 별칭으로 생성 결과를 바꾸지 않는다.

제조사 입력은 코어의 기존 제조사 판정 규칙으로 정규화한다. 예를 들어 `Taiyo Yuden`은 `TaiyoYuden`, `murata`는 `Murata`로 전달하며 원래 표기는 매핑의 `bomManufacturerText`에 보존한다. 같은 정규화는 배치 API를 직접 호출할 때도 적용한다. 실제로 다른 제조사를 지정한 경우에는 원부품 사양 충돌 검사를 유지한다.

RLC 파일은 모델 검증과 출처 추적용으로 함께 저장한다. PDN 해석에는 대응 S2P를 적용하며 두 모델을 동시에 중복 적용하지 않는다. 코어가 근사 RLC를 생성하는 경우에도 출처의 근사 표시를 유지한다. 모델 선정은 보드 PDN 통과를 의미하지 않으며 이후 기존 Full 해석·후처리를 수행한다.

```json
{
  "PDN": {
    "sParameter": {
      "enableAssign": true,
      "generation": {
        "enabled": true,
        "allowNetwork": true,
        "requireAllModels": true,
        "libraryDirs": [],
        "runtimeDirs": [
          "C:\\Program Files\\ANSYS Inc\\v252\\AnsysEM\\complib\\Unlocked\\s2p_file",
          "C:\\Program Files\\ANSYS Inc\\v261\\AnsysEM\\complib\\Unlocked\\s2p_file"
        ],
        "requireRuntimeCopies": true
      }
    }
  }
}
```

- `allowNetwork=false`: 기존 공식 저장 모델만 사용한다.
- `libraryDirs`: 추가 공식 모델 라이브러리. 상대 경로는 입력 JSON의 폴더 기준이다. 기존 `s2pDirectory`와 `model_library_dir_candidates`도 함께 전달한다. 검증용 fixture는 명시적으로 지정한 경우에만 코어 입력으로 사용한다.
- `runtimeDirs`: 모델을 복사하고 이후 실행에서 재사용할 Ansys 라이브러리. S2P·RLC·출처·원본 회로·사양 정보를 `<라이브러리>/<선택품번>/`에 저장한다. 복사본의 SHA를 확인한 뒤 첫 번째 성공한 라이브러리의 S2P를 해석에 적용한다. 기본 순서는 v252, v261이다.
- `requireRuntimeCopies=true`: 두 라이브러리 중 하나라도 복사에 실패하면 solver 실행 전에 중단한다. Program Files에 쓰기 권한이 없으면 해당 권한으로 실행해야 한다. 실패 경로와 사유는 `runtimeCopyFailures`에 기록한다.
- 기존 라이브러리 폴더와 내용이 같은 파일은 수정하지 않는다. S2P·RLC·출처 파일은 크기와 SHA-256으로 비교하고, JSON·CSV 매핑은 생성할 내용과 비교한다. 달라진 파일과 누락된 파일만 복사·저장한다. 같은 품번을 여러 부품이 선택해도 모델 파일은 한 번만 처리한다. 관계없는 기존 파일은 유지한다.
- `runtimeSyncStats`: 두 라이브러리 동기화의 실제 소요 초(`elapsedSeconds`), 복사·생략 파일 수, 매핑 갱신·생략 수를 기록한다. 같은 정보는 `[S2P_GENERATION][LIBRARY_SYNC]` 로그에서도 확인할 수 있다. 코어의 모델 선정 알고리즘은 유지하며 매핑 저장 함수에 동일 내용 보존 옵션을 추가했다.
- `requireAllModels=true`: 대상 모델이 없거나 모델 적용이 실패하면 solver 실행 전에 중단한다. 보고서에서 누락·대체·품질 검사 근거를 확인한다. `false`는 미확보 부품의 기존 전기 모델을 유지하는 명시적 예외이며 보고서에 `Skipped`로 남는다.
- `generation.enabled=false`: 기존 라이브러리 검색·별칭 적용 흐름을 사용한다.
- `originalSpecificationCatalog`: 필요하면 외부 원부품 사양 보완 JSON을 지정한다. 기본값은 코어에 포함된 근거 자료다.

결과는 입력 작업 폴더의 `outputs/pdn_models/`에 저장한다.

- `Approved_S2P_Library/`, `BOM_Spec_Substitute_Library/`: S2P, RLC, 출처 파일
- `PDN_models.model-mapping.json`, `.csv`: 원 PN·대체 PN·부품 위치·모델 경로
- `PDN_models.result.json`: 코어 선정 결과와 보류 사유
- `pdn_model_generation_summary.json`: 실제 적용 대상 및 미확보 부품
- `generation_request.json`: 입력 기록과 중복 호출 재사용용 모델 SHA

최종 EDB 적용 결과는 기존 `outputs/sparam_assignment_result.json`에 S2P/RLC 경로와 선정 상태를 함께 기록한다. 같은 입력의 중복 전처리는 S2P·RLC·출처 파일의 SHA가 일치할 때 재사용한다. `post`는 기존 결과를 사용하므로 모델을 다시 생성하지 않는다.

Ansys 라이브러리 최상위와 각 품번 폴더에도 `PDN_models.model-mapping.json` 및 CSV를 복사한다. 이 JSON의 원 PN↔선택 PN 관계와 모델 상대 경로는 복사한 라이브러리 기준으로 다시 작성한다. 다른 입력 폴더의 매핑은 `PDN_models.<작업경로 해시>.model-mapping.json`으로 함께 보존한다. 캐시를 재사용하더라도 누락된 라이브러리 복사본과 매핑을 다시 복사한다. 실제 보드 전처리에서 선택한 모델을 동기화한다.

동기화만 별도로 측정하려면 `python tools/benchmark_library_sync.py --counts 1 14 50 --repeats 5`를 실행한다. 공식 샘플의 파일 크기를 사용하며 작업 폴더 안의 임시 라이브러리를 대상으로 측정한다. 실제 Ansys 폴더를 수정하지 않는다. 결과는 `verification/library_sync_benchmark.json`에 저장한다. 이 측정에는 제조사 다운로드와 Ansys solver 시간이 포함되지 않는다.

## 실행 방법

```text
python main.py <input.json> --stage full
python main.py <input.json> --stage pre
python main.py <input.json> --stage post
```

- `full`: 입력 검증, CAD/EDB 구성, Port/VRM 설정, SIwave 해석, Touchstone 추출 및 Post를 모두 수행합니다.
- `pre`: 해석 설정과 Port/VRM 구성까지 수행하고 solver 실행 전에 종료합니다.
- `post`: 기존 완료 결과와 `outputs/preprocessing_result.json`을 사용하여 재해석 없이 Touchstone, CSV, 그래프, FitView, ZoomView 및 JSON을 다시 생성합니다.

`post` 실행은 Full 또는 Pre/Local 실행에서 만들어진 `outputs` 폴더가 입력 JSON과 같은 작업 폴더에 있어야 합니다.

## 시스템 배치 구조

```text
<SYSTEM_ROOT>/
|-- PDN_setup/
|   |-- FR4_4L_Freq.sfsdf
|   |-- FR4_4L_OPT.sws
|   |-- PSI_2L_Freq.sfsdf
|   `-- PSI_2L_OPT.sps
`-- SIwave_PDN_V1_setting_VRM/
    |-- main.py
    |-- requirements.txt
    |-- CHANGES.md
    |-- README.md
    |-- core/
    `-- EBU_lib/
```

PDN setup 파일은 입력 작업 폴더의 `Data/Library/PDN_setup`, `Library/PDN_setup`, `PDN_setup`을 먼저 찾고, 이후 프로그램 폴더의 상위 `PDN_setup`을 찾습니다.

## 주요 동작

- CAD Pin은 Designator와 Pin number를 주 기준으로 매핑합니다.
- Net name은 참고 정보이며 불일치하면 경고를 남깁니다.
- InnerCap은 동일 Pin에 복수 부품을 생성할 수 있으며 수량과 부품값을 개별 검증합니다.
- SIwave VRM은 0.005 ohm RLC Component로 생성합니다.
- 각 VRM은 독립 Pad와 고유 Part Definition을 사용합니다.
- VRM은 반드시 2-Pin이고 양단 Net이 전원/GND인 경우에만 성공 처리합니다.
- 한 개라도 VRM 검증에 실패하면 잘못된 결과 생성을 막기 위해 Full 해석을 중단합니다.
- 완료 해석에서 Case 수와 같은 Port 수의 Touchstone을 추출합니다.
- Post는 Touchstone의 각 `Zii`를 사용하여 CSV와 임피던스 그래프를 생성합니다.
- 그래프 범위는 0.0001~1 GHz이며 최대 임피던스 위치에 Peak 마커와 값을 표시합니다.
- FitView/ZoomView는 대상 Net을 빨간색 Filled로, 나머지 Net을 옅은 회색 Unfilled로 표시합니다.

## 성공 확인 로그

14 Case 입력의 정상 예시는 다음과 같습니다.

```text
[VRM_SETUP] Summary: total=14, done=14, skipped=0
Touchstone validated: format=.s14p, ports=14
Full completed: 14/14 cases
```

각 VRM에는 아래 검증 로그가 있어야 합니다.

```text
[VRM_SETUP][RLC_VERIFY] ... pins=2, nets=['<POWER_NET>', 'GND']
```

## 주요 결과물

- `outputs/preprocessing_result.json`
- `outputs/vrm_port_setup_result.json`
- `outputs/*_PDN_FULL.siw`
- `outputs/*_PDN_FULL.siwaveresults/`
- `outputs/*_PDN_FULL_SYZ.sNp`
- `outputs/Z_Param_*.csv`
- `outputs/Z_Param_*.jpg`
- `outputs/*_FitView.jpg`
- `outputs/*_ZoomView.jpg`
- `outputs/title.json`, `request.json`, `result.json`, `setting.json`, `result_detail.json`

`result_detail.json`의 공통 구조는 DCIR H11과 동일하게 `stackup`, `tools`,
`Image`, `simSchedule`, `result`, `postInfo`를 사용합니다. `tools`는 도구명,
실행 단계, 요청 버전, 실제 버전, 코드 버전을 기록하며 실제 설치 버전을
확인하지 못한 경우 `actualVersionDisplay`는 `-`입니다. `simSchedule`의
`endData` 철자는 기존 시스템 계약을 유지합니다.

PDN의 임피던스, 최대값 주파수, Touchstone, 검증 근거는 `result`에 유지합니다.
이미지·CSV·Touchstone 항목은 출력 폴더 기준 파일명으로 제공하고, 진단용
프로젝트·결과 폴더 경로는 유지합니다. `EDB_Target_Net`에는 실제 EDB Net을
보존하며 SIGN Net에 `Display_Net`이 있는 경우 표시용 `Net`에 사용합니다.

각 케이스의 `PDN_Result`에는 PDN 상세 결과를 추가로 제공합니다.
`Files`에 CSV, 임피던스 그래프, Touchstone, FitView, ZoomView의 절대 경로를
기록하며, 최대 임피던스와 해당 주파수, 결과 파일 검증 근거도 함께 저장합니다.
생성되지 않은 파일 경로는 빈 문자열, 확보되지 않은 최대값은 `null`입니다.

## 운영 주의사항

- `core/config.json`의 AEDT 버전과 설치된 AEDT/SIwave 버전이 일치해야 합니다.
- S-Parameter 모델 디렉터리는 설치 환경에 맞게 확인해야 합니다.
- Touchstone Port 수가 Case 수와 다르면 Post가 실패합니다.
- `post`는 기존 완료 해석을 다시 실행하지 않습니다.
- 수동 해석과 비교할 때 Package De-cap, S-Parameter 적용률, Scope 대상 및 Port 위치를 동일하게 맞춰야 합니다.
