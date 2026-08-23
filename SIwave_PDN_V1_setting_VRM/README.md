# Ansys SIwave PDN Automation
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

- `full`: Pre? SIWave solve瑜??섑뻾?????꾨옒??怨듯넻 Post ?뚯씠?꾨씪?몄쓣 ?ㅽ뻾
- `pre`: case蹂?SIW? handoff manifest瑜??앹꽦?섍퀬 solve ?꾩뿉 醫낅즺. case AEDB??留뚮뱾吏 ?딆쓬
- `post`: Edden ?쒕쾭????λ맂 `outputs/preprocessing_result.json`怨?理쒖떊 ?꾨즺 `.siwaveresults/NNNN`???쎌뼱 case AEDB, 寃곌낵 JSON, Viewer瑜??덈줈 ?앹꽦

`post`???꾩옱 input JSON怨?Local 寃곌낵 ?대뜑媛 ?ㅼ쓬 愿怨꾩씪 ???ㅽ뻾?⑸땲??

```text
<POST_JOB_ROOT>/
|-- input.json
`-- outputs/
    |-- preprocessing_result.json
    |-- <case>.siw
    |-- <case>.siwaveresults/NNNN[_<simulation-name>]/
    `-- <case>.aedb/                    # Post ?앹꽦
```

- batch??`NNNN` ?대뜑? Local GUI??`NNNN_<simulation-name>` ?대뜑瑜?紐⑤몢 吏?먰빀?덈떎.
- `NNNN.siw`, `NNNN.ced`, `NNNN.finished`媛 紐⑤몢 ?덈뒗 媛????踰덊샇???뚯감瑜?理쒖떊 ?꾨즺 寃곌낵濡??ъ슜?⑸땲??
- Local?먯꽌 蹂寃쎈맂 V/I? 寃곌낵 媛믪? Web JSON 諛?`result_detail.json/changeHistory`??諛섏쁺?⑸땲??
- Local ?꾨즺 寃곌낵瑜??쎌? 紐삵븳 case???먯씤怨?議고쉶??`.siwaveresults` 寃쎈줈瑜??ㅽ뻾 濡쒓렇? `changeHistory`??湲곕줉?⑸땲??
- ?꾨즺 寃곌낵媛 0嫄댁씠硫?Viewer??AEDT瑜??ㅽ뻾?섏? ?딄퀬 Post ?ㅽ뙣濡?醫낅즺?⑸땲??
- Pre/CAD 蹂?섍낵 SIWave PDN ?댁꽍? ?ㅼ떆 ?섑뻾?섏? ?딆뒿?덈떎.
- Post???좏깮??理쒖떊 ?꾨즺 ?뚯감??`NNNN.siw`瑜?SIWave濡??댁뼱 `outputs/<case>.aedb`瑜??좉퇋 export?⑸땲??
- Post???꾨즺 case??AEDB瑜?紐⑤몢 export?섍퀬 SIWave ?몄뀡???レ? ??AEDT瑜??쒖옉?⑸땲?? ?곗냽 Full/Post ?ㅽ뻾?먯꽌 SIWave? AEDT???숈떆 COM/license ?먯쑀瑜??쇳븯湲??꾪븳 ?쒖꽌?낅땲??
- 湲곗〈 case AEDB, AEDT project/results, Field/Mesh/FitView/ZoomView???ъ떎???꾩뿉 ?쒓굅?⑸땲?? export ?ㅽ뙣 ?먮뒗 `edb.def` timeout ??遺遺?AEDB瑜??쒓굅?섍퀬 Post瑜??ㅽ뙣 泥섎━?⑸땲??
- ?좉퇋 case AEDB瑜?AEDT/HFSS 3D Layout?쇰줈 Import?섏뿬 Viewer??PDN ?댁꽍???섑뻾?섎?濡?Local V/I쨌Source 蹂寃쎌씠 Viewer?먮룄 諛섏쁺?⑸땲??
- `result_detail.json/postInfo`?먮뒗 理쒖떊 Local SIW ?ъ슜 ?щ?, ?④퀎蹂??곗텧臾??뚯쑀沅? case蹂?AEDB/Viewer ?곹깭瑜?湲곕줉?⑸땲??
- `preprocessing_result.json` schema 3??`Edb_Path`/`Edb_Folder`??Pre ?곗텧臾?寃쎈줈媛 ?꾨땲??Post媛 ?앹꽦???덉젙??target contract?대ŉ, `Artifact_Ownership`???대? 援щ텇?⑸땲??
- FullBatch???낅┰ Post? 媛숈? `run_standalone_post` ?뚯씠?꾨씪?몄쓣 ?몄텧?⑸땲??
- 蹂꾨룄 Post 寃곌낵 ?대뜑 ?몄옄???꾩쭅 吏?먰븯吏 ?딆쑝硫? `outputs`??input JSON??遺紐??대뜑 諛붾줈 ?꾨옒???덉뼱???⑸땲??

