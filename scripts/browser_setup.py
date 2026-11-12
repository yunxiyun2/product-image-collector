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
        path = ROOT / item["path"]
        if not path.is_file() or digest(path) != item["sha256"]:
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
        with zipfile.ZipFile(ROOT / catalog["archive"]) as archive:
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
