import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

FIELDS = ("id", "product_name", "standard_name", "product_category", "sub_category", "product_summary", "company")
MATERIALS = {"main_screenshot", "introduction_image", "guide_screenshot", "promotional_image", "product_image", "brand_image", "service_page"}
STOPS = {"budget_exhausted", "no_new_candidates", "diminishing_returns", "ambiguous_target", "capability_missing", "storage_error", "internal_error", "invalid_input", "export_unavailable", "output_limit"}
DEFAULT_BUDGET = {"max_search_queries": 12, "max_candidate_pages": 20, "max_tool_calls": 100, "max_elapsed_seconds": None, "min_image_width": 240, "min_image_height": 240, "review_batch_size": 8, "request_seconds": 30}


class ContractError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def load_json(path):
    def reject(value):
        raise ContractError("invalid_json_number")
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, parse_constant=reject)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encode(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def normalize(value):
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())
    return re.sub(r"app$", "", text)


def validate_input(value):
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise ContractError("invalid_input")
    product = {key: value.get(key) for key in FIELDS}
    identity = product["id"]
    if isinstance(identity, bool) or not isinstance(identity, (str, int)):
        raise ContractError("invalid_input")
    if isinstance(identity, int) and identity > 9007199254740991:
        raise ContractError("id_must_be_string")
    product["id"] = str(identity)
    if not re.fullmatch(r"[0-9]+", product["id"]):
        raise ContractError("invalid_input")
    if not isinstance(product["product_name"], str) or not product["product_name"].strip():
        raise ContractError("invalid_input")
    if any(product[key] is not None and not isinstance(product[key], str) for key in FIELDS[1:]):
        raise ContractError("invalid_input")
    return product


def local_path(manifest_path, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ContractError("unsafe_asset_path")
    root = Path(manifest_path).resolve().parent
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ContractError("unsafe_asset_path")
    return path


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_manifest(value):
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise ContractError("invalid_manifest")
    validate_input(value.get("input"))
    for field in ("assets", "sources", "search_events", "events", "warnings", "errors"):
        if not isinstance(value.get(field), list):
            raise ContractError("invalid_manifest")
    if not isinstance(value.get("execution"), dict) or not isinstance(value.get("actions"), dict):
        raise ContractError("invalid_manifest")
    ids = [item.get("asset_id") for item in value["assets"] if isinstance(item, dict)]
    if any(not isinstance(item, str) or not item for item in ids):
        raise ContractError("invalid_asset_id")
    hashes = [item.get("sha256") for item in value["assets"] if isinstance(item, dict)]
    if len(ids) != len(value["assets"]) or len(set(ids)) != len(ids) or len(set(hashes)) != len(hashes):
        raise ContractError("duplicate_asset")
    if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in hashes):
        raise ContractError("invalid_asset_hash")
    return value


@contextmanager
def locked(path):
    import fcntl
    lock = Path(path).with_name(".writer.lock")
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError("run_busy") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def remaining(value):
    execution = value["execution"]
    elapsed = max(0, time.time() - execution["started_at"])
    budget = execution["budget"]
    seconds = None if budget["max_elapsed_seconds"] is None else max(0, budget["max_elapsed_seconds"] - elapsed)
    return {"seconds": seconds, "tool_calls": max(0, budget["max_tool_calls"] - execution["tool_calls"]), "search_queries": max(0, budget["max_search_queries"] - len(value["search_events"])), "candidate_pages": max(0, budget["max_candidate_pages"] - len(value["sources"]))}


def mutate(path, action, payload, operation, action_id=None, cleanup=False):
    with locked(path):
        value = validate_manifest(load_json(path))
        fingerprint = hashlib.sha256(encode({"action": action, "payload": payload}).encode()).hexdigest()
        key = action_id or fingerprint
        previous = value["actions"].get(key)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ContractError("action_conflict")
            return previous["result"]
        available = remaining(value)
        if not cleanup and ((available["seconds"] is not None and available["seconds"] <= 0) or available["tool_calls"] <= 0):
            raise ContractError("budget_exhausted")
        value["execution"]["tool_calls"] += 1
        try:
            result = operation(value)
        except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError, MemoryError):
            value["revision"] += 1
            atomic_json(path, value)
            raise
        value["actions"][key] = {"fingerprint": fingerprint, "result": result}
        value["events"].append({"action_id": key, "action": action, "timestamp": time.time()})
        value["revision"] += 1
        atomic_json(path, value)
        try:
            atomic_json(Path(path).with_name("events.json"), value["events"])
        except OSError:
            pass
        return result


