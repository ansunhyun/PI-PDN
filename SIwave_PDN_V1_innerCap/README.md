# Ansys SIwave DCIR Automation
for LGE MS Minerva

---
### Dev. Env.
OS : Windows 11\
AEDT Version : 2025.2
---

### Stage Execution

```text
python main.py target.json --stage full
python main.py target.json --stage pre
python main.py target.json --stage post
```

- `full`: Pre와 SIWave solve를 수행한 뒤 아래의 공통 Post 파이프라인을 실행
- `pre`: case별 SIW와 handoff manifest를 생성하고 solve 전에 종료. case AEDB는 만들지 않음
- `post`: Edden 서버에 저장된 `outputs/preprocessing_result.json`과 최신 완료 `.siwaveresults/NNNN`을 읽어 case AEDB, 결과 JSON, Viewer를 새로 생성

`post`는 현재 input JSON과 Local 결과 폴더가 다음 관계일 때 실행됩니다.

```text
<POST_JOB_ROOT>/
|-- input.json
`-- outputs/
    |-- preprocessing_result.json
    |-- <case>.siw
    |-- <case>.siwaveresults/NNNN[_<simulation-name>]/
    `-- <case>.aedb/                    # Post 생성
```

- batch의 `NNNN` 폴더와 Local GUI의 `NNNN_<simulation-name>` 폴더를 모두 지원합니다.
- `NNNN.siw`, `NNNN.ced`, `NNNN.finished`가 모두 있는 가장 큰 번호의 회차를 최신 완료 결과로 사용합니다.
- Local에서 변경된 V/I와 결과 값은 Web JSON 및 `result_detail.json/changeHistory`에 반영됩니다.
- Local 완료 결과를 읽지 못한 case는 원인과 조회한 `.siwaveresults` 경로를 실행 로그와 `changeHistory`에 기록합니다.
- 완료 결과가 0건이면 Viewer용 AEDT를 실행하지 않고 Post 실패로 종료합니다.
- Pre/CAD 변환과 SIWave DCIR 해석은 다시 수행하지 않습니다.
- Post는 선택한 최신 완료 회차의 `NNNN.siw`를 SIWave로 열어 `outputs/<case>.aedb`를 신규 export합니다.
- Post는 완료 case의 AEDB를 모두 export하고 SIWave 세션을 닫은 뒤 AEDT를 시작합니다. 연속 Full/Post 실행에서 SIWave와 AEDT의 동시 COM/license 점유를 피하기 위한 순서입니다.
- 기존 case AEDB, AEDT project/results, Field/Mesh/FitView/ZoomView는 재실행 전에 제거합니다. export 실패 또는 `edb.def` timeout 시 부분 AEDB를 제거하고 Post를 실패 처리합니다.
- 신규 case AEDB를 AEDT/HFSS 3D Layout으로 Import하여 Viewer용 DCIR 해석을 수행하므로 Local V/I·Source 변경이 Viewer에도 반영됩니다.
- `result_detail.json/postInfo`에는 최신 Local SIW 사용 여부, 단계별 산출물 소유권, case별 AEDB/Viewer 상태를 기록합니다.
- `preprocessing_result.json` schema 3의 `Edb_Path`/`Edb_Folder`는 Pre 산출물 경로가 아니라 Post가 생성할 안정된 target contract이며, `Artifact_Ownership`이 이를 구분합니다.
- FullBatch도 독립 Post와 같은 `run_standalone_post` 파이프라인을 호출합니다.
- 별도 Post 결과 폴더 인자는 아직 지원하지 않으며, `outputs`는 input JSON의 부모 폴더 바로 아래에 있어야 합니다.

### Schedule
> * 8/6 팀장 Demo.
> * 8/13 담당 Demo.
> * 8/22 연구소장 Demo.
---
<!-- ![Main GUI](./Resources/fig/main_GUI.bmp) -->
<details>
<summary><span style="font-size:150%"> What's New? </span></summary>

