import argparse
import sys

from apply_review import REASONS
from check import validate_asset
from run_state import (
    MATERIALS,
    STOPS,
    ContractError,
    encode,
    issue,
    load_json,
    validate_manifest,
)


def empty_result():
    return {"schema_version": "1.0", "id": None, "product_name": None, "result_status": "blocked", "stop_reason": "internal_error", "visual_check_status": "not_started", "counts": {key: None for key in ("downloaded", "accepted", "excluded", "uncertain", "pending_review")}, "images_mode": "inline", "images": [], "images_manifest_ref": None, "coverage": {key: None for key in ("queries_executed", "pages_visited", "sources_with_downloads")}, "warnings": [], "errors": [], "unprocessed_candidates": [], "manifest_ref": None}


def export(value, path):
    validate_manifest(value)
    result = empty_result()
    result["id"] = value["input"]["id"]
    result["product_name"] = value["input"]["product_name"]
    result["warnings"] = list(value["warnings"])
    result["errors"] = list(value["errors"])
    stop = value["execution"].get("stop_reason")
    result["stop_reason"] = stop if stop in STOPS else "internal_error"
    if not stop:
        result["errors"].append(issue("run_not_finalized"))
    counts = {key: 0 for key in result["counts"]}
    valid_candidates = 0
    reviewed = 0
    source_pages = set()
    for asset in sorted(value["assets"], key=lambda item: item["asset_id"]):
        counts["downloaded"] += 1
        for source in asset.get("sources", []):
            source_pages.add(source.get("source_page_url"))
        try:
            validate_asset(asset, path, value["execution"]["budget"])
        except ContractError as exc:
            counts["excluded"] += 1
            result["warnings"].append(issue(exc.code, asset_id=asset["asset_id"]))
            continue
        valid_candidates += 1
        review = asset.get("review")
        if not isinstance(review, dict) or review.get("sha256") != asset["sha256"] or review.get("asset_id") != asset["asset_id"] or review.get("decision") != asset.get("decision"):
            counts["pending_review"] += 1
            continue
        if value["execution"].get("vision_available") is not True:
            counts["pending_review"] += 1
            continue
        decision = review.get("decision")
        if decision not in {"accept", "exclude", "uncertain"}:
            counts["pending_review"] += 1
            continue
        if decision == "accept" and (not isinstance(review.get("reason_codes"), list) or not review["reason_codes"] or not set(review["reason_codes"]).issubset(REASONS) or set(review["reason_codes"]) & {"agreement_screen", "unrelated_product", "recommendation_image", "advertisement_placeholder", "unclear_content", "unclear_identity", "near_duplicate"} or not review.get("observations") or review.get("material_type") != asset.get("material_type")):
            counts["pending_review"] += 1
            result["warnings"].append(issue("invalid_review", asset_id=asset["asset_id"]))
            continue
        reviewed += 1
        if decision != "accept":
            counts["excluded" if decision == "exclude" else "uncertain"] += 1
            continue
        if asset.get("material_type") not in MATERIALS:
            counts["excluded"] += 1
            result["warnings"].append(issue("invalid_material_type", asset_id=asset["asset_id"]))
            continue
        counts["accepted"] += 1
        result["images"].append({"asset_id": asset["asset_id"], "sha256": asset["sha256"], "width": asset["width"], "height": asset["height"], "format": asset["format"], "material_type": asset["material_type"], "stored_image_ref": None, "sku_name": asset.get("sku_name"), "sources": sorted(asset["sources"], key=lambda source: (source["source_page_url"], source["original_image_url"]))})
    result["counts"] = counts
    result["coverage"] = {"queries_executed": len(value["search_events"]), "pages_visited": len(value["sources"]), "sources_with_downloads": len(source_pages)}
    available = value["execution"].get("vision_available")
    result["visual_check_status"] = "unavailable" if available is False else "incomplete" if counts["pending_review"] else "completed" if reviewed else "not_started"
    visited_urls = {source["original_image_url"] for asset in value["assets"] for source in asset.get("sources", [])}
    result["unprocessed_candidates"] = [{"source_url": item["original_image_url"], "reason_code": "not_downloaded"} for source in value["sources"] for item in source.get("candidates", []) if item["original_image_url"] not in visited_urls]
    if counts["downloaded"]:
        result["warnings"].append(issue("storage_not_persistent"))
    warning_codes = {item.get("code") for item in result["warnings"]}
    auth_blocked = not counts["downloaded"] and bool(warning_codes & {"login_required", "authorization_required", "captcha", "access_denied", "rate_limited", "source_auth_required", "source_forbidden", "source_rate_limited"})
    blocked = available is False or stop in {"capability_missing", "storage_error", "internal_error"} or auth_blocked
    if counts["accepted"]:
        result["result_status"] = "partial" if counts["pending_review"] or result["errors"] or blocked else "collected"
    elif value["target"].get("ambiguous"):
        result["result_status"] = "ambiguous_target"
    elif blocked or result["errors"]:
        result["result_status"] = "blocked"
    else:
        result["result_status"] = "no_verified_images"
    if sum(counts[key] for key in counts if key != "downloaded") != counts["downloaded"] or len(result["images"]) != counts["accepted"]:
        raise ContractError("count_mismatch")
    return result


def main():
    arguments = argparse.ArgumentParser(description="导出固定产品采集JSON")
    arguments.add_argument("--manifest", required=True)
    args = arguments.parse_args()
    result = empty_result()
    code = 0
    try:
        result = export(load_json(args.manifest), args.manifest)
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError) as exc:
        error_code = getattr(exc, "code", "manifest_read_error")
        result["errors"] = [issue(error_code)]
        if error_code in {"invalid_input", "id_must_be_string"}:
            result["result_status"] = "invalid_input"
            result["stop_reason"] = "invalid_input"
        code = 1
    print(encode(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
