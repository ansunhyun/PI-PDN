# AEDT Cutout Result Schema Checklist

Target file: `outputs/aedt_cutout_result.json`  
Purpose: preserve case-level failure reason when Touchstone export is empty.

## 1) Top-Level Fields
- [ ] `Total` (int)
- [ ] `Done` (int)
- [ ] `Skipped` (int)
- [ ] `Records` (array)

## 2) Required Per-Case Fields
- [ ] `Case_Index` (int)
- [ ] `IC` (string)
- [ ] `Net` (string)
- [ ] `PCB_Net` (string)
- [ ] `Status` (`Done|Skipped|Failed`)
- [ ] `Message` (string)

## 3) Touchstone Traceability Fields
- [ ] `Setup` (string)
- [ ] `Sweep` (string)
- [ ] `Ports` (array of string)
- [ ] `Touchstone` (string path, empty if none)
- [ ] `Touchstone_Size` (int bytes, 0 if none)

## 4) Failure Classification Fields
- [ ] `Failure_Label` (string)
- [ ] `Failure_Detail` (string)

Recommended labels:
- `NO_PORTS`
- `NO_SETUP`
- `NO_SWEEP`
- `UNSOLVED_SETUP`
- `EXPORT_API_NO_OUTPUT`
- `ARTIFACT_NOT_FOUND_AFTER_EXPORT`
- `PATH_OR_PERMISSION_ERROR`

## 5) Log Correlation Rules
- [ ] If log contains `Ports detected ... []` then `Failure_Label=NO_PORTS`
- [ ] If export attempts return no file then `Failure_Label=EXPORT_API_NO_OUTPUT`
- [ ] If fallback search also fails then keep `Touchstone=""` and add detail

## 6) Acceptance Rules
- [ ] Every `Status=Done` record has non-empty `Setup` and `Sweep`
- [ ] `Ports` is always present (empty array allowed)
- [ ] `Touchstone` empty case must have non-empty `Failure_Label`
- [ ] `Failure_Detail` should contain first meaningful API/export error

## 7) Example Record
```json
{
  "Case_Index": 1,
  "IC": "IC100",
  "Net": "+D1V0",
  "PCB_Net": "+D1V0",
  "Status": "Done",
  "Setup": "SYZ_CUTOUT_1",
  "Sweep": "Sweep_1",
  "Ports": ["PORT_IC100_D1V0"],
  "Touchstone": "",
  "Touchstone_Size": 0,
  "Failure_Label": "EXPORT_API_NO_OUTPUT",
  "Failure_Detail": "solution+path/setup+sweep+path/setup+path/path-only all returned no output file",
  "Message": "OK"
}
```
