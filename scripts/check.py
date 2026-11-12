import warnings

from run_state import ContractError, cli, issue, local_path, mutate, parser, sha_file


def inspect_file(path, budget):
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            fmt = image.format
            image.load()
            sample = image.convert("L").resize((17, 16))
            pixels = [sample.getpixel((x, y)) for y in range(16) for x in range(17)]
            bits = 0
            for y in range(16):
                for x in range(16):
                    bits = (bits << 1) | (pixels[y * 17 + x] > pixels[y * 17 + x + 1])
    if width < budget["min_image_width"] or height < budget["min_image_height"]:
        raise ContractError("image_too_small")
    if fmt not in {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"}:
        raise ContractError("unsupported_image_format")
    return {"width": width, "height": height, "format": fmt, "dhash": f"{bits:064x}"}


def validate_asset(asset, manifest_path, budget):
    try:
        path = local_path(manifest_path, asset["file"])
        if not path.is_file() or sha_file(path) != asset["sha256"]:
            raise ContractError("asset_missing_or_changed")
        metadata = inspect_file(path, budget)
        if any(asset.get(key) != metadata[key] for key in ("width", "height", "format")):
            raise ContractError("asset_metadata_mismatch")
        from source_adapters import public_shape
        if not asset.get("evidence_valid") or not asset.get("sources"):
            raise ContractError("asset_evidence_invalid")
        for source in asset["sources"]:
            public_shape(source["source_page_url"])
            public_shape(source["original_image_url"])
        return metadata
    except ContractError:
        raise
    except (OSError, ValueError, KeyError, ImportError, RuntimeError, MemoryError, Warning):
        raise ContractError("invalid_image_file") from None


def check_manifest(value, path):
    results = []
    hints = []
    for asset in value["assets"]:
        try:
            metadata = validate_asset(asset, path, value["execution"]["budget"])
            asset["file_check_status"] = "passed"
            asset.update(metadata)
            results.append({"asset_id": asset["asset_id"], "sha256": asset["sha256"], "file": asset["file"], "status": "passed", "decision": asset["decision"]})
        except ContractError as exc:
            asset["file_check_status"] = "failed"
            asset["decision"] = "exclude"
            record = issue(exc.code, asset_id=asset["asset_id"])
            if record not in value["warnings"]:
                value["warnings"].append(record)
            results.append({"asset_id": asset["asset_id"], "status": "failed", "reason_code": exc.code})
    valid = [item for item in value["assets"] if item["file_check_status"] == "passed"]
    for position, first in enumerate(valid):
        for second in valid[position + 1:]:
            distance = (int(first["dhash"], 16) ^ int(second["dhash"], 16)).bit_count()
            if distance <= 10:
                hints.append({"first": first["asset_id"], "second": second["asset_id"], "distance": distance})
    batch = [item for item in results if item.get("status") == "passed" and item["decision"] == "candidate"][:int(value["execution"]["budget"]["review_batch_size"])]
    return {"assets": results, "review_batch": batch, "near_duplicate_hints": hints}


def main():
    args = parser("文件校验与待检清单").parse_args()
    from run_state import load_json
    revision = load_json(args.manifest)["revision"]
    return mutate(args.manifest, "check", {"revision": revision}, lambda value: check_manifest(value, args.manifest), args.action_id, cleanup=True)


if __name__ == "__main__":
    cli(main)
