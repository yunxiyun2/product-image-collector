import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from batch import export_batch, import_csv
from run_state import ContractError, atomic_json, load_json, remaining


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.csv = self.root / "products.csv"

    def tearDown(self):
        self.temp.cleanup()

    def test_bom_quotes_duplicate_ids_and_invalid_rows(self):
        self.csv.write_text('id,product_name,product_summary\n933904102273466431,"产品,一","简介\n第二行"\n933904102273466431,产品二,\n,缺ID,\n', encoding="utf-8-sig")
        imported = import_csv(self.csv, self.root)
        self.assertEqual(imported["total_products"], 3)
        self.assertNotEqual(imported["rows"][0]["manifest"], imported["rows"][1]["manifest"])
        result = export_batch(imported["batch"])
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(result["results"][2]["processing_status"], "invalid_input")
        self.assertEqual(result["summary"]["unprocessed_products"], 2)
        self.assertEqual(result["batch_status"], "partial")

    def test_finished_row_and_no_wallclock_limit(self):
        self.csv.write_text('id,product_name\n123,产品\n', encoding="utf-8")
        imported = import_csv(self.csv, self.root)
        path = imported["rows"][0]["manifest"]
        value = load_json(path)
        value["execution"]["started_at"] = 0
        self.assertIsNone(remaining(value)["seconds"])
        value["execution"]["stop_reason"] = "no_new_candidates"
        atomic_json(path, value)
        result = export_batch(imported["batch"])
        self.assertEqual(result["batch_status"], "finished")
        self.assertEqual(result["summary"]["finished_products"], 1)
        self.assertEqual(result["results"][0]["result"]["result_status"], "no_verified_images")

    def test_extra_duplicate_missing_headers_and_empty(self):
        for content in ('id,product_name,id\n1,A,1\n', 'name\nA\n', 'id,product_name,task_id\n1,A,x\n', 'id,product_name\n'):
            self.csv.write_text(content, encoding="utf-8")
            with self.assertRaises(ContractError):
                import_csv(self.csv, self.root)

    def test_bad_product_manifest_preserves_other_rows(self):
        self.csv.write_text('id,product_name\n1,A\n2,B\n', encoding="utf-8")
        imported = import_csv(self.csv, self.root)
        Path(imported["rows"][0]["manifest"]).write_text('{bad', encoding="utf-8")
        result = export_batch(imported["batch"])
        self.assertEqual(result["results"][0]["processing_status"], "failed")
        self.assertEqual(result["results"][1]["result"]["id"], "2")
        self.assertEqual(result["summary"]["failed_products"], 1)


if __name__ == "__main__":
    unittest.main()