<blockquote>

<details>
<summary><span style="font-size:200%"> v0.1.0 </span></summary>
  
> * The process for choosing a version of Ansys Electronics Desktop(AEDT) has been modified.
</details>

<details>
<summary><span style="font-size:200%"> v0.2.0 </span></summary>
  
> * Update config.json
> * BOM 적용 방식 변경
>     * 기존 BOM에 없는 Component는 모두 삭제하는 방식은 PAD가 삭제됨
>     * RLC는 deactivate [IO, IC, Other] type의 component는 그대로 남겨 둠
>     * deactivate된 RLC는 Visible을 False로 변경 (SIwave Text Mode) 
> * Update DCDC tracing algorithm
> * Add 0-ohm resistor installation process for FET and Switches
</details>

<details>
<summary><span style="font-size:200%"> v0.2.1 </span></summary>
  
> * v0.3 update를 위한 Test 수행
> * DCIR Voltage Drop Contour Plot을 *.case로 export
>     * 서로 다른 Power Net에 대하여 각각 생성되는 다수의 CASE file을 하나로 합쳐야 함. (TBD)
>     * DCIR 해석이 완료된 *.siw를 입력받아 *.case로 생성해 주는 script 생성 (TBD) 
> * Image Capture @ HFSS 3DL
</details>

<details>
<summary><span style="font-size:200%"> v0.4 </span></summary>
  
> * Script 동작 방식 변경, Minerva Integration을 위해 Batch Command로 동작하도록 변경
>     * (.venv) python main.py input.json
> * 경로 문제 발생 하지 않도록 os.chdir 적용
</details>

<details>
<summary><span style="font-size:200%"> v0.5 </span></summary>
  
> * DCIR setup file (*.sws)을 core/config.py 에서 관리하도록 변경
> * 에러 없이 해석 완료 시 로그 저장 되지 않던 문제 해결
> * Stage output 산출 기능 추가
>  * core/config.py 에서 control
> * Top/Btm Image file export
> * DCIR 결과 load
> * Final Report 생성을 위한 JSON file 생성

</details>

<details>
<summary><span style="font-size:200%"> v0.6 </span></summary>
  
> * Input JSON file 상대 경로도 사용 가능 하도록 Update
> * Full BOM Bulk Inductor 못찾는 원인 확인 완료
>  * L1404가 BOM에 누락 되어 있음
> * HPC License type 추가
>  * core/DCIR.exec file에 hpc license type 추가 (workgroup)
> * {CAD_NAME}_delshort.siw default로 저장 되도록 변경
> * Post-processing 기능 완료
>  * CASE file export 완료
>  * Vdrop Contour Image export 완료
> * 43QNED80 Sample Review 완료.
>  * BOM 및 SPEC 파일 수정
>  * 자동화 해석 및 post-processing 완료

</details>

<details>
<summary><span style="font-size:200%"> v0.7 </span></summary>
  
> * Input JSON file 상대 경로 error 수정
> * log 저장 경로 수정
> * DCIR V-drop Contour Image plot 오류 수정
>  * Case 5
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp;>>>> Designator : IC100
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Pin No. : AM28
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Net Name : +3.5V_ST_SOC
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Bead Inductor: L311
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; >>>>> Connected Net Name: +3.5V_ST
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Bulk Inductor: L1700
>  * 위 Case의 경우, +3.5V_ST_SOC Net과 Bead(L311)로 연결된 +3.5V_ST Net 모두 plot 해야함.
>  * v0.6에서는 +3.5V_ST_SOC Net만 plot 되었음.
> * V-drop Contour Image export algorithm 개선
> * Case4 Bulk Inductor를 찾지 못하는 이유
>  * BOM에 Q800이 없음 → 임의로 추가 평가 진행 
> * ERROR.json 파일 생성
> * Export stackup XML file in 'output' folder