### Schedule
> * 8/6 ???Demo.
> * 8/13 ?대떦 Demo.
> * 8/22 ?곌뎄?뚯옣 Demo.
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
> * BOM ?곸슜 諛⑹떇 蹂寃?
>     * 湲곗〈 BOM???녿뒗 Component??紐⑤몢 ??젣?섎뒗 諛⑹떇? PAD媛 ??젣??
>     * RLC??deactivate [IO, IC, Other] type??component??洹몃?濡??④꺼 ??
>     * deactivate??RLC??Visible??False濡?蹂寃?(SIwave Text Mode) 
> * Update Source tracing algorithm
> * Add 0-ohm resistor installation process for FET and Switches
</details>

<details>
<summary><span style="font-size:200%"> v0.2.1 </span></summary>
  
> * v0.3 update瑜??꾪븳 Test ?섑뻾
> * PDN Voltage Drop Contour Plot??*.case濡?export
>     * ?쒕줈 ?ㅻⅨ Power Net????섏뿬 媛곴컖 ?앹꽦?섎뒗 ?ㅼ닔??CASE file???섎굹濡??⑹퀜???? (TBD)
>     * PDN ?댁꽍???꾨즺??*.siw瑜??낅젰諛쏆븘 *.case濡??앹꽦??二쇰뒗 script ?앹꽦 (TBD) 
> * Image Capture @ HFSS 3DL
</details>

<details>
<summary><span style="font-size:200%"> v0.4 </span></summary>
  
> * Script ?숈옉 諛⑹떇 蹂寃? Minerva Integration???꾪빐 Batch Command濡??숈옉?섎룄濡?蹂寃?
>     * (.venv) python main.py input.json
> * 寃쎈줈 臾몄젣 諛쒖깮 ?섏? ?딅룄濡?os.chdir ?곸슜
</details>

<details>
<summary><span style="font-size:200%"> v0.5 </span></summary>
  
> * PDN setup file (*.sws)??core/config.py ?먯꽌 愿由ы븯?꾨줉 蹂寃?
> * ?먮윭 ?놁씠 ?댁꽍 ?꾨즺 ??濡쒓렇 ????섏? ?딅뜕 臾몄젣 ?닿껐
> * Stage output ?곗텧 湲곕뒫 異붽?
>  * core/config.py ?먯꽌 control
> * Top/Btm Image file export
> * PDN 寃곌낵 load
> * Final Report ?앹꽦???꾪븳 JSON file ?앹꽦

</details>

<details>
<summary><span style="font-size:200%"> v0.6 </span></summary>
  
> * Input JSON file ?곷? 寃쎈줈???ъ슜 媛???섎룄濡?Update
> * Full BOM Bulk Inductor 紐살갼???먯씤 ?뺤씤 ?꾨즺
>  * L1404媛 BOM???꾨씫 ?섏뼱 ?덉쓬
> * HPC License type 異붽?
>  * core/PDN.exec file??hpc license type 異붽? (workgroup)
> * {CAD_NAME}_delshort.siw default濡?????섎룄濡?蹂寃?
> * Post-processing 湲곕뒫 ?꾨즺
>  * CASE file export ?꾨즺
>  * Vdrop Contour Image export ?꾨즺
> * 43QNED80 Sample Review ?꾨즺.
>  * BOM 諛?SPEC ?뚯씪 ?섏젙
>  * ?먮룞???댁꽍 諛?post-processing ?꾨즺

</details>

<details>
<summary><span style="font-size:200%"> v0.7 </span></summary>
  
> * Input JSON file ?곷? 寃쎈줈 error ?섏젙
> * log ???寃쎈줈 ?섏젙
> * PDN V-drop Contour Image plot ?ㅻ쪟 ?섏젙
>  * Case 5
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp;>>>> Designator : IC100
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Pin No. : AM28
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Net Name : +3.5V_ST_SOC
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Bead Inductor: L311
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; >>>>> Connected Net Name: +3.5V_ST
>    * 2025/08/05 02:51:00 &nbsp;&nbsp;&nbsp; >>>> Bulk Inductor: L1700
>  * ??Case??寃쎌슦, +3.5V_ST_SOC Net怨?Bead(L311)濡??곌껐??+3.5V_ST Net 紐⑤몢 plot ?댁빞??
>  * v0.6?먯꽌??+3.5V_ST_SOC Net留?plot ?섏뿀??
> * V-drop Contour Image export algorithm 媛쒖꽑
> * Case4 Bulk Inductor瑜?李얠? 紐삵븯???댁쑀
>  * BOM??Q800???놁쓬 ???꾩쓽濡?異붽? ?됯? 吏꾪뻾 
> * ERROR.json ?뚯씪 ?앹꽦
> * Export stackup XML file in 'output' folder

* ToDo List
  * IPC-2581(*.xml) export

</details>

<details>
<summary><span style="font-size:200%"> v0.8 </span></summary>
  
