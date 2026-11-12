import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from apply_review import apply
from check import check_manifest
from export_result import empty_result, export
from PIL import Image
from run_state import (
    ContractError,
    atomic_json,
    initialize,
    load_json,
    mutate,
    sha_file,
    validate_input,
)
from source_adapters import inspect_payload


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.product = {"id": "933904102273466431", "product_name": "测试游戏", "standard_name": None, "product_category": "游戏", "sub_category": None, "product_summary": None, "company": None}
        self.path = Path(initialize(self.product, self.root)["manifest"])
        self.value = load_json(self.path)
        self.value["execution"]["vision_available"] = True

    def tearDown(self):
        self.directory.cleanup()

    def asset(self, color="red"):
        path = self.path.parent / "assets" / (color + ".png")
        Image.new("RGB", (300, 400), color).save(path)
        digest = sha_file(path)
        asset = {"asset_id": "asset-" + digest, "sha256": digest, "file": "assets/" + path.name, "width": 300, "height": 400, "format": "PNG", "sources": [{"source_page_url": "https://example.com/product", "original_image_url": "https://example.com/" + color.replace("#", "") + ".png"}], "evidence_valid": True, "file_check_status": "passed", "decision": "candidate", "review": None, "material_type": "main_screenshot", "sku_name": None}
        self.value["assets"].append(asset)
        return asset

    def review(self, asset, decision="accept", reasons=None):
        return {"asset_id": asset["asset_id"], "sha256": asset["sha256"], "decision": decision, "reason_codes": reasons or ["target_relevant"], "observations": "看到了目标界面", "material_type": "main_screenshot", "duplicate_of": None}

    def test_seven_fields_and_bigint(self):
        self.assertEqual(validate_input(self.product)["id"], self.product["id"])
        bad = dict(self.product, id=933904102273466431)
        with self.assertRaises(ContractError):
            validate_input(bad)
        with self.assertRaises(ContractError):
            validate_input(dict(self.product, task_id="wrong"))

    def test_run_isolation(self):
        other = initialize(self.product, self.root)
        self.assertNotEqual(str(self.path), other["manifest"])

    def test_idempotent_commit_and_conflict(self):
        operation = lambda value: value["warnings"].append({"code": "test"}) or {"done": True}
        first = mutate(self.path, "test", {}, operation, "same")
        self.assertEqual(first, mutate(self.path, "test", {}, operation, "same"))
        self.assertEqual(len(load_json(self.path)["warnings"]), 1)
        with self.assertRaises(ContractError):
            mutate(self.path, "test", {"changed": True}, operation, "same")

    def test_budget_and_cleanup(self):
        self.value["execution"]["budget"]["max_elapsed_seconds"] = 0.001
        self.value["execution"]["started_at"] = 0
        atomic_json(self.path, self.value)
        with self.assertRaises(ContractError):
            mutate(self.path, "work", {}, lambda value: {}, "work")
        mutate(self.path, "stop", {}, lambda value: {}, "stop", cleanup=True)

    def test_source_scope_excludes_recommendations(self):
        payload = {"source_page_url": "https://example.com/detail", "page_name": "测试游戏", "scope_evidence": "目标轮播", "selectors": [".shots"], "html": '<h1>测试游戏</h1><div class="shots"><img data-src="/a.png"><img srcset="/small.png 240w, /big.png 1080w"></div><div class="recommend"><img src="/other.png"></div>'}
        record = inspect_payload(self.product, payload)
        self.assertEqual([item["original_image_url"] for item in record["candidates"]], ["https://example.com/a.png", "https://example.com/big.png"])
        with self.assertRaises(ContractError):
            inspect_payload(self.product, dict(payload, selectors=["body"]))

    def test_standard_name_not_alias(self):
        payload = {"source_page_url": "https://example.com/detail", "page_name": "其他游戏", "scope_evidence": "目标轮播", "selectors": [".shots"], "html": '<h1>其他游戏</h1><div class="shots"><img src="/a.png"></div>'}
        with self.assertRaises(ContractError):
            inspect_payload(dict(self.product, standard_name="其他游戏"), payload)

    def test_file_validation_and_review(self):
        asset = self.asset()
        result = check_manifest(self.value, self.path)
        self.assertEqual(len(result["review_batch"]), 1)
        apply(self.value, self.path, {"asset_ids": [asset["asset_id"]], "reviews": [self.review(asset)]})
        self.value["execution"]["stop_reason"] = "budget_exhausted"
        result = export(self.value, self.path)
        self.assertEqual(result["result_status"], "collected")
        self.assertEqual(result["counts"]["accepted"], len(result["images"]))
        self.assertIsNone(result["images"][0]["stored_image_ref"])

    def test_reject_stale_and_missing_review(self):
        asset = self.asset()
        review = self.review(asset)
        review["sha256"] = "0" * 64
        with self.assertRaises(ContractError):
            apply(self.value, self.path, {"asset_ids": [asset["asset_id"]], "reviews": [review]})
        with self.assertRaises(ContractError):
            apply(self.value, self.path, {"asset_ids": [asset["asset_id"], "missing"], "reviews": [self.review(asset)]})
        self.assertEqual(asset["decision"], "candidate")

    def test_agreement_not_accepted(self):
        asset = self.asset()
        with self.assertRaises(ContractError):
            apply(self.value, self.path, {"asset_ids": [asset["asset_id"]], "reviews": [self.review(asset, reasons=["agreement_screen"])]})

    def test_missing_file_excluded_without_recovery(self):
        asset = self.asset()
        (self.path.parent / asset["file"]).unlink()
        self.value["execution"]["stop_reason"] = "no_new_candidates"
        result = export(self.value, self.path)
        self.assertEqual(result["counts"]["excluded"], 1)
        self.assertEqual(result["images"], [])
        self.assertFalse((self.path.parent / asset["file"]).exists())

    def test_no_vision_cannot_accept(self):
        asset = self.asset()
        self.value["execution"]["vision_available"] = False
        with self.assertRaises(ContractError):
            apply(self.value, self.path, {"asset_ids": [asset["asset_id"]], "reviews": [self.review(asset)]})
        self.value["execution"]["stop_reason"] = "capability_missing"
        result = export(self.value, self.path)
        self.assertEqual(result["visual_check_status"], "unavailable")
        self.assertEqual(result["counts"]["pending_review"], 1)
        self.assertEqual(result["result_status"], "blocked")

    def test_empty_has_same_keys_as_success(self):
        asset = self.asset()
        apply(self.value, self.path, {"asset_ids": [asset["asset_id"]], "reviews": [self.review(asset)]})
        self.value["execution"]["stop_reason"] = "no_new_candidates"
        self.assertEqual(set(export(self.value, self.path)), set(empty_result()))

    def test_cli_invalid_manifest_fixed_json(self):
        self.path.write_text("{bad", encoding="utf-8")
        command = subprocess.run([sys.executable, str(SCRIPTS / "export_result.py"), "--manifest", str(self.path)], capture_output=True, text=True, check=False)
        self.assertNotEqual(command.returncode, 0)
        result = json.loads(command.stdout)
        self.assertEqual(set(result), set(empty_result()))
        self.assertEqual(command.stderr, "")

    def test_partial_failure_preserves_good_asset(self):
        first = self.asset("red")
        second = self.asset("blue")
        apply(self.value, self.path, {"asset_ids": [first["asset_id"]], "reviews": [self.review(first)]})
        (self.path.parent / second["file"]).write_bytes(b"bad")
        self.value["execution"]["stop_reason"] = "no_new_candidates"
        result = export(self.value, self.path)
        self.assertEqual(result["counts"], {"downloaded": 2, "accepted": 1, "excluded": 1, "uncertain": 0, "pending_review": 0})

    def test_root_scope_with_class_rejected(self):
        payload = {"source_page_url": "https://example.com/detail", "page_name": "测试游戏", "scope_evidence": "目标图区", "selectors": ["body.shots"], "html": '<body class="shots"><h1>测试游戏</h1><img src="/a.png"></body>'}
        with self.assertRaises(ContractError):
            inspect_payload(self.product, payload)

    def test_dynamic_image_attributes(self):
        payload = {"source_page_url": "https://example.com/detail", "page_name": "测试游戏", "scope_evidence": "实际浏览器图区", "selectors": [".shots"], "html": '<h1>测试游戏</h1><div class="shots"><img data-current-src="/actual.png"><div data-background-image="/background.jpg"></div></div>'}
        result = inspect_payload(self.product, payload)
        self.assertEqual(len(result["candidates"]), 2)

    def test_export_rejects_forged_accept_reason(self):
        asset = self.asset()
        apply(self.value, self.path, {"asset_ids": [asset["asset_id"]], "reviews": [self.review(asset)]})
        asset["review"]["reason_codes"] = ["agreement_screen"]
        self.value["execution"]["stop_reason"] = "no_new_candidates"
        result = export(self.value, self.path)
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["pending_review"], 1)

    def test_download_partial_and_duplicate(self):
        from collect import download_batch
        from network import FetchError
        payload = {"source_page_url": "https://example.com/detail", "page_name": "测试游戏", "scope_evidence": "目标轮播", "selectors": [".shots"], "html": '<h1>测试游戏</h1><div class="shots"><img src="/a.png"><img src="/bad.png"><img src="/same.png"></div>'}
        source = inspect_payload(self.product, payload)
        self.value["sources"].append(source)

        def fake_download(url, destination, deadline, referer=None):
            if url.endswith("bad.png"):
                raise FetchError("source_forbidden")
            Image.new("RGB", (300, 400), "red").save(destination, format="PNG")
            return {"sha256": sha_file(destination), "bytes": destination.stat().st_size}

        with patch("network.download_to", side_effect=fake_download):
            result = download_batch(self.value, self.path, {"source_id": source["source_id"], "image_urls": [item["original_image_url"] for item in source["candidates"]]})
        self.assertEqual(len(self.value["assets"]), 1)
        self.assertEqual(len(self.value["assets"][0]["sources"]), 2)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(len(load_json(self.path)["assets"]), 1)

    def test_failed_action_counts_budget(self):
        def fail(value):
            raise ContractError("test_failure")
        with self.assertRaises(ContractError):
            mutate(self.path, "test", {}, fail, "failed")
        self.assertEqual(load_json(self.path)["execution"]["tool_calls"], 1)

    def test_no_ten_image_limit(self):
        assets = [self.asset(f"#{number:06x}") for number in range(11)]
        self.assertEqual(len(assets), 11)
        apply(self.value, self.path, {"asset_ids": [item["asset_id"] for item in assets], "reviews": [self.review(item) for item in assets]})
        self.value["execution"]["stop_reason"] = "budget_exhausted"
        self.assertEqual(export(self.value, self.path)["counts"]["accepted"], 11)


if __name__ == "__main__":
    unittest.main()
