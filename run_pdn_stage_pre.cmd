@echo off
setlocal enabledelayedexpansion

set "ROOT_DIR=%~dp0"
set "APP_DIR=%ROOT_DIR%SIwave_PDN_V1_setting_VRM"
set "MAIN_SCRIPT=main_try_20260821_150435.py"
set "MAIN_PATH=%APP_DIR%\%MAIN_SCRIPT%"
set "INPUT_JSON=%ROOT_DIR%65MRGB82.json"
set "RUN_JSON=%INPUT_JSON%"
set "PDN_USE_PRECONVERTED=1"
set "PDN_ENABLE_VRM_SETUP=1"
set "PDN_FORCE_KILL_SIWAVE=1"
set "PDN_FORCE_ADMIN=0"

set "SPEC_FILE=%ROOT_DIR%PI-PDN_K27Mp_reference.csv"
set "CAD_ZIP=%ROOT_DIR%EAX02034601-HD-BD.zip"
set "STACKUP_FILE=%ROOT_DIR%Standard 2L 1.6T.stk"
set "PMAP_FILE=%ROOT_DIR%Pmap_K27Mp_65MRGB82_260721.pmap"
set "INNER_CAP_FILE=%ROOT_DIR%InnerCap_K27Mp.csv"
set "S2P_DIR=%ROOT_DIR%s2p"
set "BOM_JSON_PATH=%ROOT_DIR%65MRGB82_EAX02034601\K27MP_65MRGB82_withCI_BOM_260423.csv"
set "BOM_ROOT_PATH=%ROOT_DIR%K27MP_65MRGB82_withCI_BOM_260423.csv"

echo ============================================================
echo PDN Auto Sim - Stage PRE Runner
echo Root : %ROOT_DIR%
echo App  : %APP_DIR%
echo Main : %MAIN_SCRIPT%
echo Json : %INPUT_JSON%
echo Temp Preconverted Mode (requested) : %PDN_USE_PRECONVERTED%
echo VRM Setup Mode : %PDN_ENABLE_VRM_SETUP%
echo Force SIwave Process Cleanup : %PDN_FORCE_KILL_SIWAVE%
echo Force Admin Relaunch : %PDN_FORCE_ADMIN%
echo ============================================================

if /I "%PDN_USE_PRECONVERTED%"=="1" (
    set "HAS_PRECONV_DSGN="
    set "HAS_PRECONV_AEDB="
    for %%F in ("%ROOT_DIR%*.dsgn") do (
        if exist "%%~fF" set "HAS_PRECONV_DSGN=1"
    )
    for %%F in ("%ROOT_DIR%*.aedb") do (
        if exist "%%~fF" set "HAS_PRECONV_AEDB=1"
    )
    if not defined HAS_PRECONV_DSGN (
        if not defined HAS_PRECONV_AEDB (
            echo [WARN] Preconverted mode requested but no .dsgn/.aedb found in root.
            echo [WARN] Fallback to normal conversion mode.
            set "PDN_USE_PRECONVERTED=0"
        )
    )
    if not defined HAS_PRECONV_AEDB (
        echo [WARN] Preconverted mode: no .aedb found.
        echo [INFO] If .dsgn exists, stage-pre will try DSGN->EDB export automatically.
    )
)
echo Effective Preconverted Mode : %PDN_USE_PRECONVERTED%

if not exist "%MAIN_PATH%" (
    echo [WARN] Requested main script not found: %MAIN_PATH%
    echo [INFO] Fallback to default main.py
    set "MAIN_SCRIPT=main.py"
    set "MAIN_PATH=%APP_DIR%\main.py"
)

if not exist "%MAIN_PATH%" (
    echo [ERROR] Main script not found: %MAIN_PATH%
    exit /b 1
)

if not exist "%INPUT_JSON%" (
    echo [ERROR] Input JSON not found: %INPUT_JSON%
    exit /b 1
)

if not exist "%SPEC_FILE%" (
    echo [ERROR] Spec file not found: %SPEC_FILE%
    exit /b 1
)

if not exist "%CAD_ZIP%" (
    echo [ERROR] CAD zip file not found: %CAD_ZIP%
    exit /b 1
)

if not exist "%STACKUP_FILE%" (
    echo [ERROR] Stackup file not found: %STACKUP_FILE%
    exit /b 1
)

if not exist "%PMAP_FILE%" (
    echo [WARN] Pmap file not found: %PMAP_FILE%
    echo [WARN] S-parameter mapping may be skipped.
)

if not exist "%INNER_CAP_FILE%" (
    echo [ERROR] Inner cap file not found: %INNER_CAP_FILE%
    exit /b 1
)

if not exist "%S2P_DIR%" (
    echo [WARN] S2P directory not found: %S2P_DIR%
    echo [WARN] S-parameter assignment report may show all components as unlinked.
)

if not exist "%BOM_JSON_PATH%" (
    if exist "%BOM_ROOT_PATH%" (
        echo [WARN] JSON BOM path does not exist:
        echo        %BOM_JSON_PATH%
        echo [WARN] But root BOM exists:
        echo        %BOM_ROOT_PATH%
        echo [INFO] Creating temp JSON with BOM path = K27MP_65MRGB82_withCI_BOM_260423.csv
        python -c "import json, pathlib; src=pathlib.Path(r'%INPUT_JSON%'); out=pathlib.Path(r'%ROOT_DIR%65MRGB82.autofix.json'); data=json.load(open(src,'r',encoding='utf-8')); data['CAE']['PCB']['BOM']='K27MP_65MRGB82_withCI_BOM_260423.csv'; json.dump(data, open(out,'w',encoding='utf-8'), ensure_ascii=False, indent=2)"
        if errorlevel 1 (
            echo [ERROR] Failed to create temp JSON file.
            exit /b 1
        )
        set "RUN_JSON=%ROOT_DIR%65MRGB82.autofix.json"
        echo [INFO] Temp JSON: !RUN_JSON!
    ) else (
        echo [ERROR] BOM file not found:
        echo        %BOM_JSON_PATH%
        echo        %BOM_ROOT_PATH%
        exit /b 1
    )
)

if "%ANSYSEM_ROOT252%"=="" (
    echo [WARN] ANSYSEM_ROOT252 is not set. Continuing anyway.
    echo [WARN] If AEDT runtime is not discoverable, execution may fail later.
)

if /I "%PDN_USE_PRECONVERTED%"=="1" (
    echo [INFO] Preconverted mode enabled. Zuken converter check is skipped.
) else (
    if not exist "C:\Program Files\Zuken\CR-8000\Design Force\bin\DFevolv.cr5.exe" (
        echo [ERROR] Zuken converter not found:
        echo        C:\Program Files\Zuken\CR-8000\Design Force\bin\DFevolv.cr5.exe
        exit /b 1
    )
)

echo.
echo [RUN] python %MAIN_SCRIPT% "!RUN_JSON!" --stage pre
cd /d "%APP_DIR%"
set "PDN_USE_PRECONVERTED=%PDN_USE_PRECONVERTED%"
set "PDN_ENABLE_VRM_SETUP=%PDN_ENABLE_VRM_SETUP%"
set "PDN_FORCE_KILL_SIWAVE=%PDN_FORCE_KILL_SIWAVE%"
set "PDN_FORCE_ADMIN=%PDN_FORCE_ADMIN%"
python "%MAIN_SCRIPT%" "!RUN_JSON!" --stage pre
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [DONE] Stage pre completed successfully.
) else (
    echo [FAIL] Stage pre failed with exit code %EXIT_CODE%.
)

exit /b %EXIT_CODE%