> Check point
> * Validation Check = False
> * PDN sim. setup = core/PDN_Fast.sws
> * isZuken = True ???뺤긽 ?숈옉 ?섎뒗吏 ?뺤씤 ?꾩슂
> * 43QNED80 Sample
>  * BOM??Q800 ?꾨씫?섏뼱 ?덉쓬. ??CASE 4?먯꽌 Bulk Inductor瑜?李얠? 紐삵븿
---
> * CAE type 蹂寃?- "PDN" ??"PI-PDN"
>  * core/postprocess.py line# 277 ?섏젙
> * AEDT gRPC disable
> * Stackup XML export 媛쒖꽑
>  * SIwave API媛 XML export瑜?吏?먰븯吏 ?딆븘, AEDT?먯꽌 Export?섎뒗 諛⑸쾿?쇰줈 蹂寃?
>  * stackup XML export??core/postprocess.py line# 465 ?먯꽌 ?섑뻾
>  * stackup XML ?뚯씪 ?대쫫 fixed to "stackup.xml"
> * NG mode Image export 媛쒖꽑
>  * using pyVista
> * (ToDo) python library update ??requirements.txt 
> * (ToDo) Add Validation Check Process & Evaluation 
</details>

<details>
<summary><span style="font-size:200%"> v0.81 </span></summary>

> * AEDT version 蹂寃?: 25R1 -> 24R2
> * core/post_process.py : +from core.database import PDNSessionException, ErrorCode
> * Mesh Plot Name 怨좎젙 : "Mesh1"
> * FieldType 蹂寃?
>  * "DC Fields" for 24R2
>  * "PDN Fields" for 25R1
> * Face list 媛쒖닔 諛섏쁺?섎룄濡??섏젙
> * 以묎컙 ?④퀎?먯꽌 H3DL ???
> * FitView??Target Component留??곸슜?섎룄濡??섏젙
</details>

<details>
<summary><span style="font-size:200%"> v0.9 </span></summary>

> * core/CleaningFiles.py ?곸슜 寃???섏??쇰굹, Minerva?먯꽌 ?섑뻾?섎뒗 寃껋쑝濡?寃곗젙.
>  * post-processing ??遺덊븘?뷀븳 ?뚯씪 ??젣
> * plotter method window size ?먮룞 ?ㅼ젙 - ?λ퉬 蹂??댁긽??怨좊젮
> * Output Files 寃쎈줈 ??젣 ???뚯씪紐낅쭔 寃곌낵 JSON??湲곕줉?섎룄濡??섏젙
> * FieldType 蹂寃?
>  * "DC Fields" for 24R2
>  * "PDN Fields" for 25R1
> * Top/Btm Image export??Background Color White?곸슜, Grid off, Ruler off
> * Zoom Area ?ㅼ젙 ?섏젙
>  * v0.8 : Target Net???곌껐??component 湲곗??쇰줈 bounding box ?ㅼ젙
>  * v0.9 : Target Net??primitive 湲곗??쇰줈 bounding box ?ㅼ젙
> * Fit/Zoom View Image?먯꽌 V/I Source留?洹몃┝??異붽?
> * Input file search 諛⑹떇 update
> * *.tgz file ?앹꽦

> * Voltage Source Install Algorithm Update
> * AEDT License ?놁뼱???덉뿴由?寃쎌슦, waiting time ???ъ떆?? 紐?踰??쒕룄 ??紐?李얠쑝硫?

</details>

<details>
<summary><span style="font-size:200%"> v0.91 </span></summary>

> * tgz ?뚯씪 ?앹꽦 寃쎈줈 ?섏젙
> * AEDT Version = 24R2
> * ZoomView Image Capture ?ㅻ쪟 ?섏젙
> * Spec file??Voltage Spec format 蹂寃??ы빆 ?곸슜

</details>

<details>
<summary><span style="font-size:200%"> v0.99 </span></summary>

> * AEDT Version = 24R2濡??섏젙 ???ъ슜?섏꽭??@config.json
> * 'Source_net' = "[]" ?먮윭 ?섏젙
> * tgz ?뚯씪 臾몄젣 -> 肄붾뱶?먮뒗 臾몄젣 ?놁쓬 DSGN 寃쎈줈 ?뺤씤??蹂?寃? SIwave Import ?섎뒗 寃껊룄 ?뺤씤
> * result json ?뚯씪???꾩껜 寃쎈줈 臾몄젣 ?닿껐.
> * Top/Btm Image Capture瑜??꾪븳 SIwave 李?理쒕???code 異붽?
> * Vsource Tracing Algorithm 媛쒖꽑
>  * ERROR2 LDO 

<summary><span style="font-size:200%"> v1.0 </span></summary>

> * Analog Switch媛 net???곌껐?섏뼱 ?덈뒗 寃쎌슦 異붽?
> * tgz ?뚯씪 ?앹꽦 ?ㅻ쪟 ?섏젙
</details>

</blockquote>
ToDo List : IPC-2581(*.xml) export
</details>

---

