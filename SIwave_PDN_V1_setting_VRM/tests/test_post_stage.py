import json
import ast
import tempfile
import unittest
from pathlib import Path

from core.post_stage import (
    PostStageError,
    append_post_detail,
    export_post_edb,
    prepare_post_settings,
    reconstruct_post_state,
)


class PostStageTests(unittest.TestCase):
    def _write_run(
        self,
        result_root,
        run_name,
        v_mag,
        i_mag,
        load_voltage,
        i_net="+NET",
        complete=True,
        folder_name=None,
    ):
        run_dir = result_root / (folder_name or run_name)
        run_dir.mkdir(parents=True)
        (run_dir / f"{run_name}.siw").write_text(
            f'CSRC "I_NET" 0 0 0 0 1 1 0 0 {i_mag} 1 0\n'
            f'VSRC "V_NET" 0 0 0 0 1 1 0 0 {v_mag} 1 0\n',
            encoding="utf-8",
        )
        (run_dir / f"{run_name}.ced").write_text(
            f'I_NET "{i_net}" {load_voltage} "GND" 0 {i_mag} I\n'
            f'V_NET "+SOURCE" {v_mag} "GND" 0 {i_mag} V\n',
            encoding="utf-8",
        )
        if complete:
            (run_dir / f"{run_name}.finished").write_text("complete\n", encoding="utf-8")
        return run_dir

    def _write_manifest(self, output_dir):
        manifest = [{
            "Case_Index": 1,
            "IC_Designator": "IC1",
            "IC_Pin": "A1",
            "Target_Net": "+NET",
            "Source_Component": "VRM1",
            "Source_Pin": "1",
            "Net_Chain": ["+NET"],
            "Full_Net_Chain": ["+NET"],
            "Voltage_V": "1.0",
            "Current_A": "2.0",
            "Min_Spec_V": "0.9",
            "Max_Spec_V": "1.1",
            "Project_Path": r"D:\stale\path\board_IC1_NET.siw",
            "V_Port_Name": "V_NET",
            "I_Port_Name": "I_NET",
        }]
        (output_dir / "preprocessing_result.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        (output_dir / "board_IC1_NET.siw").write_text("project", encoding="utf-8")
        return output_dir / "board_IC1_NET.siwaveresults"

    def test_latest_completed_run_reconstructs_summary_and_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result_root = self._write_manifest(output_dir)
            self._write_run(result_root, "0000", "1.0", "2.0", "0.95")
            self._write_run(result_root, "0001", "1.05", "2.5", "0.96")
            self._write_run(result_root, "0002", "1.10", "3.0", "0.97", complete=False)

            state = reconstruct_post_state(output_dir)

            case = state.summary[0]
            self.assertTrue(case["is_done"])
            self.assertEqual(case["Vmag"], "1.05")
            self.assertEqual(case["Imag"], "2.5")
            self.assertEqual(case["IC_pin"], "A1")
            self.assertEqual(case["Source_pin"], "1")
            self.assertEqual(case["Result"], 0.96)
            self.assertEqual(case["Drop Rate"], 8.571)
            self.assertEqual(case["FitView"].name, "IC1_NET_FitView.jpg")
            self.assertEqual(case["_viewer_siw"].name, "0001.siw")
            self.assertEqual(case["edb"], output_dir / "board_IC1_NET.aedb")
            self.assertEqual(state.change_history[0]["Latest_Run"], "0001")
            self.assertEqual(state.change_history[0]["Latest_Siw"].name, "0001.siw")
            self.assertEqual(
                [change["Field"] for change in state.change_history[0]["Changes"]],
                ["Vmag", "Imag"],
            )
            self.assertEqual(state.gnd_net, "GND")

    def test_changed_target_net_is_reported_as_case_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result_root = self._write_manifest(output_dir)
            self._write_run(result_root, "0000", "1.0", "2.0", "0.95", i_net="+OTHER")

            state = reconstruct_post_state(output_dir)

            self.assertFalse(state.summary[0]["is_done"])
            self.assertEqual(state.summary[0]["Pass/Fail"], "Error")
            self.assertIn("Current source net changed", state.change_history[0]["Error"])
            self.assertEqual(state.change_history[0]["Project_File"], "board_IC1_NET.siw")
            self.assertTrue(
                str(state.change_history[0]["Result_Folder"]).endswith("board_IC1_NET.siwaveresults")
            )

    def test_named_gui_run_folder_is_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result_root = self._write_manifest(output_dir)
            self._write_run(
                result_root,
                "0000",
                "1.0",
                "2.0",
                "0.95",
                folder_name="0000_PDN - IC1__NET",
            )

            state = reconstruct_post_state(output_dir)

            self.assertTrue(state.summary[0]["is_done"])
            self.assertEqual(state.change_history[0]["Latest_Run"], "0000")
            self.assertEqual(
                state.change_history[0]["Runs"][0]["Folder"],
                "0000_PDN - IC1__NET",
            )

    def test_single_alternate_artifact_names_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result_root = self._write_manifest(output_dir)
            run_dir = self._write_run(
                result_root,
                "0000",
                "1.0",
                "2.0",
                "0.95",
                folder_name="0000_PDN - IC1__NET",
            )
            for suffix in ("siw", "ced", "finished"):
                (run_dir / f"0000.{suffix}").rename(run_dir / f"local-result.{suffix}")

            state = reconstruct_post_state(output_dir)

            self.assertTrue(state.summary[0]["is_done"])
            self.assertEqual(state.change_history[0]["Latest_Run"], "0000")

    def test_missing_result_folder_reports_expected_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            self._write_manifest(output_dir)

            state = reconstruct_post_state(output_dir)

            history = state.change_history[0]
            self.assertFalse(state.summary[0]["is_done"])
            self.assertIn("Result folder does not exist", history["Error"])
            self.assertEqual(history["Result_Folder"], output_dir / "board_IC1_NET.siwaveresults")

    def test_post_detail_adds_history_without_replacing_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result_root = self._write_manifest(output_dir)
            self._write_run(result_root, "0000", "1.0", "2.0", "0.95")
            state = reconstruct_post_state(output_dir)
            state.viewer_artifacts = [{
                "Case_Index": 1,
                "Edb_Folder": "board_IC1_NET.aedb",
                "Edb_Status": "Complete",
                "Viewer_Status": "Complete",
            }]
            (output_dir / "result_detail.json").write_text(
                json.dumps({"result": [{"Net": "+NET"}]}),
                encoding="utf-8",
            )

            append_post_detail(output_dir, state)

            detail = json.loads((output_dir / "result_detail.json").read_text(encoding="utf-8"))
            self.assertEqual(detail["result"], [{"Net": "+NET"}])
            self.assertEqual(detail["changeHistory"][0]["Latest_Run"], "0000")
            self.assertEqual(detail["postInfo"]["viewerBasis"], "latest_completed_local_siw")
            self.assertTrue(detail["postInfo"]["viewerReflectsLocalSettings"])
            self.assertEqual(detail["postInfo"]["artifactOwnership"]["case_aedb"], "Post")
            self.assertEqual(detail["postInfo"]["viewerArtifacts"][0]["Edb_Status"], "Complete")

    def test_legacy_manifest_reuses_existing_result_pin_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            result_root = self._write_manifest(output_dir)
            manifest_path = output_dir / "preprocessing_result.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest[0].pop("IC_Pin")
            manifest[0].pop("Source_Pin")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (output_dir / "result.json").write_text(
                json.dumps({
                    "summary": [{
                        "IC": "IC1",
                        "Net": "+NET",
                        "IC_pin": "B2",
                        "Source_pin": "7",
                    }]
                }),
                encoding="utf-8",
            )
            self._write_run(result_root, "0000", "1.0", "2.0", "0.95")

            state = reconstruct_post_state(output_dir)

            self.assertEqual(state.summary[0]["IC_pin"], "B2")
            self.assertEqual(state.summary[0]["Source_pin"], "7")

    def test_prepare_post_settings_converts_web_file_names_to_paths(self):
        prepared = prepare_post_settings({
            "CAE": {"PCB": {"Stackup": "board.stk", "BOM": "bom.csv"}}
        })
        self.assertEqual(prepared["CAE"]["PCB"]["Stackup"].name, "board.stk")
        self.assertEqual(prepared["CAE"]["PCB"]["BOM"].name, "bom.csv")

    def test_post_edb_export_replaces_stale_target_from_selected_siw(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            siw_file = root / "latest.siw"
            siw_file.write_text("latest local settings", encoding="utf-8")
            edb_path = root / "case.aedb"
            edb_path.mkdir()
            (edb_path / "stale.txt").write_text("old", encoding="utf-8")
            events = {}

            class FakeProject:
                def ScrExportEDB(self, path):
                    target = Path(path)
                    target.mkdir()
                    (target / "edb.def").write_text("fresh", encoding="utf-8")

            class FakeSIwave:
                def __init__(self, version):
                    events["version"] = version
                    events["quit"] = False
                    self.oproject = FakeProject()

                def open_project(self, path):
                    events["source"] = path

                def quit_application(self):
                    events["quit"] = True

            exported = export_post_edb(
                siw_file,
                edb_path,
                "2025.2",
                siwave_factory=FakeSIwave,
                timeout=0.1,
            )

            self.assertEqual(exported, edb_path)
            self.assertFalse((edb_path / "stale.txt").exists())
            self.assertEqual((edb_path / "edb.def").read_text(encoding="utf-8"), "fresh")
            self.assertEqual(events["source"], str(siw_file))
            self.assertTrue(events["quit"])

    def test_failed_post_edb_export_removes_partial_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            siw_file = root / "latest.siw"
            siw_file.write_text("latest local settings", encoding="utf-8")
            edb_path = root / "case.aedb"

            class FakeProject:
                def ScrExportEDB(self, path):
                    target = Path(path)
                    target.mkdir()
                    (target / "partial.bin").write_bytes(b"partial")
                    raise RuntimeError("simulated export failure")

            class FakeSIwave:
                def __init__(self, version):
                    self.oproject = FakeProject()

                def open_project(self, path):
                    pass

                def quit_application(self):
                    pass

            with self.assertRaises(PostStageError):
                export_post_edb(
                    siw_file,
                    edb_path,
                    "2025.2",
                    siwave_factory=FakeSIwave,
                    timeout=0.1,
                )

            self.assertFalse(edb_path.exists())

    def test_pre_case_function_does_not_export_case_edb(self):
        main_path = Path(__file__).parents[1] / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8-sig"))
        run_case = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_pdn_case"
        )
        called_attributes = {
            node.func.attr
            for node in ast.walk(run_case)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("export_edb", called_attributes)

    def test_fullbatch_reuses_standalone_post_pipeline(self):
        main_path = Path(__file__).parents[1] / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8-sig"))
        post_pipeline_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_standalone_post"
        ]
        append_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "append_post_detail"
        ]

        self.assertEqual(len(post_pipeline_calls), 2)
        self.assertEqual(len(append_calls), 1)

    def test_post_exports_case_edbs_before_aedt_launch(self):
        module_path = Path(__file__).parents[1] / "core" / "post_processing.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8-sig"))
        post_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "PostProcessing"
        )
        extract_results = next(
            node for node in post_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "extract_results"
        )
        export_calls = [
            node.lineno for node in ast.walk(extract_results)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "export_edb"
        ]
        aedt_launch_calls = [
            node.lineno for node in ast.walk(extract_results)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "init_aedt_with_retry"
        ]

        self.assertEqual(len(export_calls), 1)
        self.assertGreaterEqual(len(aedt_launch_calls), 1)
        self.assertLess(export_calls[0], min(aedt_launch_calls))



if __name__ == "__main__":
    unittest.main()

