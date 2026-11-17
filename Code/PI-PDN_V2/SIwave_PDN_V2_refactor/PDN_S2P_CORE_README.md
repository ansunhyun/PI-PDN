# PDN S2P Core

현재 Pmap 실행앱의 모델 처리 코드를 PDN 자동화에서 GUI 없이 호출하도록 분리했다. 제조사에서 공식 S2P를 확보하고 RLC 정규화·품질 검증·특성 대체·원 PN 매핑을 수행한다. 명목 용량만으로 임의 S2P를 만들어 공식 모델로 사용하지 않는다.

## 포함 범위

| 위치 | 역할 |
|---|---|
| `pdn_s2p_core/api.py` | 자동화에서 호출할 배치 처리 API |
| `pdn_s2p_core/pmap_core/vendor_download.py`, `vendors/` | 제조사 요청·공식 파일·출처 저장 |
| `pdn_s2p_core/pmap_core/model_service.py` | exact 모델·대체 모델 확보 및 QC 조합 |
| `pdn_s2p_core/pdn_pre_s2p/substitution.py` | 동일 사양·단일 항목 인접 단계 선정 |
| `pdn_s2p_core/pdn_pre_s2p/netlist.py`, `approximate_rlc.py` | 공식 회로 정규화, S2P/RLC 대조, 허용된 근사 RLC |
| `pdn_s2p_core/pmap_core/component_spec.py`, `specification_evidence.py` | BOM 사양 추출과 공식 자료 보완 |
| `pdn_s2p_core/pmap_core/analysis_spec.py`, `operating_conditions.py` | PDN Spec CSV·NDF 범위 판정 |
| `pdn_s2p_core/pmap_core/model_mapping.py` | 원 PN↔선택 PN, 위치, 출처, 모델 경로 저장 |
| `pdn_s2p_core/settings/` | 원부품 보완 사양과 SHA를 검증할 공식 근거 |
| `tests/fixtures/official_library/` | 오프라인 검증용 실제 공식 모델 한 쌍 |

GUI, EXE, PyInstaller, TkinterDnD, 전체 승인 모델 라이브러리, 보드별 설정은 포함하지 않는다. 직접 의존 패키지는 NumPy·SciPy·scikit-rf이며 scikit-rf의 하위 의존 패키지는 pip가 설치한다. 상위 프로젝트의 `config.py`와 충돌하지 않도록 모든 코드를 `pdn_s2p_core` 네임스페이스에 넣었다. 원본 앱은 변경하지 않았다.

## 설치

Python 3.11을 권장하며, 고정한 의존 패키지에 맞춰 Python 3.11~3.12의 자동화 프로젝트 가상환경에 설치한다.

```powershell
python -m pip install -e "C:\Users\HP\Documents\Pmap_Gerneration\PDN_S2P_Core"
```

다른 PC에서는 이 폴더를 통째로 복사한 뒤 복사한 경로로 설치한다. `pdn_s2p_core/settings/evidence`도 함께 유지한다. 배포 패키지로 설치할 수 있도록 `pyproject.toml`에 설정·근거 파일을 포함했다.

## PDN 자동화에서 호출

자동화가 이미 PDN capacitor를 골랐다면 다음처럼 호출한다. 아래 원 PN은 사용 예시이며 코드의 대체 대상 지정에 사용하지 않는다.

```python
from pdn_s2p_core import PartEntry, generate_models, part_from_bom

parts = [
    part_from_bom(
        "0CK104BF56A",
        "C1005X7R104KET 100 nF 10% 16V X7R -55TO+125C 1005 R/TP DARFON ELECTRONICS CORP.",
        "COWELL FASHION CO., LTD",
    ),
    PartEntry("", "AMK063C6104MH-R", False, manufacturer="TaiyoYuden"),
]

result = generate_models(
    parts,
    output_dir=r"C:\PDN\models",
    library_dirs=[r"C:\PDN\existing_official_models"],
    filter_pdn_scope=False,  # 호출 측이 이미 PDN 대상만 선택한 경우
    allow_network=True,
    log=print,
)

for record in result["records"]:
    if record["status"] in ("exact_model_selected", "selected_bom_spec_substitute"):
        original_pn = record["originalPn"]
        selected_pn = record["selectedPn"]
        for variant in record["modelVariants"]:
            s2p_path = variant["s2pFile"]
            # 여기서 기존 PDN 해석기의 부품 모델에 s2p_path를 연결한다.
```

아직 대상 net을 선택하지 않았다면 `filter_pdn_scope=True`를 유지하고 `analysis_spec=Spec CSV 경로`, `ndf_file=NDF 경로`를 전달한다. Spec CSV에는 `Designator`, `Net_name`, `Pin_number`, `Frequency_start(MHz)`, `Frequency_end(MHz)`, `Spec(ohm)` 열이 필요하다. `Spec(ohm)` 표기는 `<10`처럼 입력한다. BOM은 지정된 net에 연결된 PDN capacitor만 처리하며, Inner Cap(`is_bom=False`)은 net과 무관하게 전부 처리한다. NDF 연결을 확인할 수 없는 BOM은 용도 확인으로 남는다.

