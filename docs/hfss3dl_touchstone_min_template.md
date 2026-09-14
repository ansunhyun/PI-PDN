# HFSS 3D Layout Touchstone Minimum Template

Project: `PI-PDN_auto_sim_V1`  
Primary flow: `SIwave_PDN_V1_setting_VRM/EBU_lib/AEDT.py -> run_cutout_batch(...)`

## 1) Scope And Version
- AEDT version: `2025.2.x`
- PyAEDT version: `0.17.x`
- Solver/design: `HFSS 3D Layout (AEDT cutout flow)`
- Entry points:
  - `SIwave_PDN_V1_setting_VRM/EBU_lib/AEDT.py`
  - `run_pdn_aedt_cutout_solve(...)`

## 2) Preconditions (Must Pass)
- [ ] Cutout EDB generated for each case
- [ ] H3DL import success
- [ ] Setup exists (`SYZ_CUTOUT_{idx}`)
- [ ] Sweep exists (`Sweep_{idx}` or equivalent)
- [ ] Analyze completed
- [ ] Excitations (ports) count > 0

## 3) Runtime Sanity Logs (Required)
- Case index, IC, Net, PCB_Net
- Detected port list
- Selected setup/sweep
- Solution name (`"{setup} : {sweep}"`)
- Export output target path(s)

## 4) Export Call Matrix (Required Order)
1. `export_touchstone(solution_name, out_path)`
2. `export_touchstone(setup, sweep, out_path)`
3. `export_touchstone(setup, out_path)`
4. `export_touchstone(out_path)`

For each attempt:
- Capture return/exception
- Check `exists`
- Check file size `> 0`

## 5) Artifact Validation
- [ ] Touchstone file exists (`.sNp`)
- [ ] File size > 0
- [ ] Header parse success (`# ... S ... R ...`)
- [ ] Optional Z artifacts generated (`Z_Param_*.csv`, `Z_Param_*.jpg`)

## 6) Fallback Search Rules
Search roots:
- `outputs/`
- `*.aedtresults/`
- `*.pyaedt/*` runtime folders

Filename patterns:
- `Z_Param_{safe_case}.s*p`
- `*{safe_case}*.s*p`
- `*.s*p`, `*.ts`, `*.touchstone`

Selection:
- Choose newest file by modification time.

## 7) Failure Classification
- `NO_PORTS`
- `NO_SETUP`
- `NO_SWEEP`
- `UNSOLVED_SETUP`
- `EXPORT_API_NO_OUTPUT`
- `ARTIFACT_NOT_FOUND_AFTER_EXPORT`
- `PATH_OR_PERMISSION_ERROR`

## 8) Result JSON Minimum Schema
```json
{
  "Case_Index": 1,
  "IC": "IC100",
  "Net": "+D1V0",
  "Status": "Done",
  "Setup": "SYZ_CUTOUT_1",
  "Sweep": "Sweep_1",
  "Ports": ["PORT_IC100_D1V0"],
  "Touchstone": "C:/.../Z_Param_IC100_D1V0.s4p",
  "Touchstone_Size": 12345,
  "Failure_Label": "",
  "Failure_Detail": ""
}
```

## 9) Acceptance Criteria
- Done-case ratio: target `>= 95%`
- Touchstone non-empty ratio (done cases): target `>= 95%`
- `EXPORT_API_NO_OUTPUT` ratio: target `<= 5%`
- `NO_PORTS`: target `0`

## 10) Current Observed Status (Reference)
- Ports and setup/sweep selection can be valid.
- Touchstone may still fail with `EXPORT_API_NO_OUTPUT`.
- If export API returns without output file, classify and persist failure details in JSON.
