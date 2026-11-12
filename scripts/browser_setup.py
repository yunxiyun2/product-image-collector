import argparse
import hashlib
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

from run_state import ContractError, cli, load_json

ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def bundle_file(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ContractError("unsafe_browser_bundle_path")
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise ContractError("unsafe_browser_bundle_path")
    return path


def assemble_archive(root, catalog, destination):
    parts = catalog.get("archive_parts")
    if not isinstance(parts, list) or not parts or len(set(parts)) != len(parts):
        raise ContractError("invalid_browser_parts")
    declared = {item["path"]: item for item in catalog["files"]}
    target = destination / "chrome-linux64.zip"
    temporary = destination / "chrome-linux64.zip.partial"
    result = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as output:
            for relative in parts:
                item = declared.get(relative)
                path = bundle_file(root, relative)
                if item is None or not path.is_file() or path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
                    raise ContractError("browser_bundle_integrity_failed")
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(chunk)
                        result.update(chunk)
                        size += len(chunk)
        if size != catalog["archive_bytes"] or result.hexdigest() != catalog["archive_sha256"]:
            raise ContractError("browser_archive_integrity_failed")
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def prepare(destination):
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ContractError("browser_requires_linux_x86_64")
    catalog = load_json(ROOT / "vendor" / "bundle.json")
    destination = Path(destination).resolve()
    if (destination / "ready.json").is_file():
        ready = load_json(destination / "ready.json")
        if ready.get("bundle_sha256") == digest(ROOT / "vendor" / "bundle.json") and Path(ready["executable"]).is_file():
            return ready
    for item in catalog["files"]:
        path = bundle_file(ROOT, item["path"])
        if not path.is_file() or path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise ContractError("browser_bundle_integrity_failed")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    packages = destination / "python"
    if not (packages / "playwright" / "__init__.py").exists():
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--find-links", str(ROOT / "vendor" / "wheels"), "--target", str(packages), "playwright==1.58.0"], check=True, capture_output=True, text=True)
    browser = destination / "chromium"
    binary = browser / catalog["executable"]
    if not binary.exists():
        browser.mkdir(exist_ok=True)
        archive_path = assemble_archive(ROOT, catalog, destination)
        with zipfile.ZipFile(archive_path) as archive:
            for item in archive.infolist():
                path = (browser / item.filename).resolve()
                if browser not in path.parents or (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ContractError("unsafe_browser_archive")
            archive.extractall(browser)
            for item in archive.infolist():
                path = browser / item.filename
                if path.is_file():
                    permissions = (item.external_attr >> 16) & 0o777
                    if permissions:
                        path.chmod(permissions & 0o777)
    binary.chmod(binary.stat().st_mode | 0o100)
    from run_state import atomic_json
    ready = {"runtime": str(destination), "python_path": str(packages), "executable": str(binary), "browser_version": catalog["browser_version"], "bundle_sha256": digest(ROOT / "vendor" / "bundle.json")}
    atomic_json(destination / "ready.json", ready)
    return ready


def main():
    arguments = argparse.ArgumentParser(description="离线准备随Skill打包的Linux浏览器")
    arguments.add_argument("--runtime", required=True)
    args = arguments.parse_args()
    return prepare(args.runtime)


if __name__ == "__main__":
    cli(main)
