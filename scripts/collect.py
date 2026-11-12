import time
import uuid
from pathlib import Path

from check import inspect_file
from run_state import (
    MATERIALS,
    ContractError,
    atomic_json,
    cli,
    issue,
    load_json,
    mutate,
    parser,
    remaining,
    sha_file,
)

SUFFIXES = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tiff"}


def download_batch(value, manifest_path, payload):
    from network import FetchError, download_to
    source = next((item for item in value["sources"] if item["source_id"] == payload.get("source_id")), None)
    if not source:
        raise ContractError("unknown_source")
    if source["material_type"] not in MATERIALS:
        raise ContractError("invalid_material_type")
    urls = payload.get("image_urls")
    if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
        raise ContractError("invalid_image_urls")
    candidates = {item["original_image_url"]: item for item in source["candidates"]}
    if set(urls) - set(candidates):
        raise ContractError("image_outside_scope")
    root = Path(manifest_path).resolve().parent
    output = {"assets": [], "failures": [], "unprocessed": []}
    for url in dict.fromkeys(urls):
        seconds_left = remaining(value)["seconds"]
        if seconds_left is not None and seconds_left <= 0:
            output["unprocessed"].append(url)
            value["warnings"].append(issue("budget_exhausted", source["source_id"]))
            continue
        existing = next((item for item in value["assets"] if candidates[url] in item["sources"]), None)
        if existing:
            output["assets"].append({"asset_id": existing["asset_id"], "reused": True})
            continue
        temporary = root / "assets" / (uuid.uuid4().hex + ".download")
        downloaded = None
        try:
            seconds = value["execution"]["budget"]["request_seconds"] if seconds_left is None else min(value["execution"]["budget"]["request_seconds"], seconds_left)
            downloaded = download_to(url, temporary, time.monotonic() + seconds, referer=source["source_page_url"])
            digest = downloaded["sha256"]
            existing = next((item for item in value["assets"] if item["sha256"] == digest), None)
            if existing:
                if candidates[url] not in existing["sources"]:
                    existing["sources"].append(candidates[url])
                if source["source_id"] not in existing["source_ids"]:
                    existing["source_ids"].append(source["source_id"])
                atomic_json(manifest_path, value)
                output["assets"].append({"asset_id": existing["asset_id"], "reused": True})
                continue
            try:
                metadata = inspect_file(temporary, value["execution"]["budget"])
                status, decision = "passed", "candidate"
                suffix = SUFFIXES[metadata["format"]]
            except (OSError, ValueError, KeyError, ImportError, RuntimeError, MemoryError, Warning) as exc:
                metadata = {"width": None, "height": None, "format": None, "dhash": None}
                status, decision, suffix = "failed", "exclude", ".bin"
                code = getattr(exc, "code", "invalid_image_file")
                value["warnings"].append(issue(code, source["source_id"], "asset-" + digest))
            relative = "assets/" + digest + suffix
            destination = root / relative
            if destination.exists():
                if sha_file(destination) != digest:
                    raise ContractError("asset_file_conflict")
            else:
                temporary.rename(destination)
            asset = {"asset_id": "asset-" + digest, "sha256": digest, "file": relative, "bytes": downloaded["bytes"], **metadata, "sources": [candidates[url]], "source_ids": [source["source_id"]], "evidence_valid": source["evidence_valid"], "file_check_status": status, "decision": decision, "review": None, "material_type": source["material_type"], "sku_name": source.get("sku_name"), "stored_image_ref": None}
            value["assets"].append(asset)
            atomic_json(manifest_path, value)
            output["assets"].append({"asset_id": asset["asset_id"], "sha256": digest, "file": relative, "file_check_status": status})
        except (FetchError, OSError, ValueError, KeyError, ImportError, RuntimeError, MemoryError, Warning) as exc:
            code = getattr(exc, "code", "download_error")
            record = issue(code, source["source_id"])
            value["warnings"].append(record)
            output["failures"].append({"url": url, "code": code, "bytes_downloaded": downloaded is not None})
        finally:
            temporary.unlink(missing_ok=True)
    value["execution"]["phase"] = "check"
    return output


def fetch_page(value, manifest_path, payload):
    from network import download_to
    root = Path(manifest_path).resolve().parent
    relative = "evidence/" + uuid.uuid4().hex + ".html"
    seconds_left = remaining(value)["seconds"]
    seconds = value["execution"]["budget"]["request_seconds"] if seconds_left is None else min(value["execution"]["budget"]["request_seconds"], seconds_left)
    response = download_to(payload["url"], root / relative, time.monotonic() + seconds)
    return {"html_file": relative, "source_page_url": response["final_url"], "redirects": response["redirects"], "content_type": response["content_type"]}


def main():
    arguments = parser("受控下载目标范围图片或公开HTML")
    arguments.add_argument("command", choices=("download", "fetch-page"), default="download", nargs="?")
    args = arguments.parse_args()
    payload = load_json(args.input)
    operation = fetch_page if args.command == "fetch-page" else download_batch
    return mutate(args.manifest, args.command, payload, lambda value: operation(value, args.manifest, payload), args.action_id)


if __name__ == "__main__":
    cli(main)