`PartEntry` 또는 같은 필드 이름의 dict 목록을 받을 수 있다. 제조사 PN은 `maker_pn`, 원 사양은 `source_spec` 또는 `product_characteristics`, 명목 용량은 F 단위의 `nominal_capacitance_f`로 전달한다. BOM 행은 `part_from_bom()`을 사용하면 Technical Spec의 제조사·PN·용량·특성을 기존 판정 규칙으로 추출한다. Excel/CSV 전체 BOM과 Inner Cap 읽기는 호스트 자동화에서 수행한다.

## 대체 규칙

원 PN의 공식 모델 우선 → 같은 제조사의 동일 사양 → 같은 제조사의 한 항목 인접 단계 → 타사의 동일 사양 → 타사의 한 항목 인접 단계 순서다. 용량·유전체·크기·길이·폭은 유지한다. 정격전압 하향, 오차 확대, 온도범위 축소는 제외한다. 임의 ±비율이나 특정 대체 PN 목록을 사용하지 않는다.

누락된 원부품 사양은 `settings/original-component-specifications.json`에서 읽는다. BOM 값 우선, 누락 값만 보완하고 공식 근거 SHA를 검증한다. 현재 AMK 등록은 원 PN exact 사양서가 아니라 공식 계열·품번 문서에서 도출한 사양이며 옛 H 두께는 미확인이다. 이 구분을 매핑에 유지한다. 다른 부품도 같은 설정 구조로 추가할 수 있고, 호출 시 `original_specification_catalog`로 별도 자료 파일을 지정할 수 있다.

PDN Spec의 전압·목표 임피던스를 부품 모델의 동등성 근거로 사용하지 않는다. 모델 파일 확보와 보드 PDN 통과는 별도다. `pdnValidated=False`, `performanceSimilarityValidated=False`를 유지하며 실제 해석 판단은 호스트 자동화에서 한다.

Murata·Samsung·TDK·Taiyo Yuden·Darfon 다운로드 어댑터와 나머지 제조사의 조사·보류 흐름을 포함했다. 제조사별 공식 S2P 제공 여부는 다르므로 모든 PN의 다운로드 성공을 보장하지 않는다. Darfon처럼 공식 회로가 없을 때는 공식 S2P 기반의 근사 RLC가 사용될 수 있고 출처에 근사임을 기록한다.

## 출력과 연동

```text
출력 폴더/
├─ Approved_S2P_Library/PN/PN.s2p, PN.rlc, 출처 파일
├─ BOM_Spec_Substitute_Library/원 식별자/실행ID/대체PN/모델 파일
├─ Characteristic_Model_Investigations/실행ID/원 식별자/비교·선정 근거
├─ PDN_models.model-mapping.json
├─ PDN_models.model-mapping.csv
├─ PDN_models.result.json
└─ sparameter_registry.json
```

매핑은 모델 폴더와 라이브러리 최상위에도 저장한다. `s2pFileRelative`·`rlcFileRelative`·`sourceFileRelative`는 매핑 파일 폴더 기준이다. 폴더를 이동하면 해석기에서 상대 경로를 우선 사용한다. 다른 보드를 실행할 때는 `batch_name`을 달리해 기록을 구분한다.

API는 Pmap 파일을 생성하거나 PDN 시뮬레이터를 실행하지 않는다. 기존 매핑 스키마의 `pmapFile`에는 실행 이름을 담는 `.pmap` anchor가 기록되고 실제 파일은 생성하지 않으며 `pmapSaved=False`이다. 호스트는 매핑의 `records`와 모델 경로를 사용한다.

ANSYS 고정 경로와 관리자 권한 요구는 제거했다. `runtime_dirs=[실제 모델 폴더]`를 지정하면 기존 모델 복사와 매핑 동기화를 수행한다. 기본값은 복사하지 않는다. registry도 `output_dir`별로 분리해 다른 자동화 실행의 기록을 덮어쓰지 않는다.

## 오프라인 실행 예제

이 폴더에서 다음 명령을 실행한다. 포함된 검증용 공식 모델로 exact 1개·대체 1개와 매핑을 확인한다. fixture는 라이브러리 경로를 명시할 때만 사용한다.

```powershell
python -m pdn_s2p_core --input examples\parts.json --output output\demo --offline --library tests\fixtures\official_library
```

실연결은 `--offline`을 제거한다. BOM 범위 판정에는 `--pdn-spec`·`--ndf`, 호출 측에서 이미 선택한 PDN 부품 목록에는 `--already-pdn-filtered`를 사용한다. CLI 입력 JSON은 `examples/parts.json` 형식이다. 보류 모델 또는 매핑 저장 실패가 있으면 CLI는 종료 코드 1을 반환한다. 용도 확인·범위 밖 제외는 결과 파일에서 별도로 확인한다.

## 검증·원본 추적

`SOURCE_MANIFEST.json`에는 원본 파일 SHA와 export 파일 SHA, namespace·registry 분리 등 변경 사항을 기록했다. 알고리즘 본문은 기존 실행앱에서 가져왔으며 import 경로·저장 설정·registry 격리와 GUI 없는 실행 조합만 조정했다.

```powershell
python -m pytest tests -q
```

테스트에는 pytest가 별도로 필요하다. 외부 연결 없이 실제 공식 저장 모델의 exact·대체·QC·PDN 범위·매핑·출력 경로 분리를 확인한다. 새 제조사 실연결을 재검증했다는 뜻은 아니다.