* ToDo List
  * IPC-2581(*.xml) export

</details>

<details>
<summary><span style="font-size:200%"> v0.8 </span></summary>
  
> Check point
> * Validation Check = False
> * DCIR sim. setup = core/DCIR_Fast.sws
> * isZuken = True → 정상 동작 하는지 확인 필요
> * 43QNED80 Sample
>  * BOM에 Q800 누락되어 있음. → CASE 4에서 Bulk Inductor를 찾지 못함
---
> * CAE type 변경 - "DCIR" → "PI-DCIR"
>  * core/postprocess.py line# 277 수정
> * AEDT gRPC disable
> * Stackup XML export 개선
>  * SIwave API가 XML export를 지원하지 않아, AEDT에서 Export하는 방법으로 변경
>  * stackup XML export는 core/postprocess.py line# 465 에서 수행
>  * stackup XML 파일 이름 fixed to "stackup.xml"
> * NG mode Image export 개선
>  * using pyVista
> * (ToDo) python library update → requirements.txt 
> * (ToDo) Add Validation Check Process & Evaluation 
</details>

<details>
<summary><span style="font-size:200%"> v0.81 </span></summary>

> * AEDT version 변경 : 25R1 -> 24R2
> * core/post_process.py : +from core.database import DCIRSessionException, ErrorCode
> * Mesh Plot Name 고정 : "Mesh1"
> * FieldType 변경
>  * "DC Fields" for 24R2
>  * "DCIR Fields" for 25R1
> * Face list 개수 반영하도록 수정
> * 중간 단계에서 H3DL 저장
> * FitView에 Target Component만 적용되도록 수정
</details>

<details>
<summary><span style="font-size:200%"> v0.9 </span></summary>

> * core/CleaningFiles.py 적용 검토 하였으나, Minerva에서 수행하는 것으로 결정.
>  * post-processing 후 불필요한 파일 삭제
> * plotter method window size 자동 설정 - 장비 별 해상도 고려
> * Output Files 경로 삭제 후 파일명만 결과 JSON에 기록되도록 수정
> * FieldType 변경
>  * "DC Fields" for 24R2
>  * "DCIR Fields" for 25R1
> * Top/Btm Image export시 Background Color White적용, Grid off, Ruler off
> * Zoom Area 설정 수정
>  * v0.8 : Target Net에 연결된 component 기준으로 bounding box 설정
>  * v0.9 : Target Net의 primitive 기준으로 bounding box 설정
> * Fit/Zoom View Image에서 V/I Source만 그림에 추가
> * Input file search 방식 update
> * *.tgz file 생성

> * Voltage Source Install Algorithm Update
> * AEDT License 없어서 안열릴 경우, waiting time 후 재시도, 몇 번 시도 후 못 찾으면

</details>

<details>
<summary><span style="font-size:200%"> v0.91 </span></summary>

> * tgz 파일 생성 경로 수정
> * AEDT Version = 24R2
> * ZoomView Image Capture 오류 수정
> * Spec file의 Voltage Spec format 변경 사항 적용

</details>

<details>
<summary><span style="font-size:200%"> v0.99 </span></summary>

> * AEDT Version = 24R2로 수정 후 사용하세요 @config.json
> * 'DCDC_net' = "[]" 에러 수정
> * tgz 파일 문제 -> 코드에는 문제 없음 DSGN 경로 확인해 볼 것, SIwave Import 되는 것도 확인
> * result json 파일에 전체 경로 문제 해결.
> * Top/Btm Image Capture를 위한 SIwave 창 최대화 code 추가
> * Vsource Tracing Algorithm 개선
>  * ERROR2 LDO 

<summary><span style="font-size:200%"> v1.0 </span></summary>

> * Analog Switch가 net에 연결되어 있는 경우 추가
> * tgz 파일 생성 오류 수정
</details>

</blockquote>
ToDo List : IPC-2581(*.xml) export
</details>

---