def initialize(product, workspace, config=None):
    product = validate_input(product)
    budget = dict(DEFAULT_BUDGET)
    if config:
        if set(config) - set(budget):
            raise ContractError("invalid_config")
        for key, number in config.items():
            if key == "max_elapsed_seconds" and number is None:
                budget[key] = None
                continue
            if isinstance(number, bool) or not isinstance(number, (int, float)) or number <= 0:
                raise ContractError("invalid_config")
            budget[key] = number
    run_id = uuid.uuid4().hex
    root = Path(workspace).resolve() / "products" / product["id"] / "runs" / run_id
    root.mkdir(parents=True, exist_ok=False)
    for folder in ("assets", "evidence", "reviews"):
        (root / folder).mkdir()
    value = {"schema_version": "1.0", "run_id": run_id, "revision": 0, "input": product, "target": {"type": None, "ambiguous": False, "company_policy": "record_only"}, "execution": {"started_at": time.time(), "budget": budget, "tool_calls": 0, "phase": "search", "stop_reason": None, "vision_available": None}, "search_events": [], "sources": [], "assets": [], "events": [], "actions": {}, "warnings": [], "errors": []}
    atomic_json(root / "input.json", product)
    atomic_json(root / "manifest.json", value)
    return {"run_id": run_id, "manifest": str(root / "manifest.json"), "remaining": remaining(value)}


def issue(code, source_id=None, asset_id=None):
    return {"code": code, "message": code, "source_id": source_id, "asset_id": asset_id}


def parser(description):
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--manifest")
    result.add_argument("--input")
    result.add_argument("--action-id")
    return result


def cli(operation):
    try:
        result = operation()
        print(encode({"ok": True, "data": result, "error": None, "retryable": False}))
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError, MemoryError) as exc:
        code = getattr(exc, "code", "internal_error")
        print(encode({"ok": False, "data": None, "error": issue(code), "retryable": code in {"run_busy", "network_error", "timeout"}}))
        sys.exit(1)


def main():
    arguments = parser("运行状态与证据记录")
    arguments.add_argument("command", choices=("init", "show", "record-search", "record-source", "record-failure", "configure", "stop"))
    arguments.add_argument("--workspace")
    arguments.add_argument("--config")
    args = arguments.parse_args()
    if args.command == "init":
        return initialize(load_json(args.input), args.workspace, load_json(args.config) if args.config else None)
    if args.command == "show":
        value = validate_manifest(load_json(args.manifest))
        return {"manifest": value, "remaining": remaining(value)}
    payload = load_json(args.input)

    def operation(value):
        command = args.command
        if command == "record-search":
            if remaining(value)["search_queries"] <= 0:
                raise ContractError("budget_exhausted")
            if not isinstance(payload.get("query"), str) or not payload["query"].strip() or not isinstance(payload.get("results"), list):
                raise ContractError("invalid_search")
            if payload.get("evidence_kind") not in {"structured_tool_result", "tool_summary", "raw_http"}:
                raise ContractError("invalid_search")
            record = copy.deepcopy(payload)
            record["search_id"] = "search-" + uuid.uuid4().hex
            value["search_events"].append(record)
            return record
        if command == "record-source":
            if remaining(value)["candidate_pages"] <= 0:
                raise ContractError("budget_exhausted")
            from source_adapters import inspect_payload
            source_payload = dict(payload)
            if "html_file" in source_payload:
                html_path = local_path(args.manifest, source_payload.pop("html_file"))
                raw = html_path.read_bytes()
                encoding = source_payload.pop("encoding", "utf-8")
                try:
                    source_payload["html"] = raw.decode(encoding, errors="strict")
                except UnicodeDecodeError:
                    if encoding.lower().replace("-", "") not in {"gb2312", "gbk", "cp936"}:
                        raise
                    source_payload["html"] = raw.decode("gb18030", errors="strict")
            record = inspect_payload(value["input"], source_payload)
            evidence_file = "evidence/" + record["source_id"] + ".html"
            (Path(args.manifest).resolve().parent / evidence_file).write_text(source_payload["html"], encoding="utf-8")
            record["evidence_file"] = evidence_file
            value["sources"].append(record)
            return record
        if command == "record-failure":
            allowed = {"login_required", "authorization_required", "captcha", "access_denied", "rate_limited", "network_error", "timeout", "capability_missing", "storage_error", "internal_error"}
            if payload.get("code") not in allowed:
                raise ContractError("invalid_error_code")
            record = issue(payload["code"], payload.get("source_id"), payload.get("asset_id"))
            value["warnings"].append(record)
            return record
        if command == "configure":
            if set(payload) - {"vision_available", "target_type", "ambiguous"}:
                raise ContractError("invalid_config")
            if "vision_available" in payload:
                if not isinstance(payload["vision_available"], bool):
                    raise ContractError("invalid_config")
                value["execution"]["vision_available"] = payload["vision_available"]
            if "ambiguous" in payload:
                if not isinstance(payload["ambiguous"], bool):
                    raise ContractError("invalid_config")
                value["target"]["ambiguous"] = payload["ambiguous"]
            if "target_type" in payload:
                if payload["target_type"] not in {"game", "app", "product", "brand", "service"}:
                    raise ContractError("invalid_config")
                value["target"]["type"] = payload["target_type"]
            return {"target": value["target"], "vision_available": value["execution"]["vision_available"]}
        if payload.get("stop_reason") not in STOPS:
            raise ContractError("invalid_stop_reason")
        value["execution"]["stop_reason"] = payload["stop_reason"]
        value["execution"]["phase"] = "finalize"
        return {"stop_reason": payload["stop_reason"]}

    return mutate(args.manifest, args.command, payload, operation, args.action_id, cleanup=args.command in {"stop", "record-failure"})


if __name__ == "__main__":
    cli(main)
