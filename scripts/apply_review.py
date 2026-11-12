from check import validate_asset
from run_state import MATERIALS, ContractError, cli, load_json, mutate, parser

REASONS = {"target_relevant", "agreement_screen", "unrelated_product", "recommendation_image", "advertisement_placeholder", "unclear_content", "unclear_identity", "near_duplicate", "supplementary_screen", "promotional_content"}


def apply(value, manifest_path, payload):
    reviews = payload.get("reviews")
    expected = payload.get("asset_ids")
    if not isinstance(reviews, list) or not reviews or not isinstance(expected, list) or len(set(expected)) != len(expected):
        raise ContractError("invalid_review_batch")
    ids = [item.get("asset_id") for item in reviews if isinstance(item, dict)]
    if len(ids) != len(reviews) or len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise ContractError("review_batch_incomplete")
    assets = {item["asset_id"]: item for item in value["assets"]}
    projected = {key: asset["decision"] for key, asset in assets.items()}
    for review in reviews:
        asset = assets.get(review["asset_id"])
        if asset is None:
            raise ContractError("unknown_asset")
        if asset["sha256"] != review.get("sha256"):
            raise ContractError("stale_review")
        validate_asset(asset, manifest_path, value["execution"]["budget"])
        if review.get("decision") not in {"accept", "exclude", "uncertain"} or review.get("material_type") not in MATERIALS:
            raise ContractError("invalid_review")
        reasons = review.get("reason_codes")
        if not isinstance(reasons, list) or not reasons or any(reason not in REASONS for reason in reasons):
            raise ContractError("invalid_review_reason")
        if not isinstance(review.get("observations"), str) or not review["observations"].strip():
            raise ContractError("visual_observations_required")
        if review.get("decision") == "accept" and set(reasons) & {"agreement_screen", "unrelated_product", "recommendation_image", "advertisement_placeholder", "unclear_content", "unclear_identity", "near_duplicate"}:
            raise ContractError("review_reason_conflict")
        duplicate = review.get("duplicate_of")
        if duplicate is not None and (duplicate not in assets or duplicate == asset["asset_id"] or review["decision"] != "exclude" or "near_duplicate" not in reasons):
            raise ContractError("invalid_duplicate_reference")
        projected[asset["asset_id"]] = review["decision"]
    for review in reviews:
        duplicate = review.get("duplicate_of")
        if duplicate and projected[duplicate] != "accept":
            raise ContractError("duplicate_target_not_accepted")
    if value["execution"].get("vision_available") is not True:
        raise ContractError("vision_not_available")
    for review in reviews:
        asset = assets[review["asset_id"]]
        asset["review"] = dict(review)
        asset["decision"] = review["decision"]
        asset["material_type"] = review["material_type"]
        asset["file_check_status"] = "passed"
    return {"reviewed": ids, "remaining_candidates": sum(asset["decision"] == "candidate" for asset in assets.values())}


def main():
    args = parser("提交实际读图后的完整审核结果").parse_args()
    payload = load_json(args.input)
    return mutate(args.manifest, "review", payload, lambda value: apply(value, args.manifest, payload), args.action_id, cleanup=True)


if __name__ == "__main__":
    cli(main)
