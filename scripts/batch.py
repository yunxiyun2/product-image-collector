import argparse
import csv
import io
import uuid
from pathlib import Path

from export_result import empty_result, export
from run_state import (
    FIELDS,
    ContractError,
    atomic_json,
    cli,
    encode,
    initialize,
    issue,
    load_json,
    local_path,
    validate_input,
)


def import_csv(csv_path, workspace):
    text = Path(csv_path).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    headers = reader.fieldnames
    if not headers or len(set(headers)) != len(headers) or not {"id", "product_name"}.issubset(headers) or set(headers) - set(FIELDS):
        raise ContractError("invalid_csv_headers")
    root = Path(workspace).resolve() / "batches" / uuid.uuid4().hex
    root.mkdir(parents=True)
    rows = []
    for index, row in enumerate(reader, 1):
        item = {"row_index": index, "input": {key: row.get(key) or None for key in FIELDS}, "manifest": None, "error": None}
        try:
            if None in row or any(value is None for value in row.values()):
                raise ContractError("invalid_csv_row")
            product = validate_input(item["input"])
            initialized = initialize(product, root)
            item["manifest"] = str(Path(initialized["manifest"]).relative_to(root))
        except ContractError as exc:
            item["error"] = exc.code
        rows.append(item)
    if not rows:
        raise ContractError("empty_csv")
    batch = {"schema_version": "1.0", "input_file": Path(csv_path).name, "rows": rows}
    atomic_json(root / "batch.json", batch)
    return {"batch": str(root / "batch.json"), "total_products": len(rows), "rows": [{"row_index": item["row_index"], "id": item["input"]["id"], "manifest": str(root / item["manifest"]) if item["manifest"] else None, "error": item["error"]} for item in rows]}


def export_batch(batch_path):
    batch = load_json(batch_path)
    if batch.get("schema_version") != "1.0" or not isinstance(batch.get("rows"), list):
        raise ContractError("invalid_batch")
    results = []
    for row in batch["rows"]:
        result = empty_result()
        result["id"] = row["input"].get("id")
        result["product_name"] = row["input"].get("product_name")
        processing = "invalid_input" if row.get("error") else "not_started"
        if row.get("error"):
            result["result_status"] = "invalid_input"
            result["stop_reason"] = "invalid_input"
            result["errors"] = [issue(row["error"])]
        else:
            try:
                path = local_path(batch_path, row["manifest"])
                value = load_json(path)
                result = export(value, path)
                processing = "finished" if value["execution"].get("stop_reason") else "in_progress" if value["execution"]["tool_calls"] else "not_started"
            except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError) as exc:
                result["errors"] = [issue(getattr(exc, "code", "product_export_error"))]
                processing = "failed"
        results.append({"row_index": row["row_index"], "processing_status": processing, "result": result})
    summary = {"total_products": len(results), "finished_products": sum(item["processing_status"] == "finished" for item in results), "invalid_products": sum(item["processing_status"] == "invalid_input" for item in results), "failed_products": sum(item["processing_status"] == "failed" for item in results), "unprocessed_products": sum(item["processing_status"] in {"not_started", "in_progress"} for item in results), "products_with_images": sum(bool(item["result"]["counts"]["accepted"]) for item in results)}
    return {"schema_version": "1.0", "input_file": batch["input_file"], "batch_status": "partial" if summary["unprocessed_products"] or summary["failed_products"] else "finished", "summary": summary, "results": results, "warnings": [], "errors": []}


def main():
    arguments = argparse.ArgumentParser(description="CSV批次导入与固定JSON导出")
    arguments.add_argument("command", choices=("init", "show", "export"))
    arguments.add_argument("--csv")
    arguments.add_argument("--workspace")
    arguments.add_argument("--batch")
    args = arguments.parse_args()
    if args.command == "init":
        return import_csv(args.csv, args.workspace)
    if args.command == "show":
        return load_json(args.batch)
    result = export_batch(args.batch)
    atomic_json(Path(args.batch).with_name("batch_result.json"), result)
    return result


if __name__ == "__main__":
    import sys
    if "export" in sys.argv[1:2]:
        try:
            print(encode(main()))
        except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError) as exc:
            print(encode({"schema_version": "1.0", "input_file": None, "batch_status": "failed", "summary": None, "results": [], "warnings": [], "errors": [issue(getattr(exc, "code", "batch_export_error"))]}))
            sys.exit(1)
    else:
        cli(main)
