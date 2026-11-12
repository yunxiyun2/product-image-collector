import argparse
import sys
import tempfile
import time
import uuid
from pathlib import Path

from browser_setup import prepare
from network import FetchError, download_to
from run_state import ContractError, cli, load_json

EXTRACT = """selector => {
const roots = [...document.querySelectorAll(selector)];
if (!roots.length || roots.some(x => ['BODY','HTML'].includes(x.tagName))) throw new Error('unsafe_or_missing_scope');
const clone = document.createElement('div');
for (const root of roots) {
const copy = root.cloneNode(true);
const originals = [root, ...root.querySelectorAll('*')];
const copies = [copy, ...copy.querySelectorAll('*')];
originals.forEach((node, index) => {
if (node instanceof HTMLImageElement && node.currentSrc) copies[index].setAttribute('data-current-src', node.currentSrc);
const bg = getComputedStyle(node).backgroundImage;
const match = /^url\\([\"']?(.*?)[\"']?\\)$/.exec(bg);
if (match) copies[index].setAttribute('data-background-image', new URL(match[1], location.href).href);
});
clone.appendChild(copy);
}
return {url: location.href, title: document.title, html: clone.innerHTML};
}"""


def operate(payload, runtime, output):
    prepared = prepare(runtime)
    sys.path.insert(0, prepared["python_path"])
    from playwright.sync_api import Error, TimeoutError, sync_playwright
    url = payload.get("url")
    if not isinstance(url, str):
        raise ContractError("browser_url_required")
    actions = payload.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 20:
        raise ContractError("invalid_browser_actions")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    obstacles = []
    with tempfile.TemporaryDirectory(dir=output) as temporary, sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(executable_path=prepared["executable"], headless=True, chromium_sandbox=True, timeout=30000)
            context = browser.new_context(service_workers="block", accept_downloads=False)
            context.set_default_timeout(15000)

            def route_request(route):
                request = route.request
                if request.method != "GET" or request.resource_type in {"media", "font"}:
                    route.abort()
                    return
                path = Path(temporary) / uuid.uuid4().hex
                try:
                    result = download_to(request.url, path, time.monotonic() + 30)
                    route.fulfill(status=200, content_type=result["content_type"] or "application/octet-stream", path=str(path))
                except (FetchError, OSError, Error):
                    obstacles.append("browser_resource_unavailable")
                    route.abort()

            context.route("**/*", route_request)
            context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                for action in actions:
                    kind = action.get("type")
                    selector = action.get("selector")
                    if kind == "click" and isinstance(selector, str):
                        page.locator(selector).first.click()
                    elif kind == "fill" and isinstance(selector, str) and isinstance(action.get("value"), str):
                        page.locator(selector).first.fill(action["value"])
                    elif kind == "scroll":
                        amount = action.get("pixels", 600)
                        if isinstance(amount, bool) or not isinstance(amount, int) or abs(amount) > 5000:
                            raise ContractError("invalid_scroll")
                        page.mouse.wheel(0, amount)
                    elif kind == "wait" and isinstance(selector, str):
                        page.locator(selector).first.wait_for(state="visible")
                    else:
                        raise ContractError("unsupported_browser_action")
                selector = payload.get("scope_selector")
                if selector:
                    if not isinstance(selector, str):
                        raise ContractError("invalid_scope_selector")
                    result = page.evaluate(EXTRACT, selector)
                    html_path = output / (uuid.uuid4().hex + ".html")
                    html_path.write_text(result.pop("html"), encoding="utf-8")
                    result["html_file"] = str(html_path)
                else:
                    result = {"url": page.url, "title": page.title(), "snapshot": page.locator("body").aria_snapshot()}
                if payload.get("screenshot") is True:
                    path = output / (uuid.uuid4().hex + ".png")
                    page.screenshot(path=str(path))
                    result["debug_screenshot"] = str(path)
                result["warnings"] = sorted(set(obstacles))
                return result
            finally:
                context.close()
                browser.close()
        except TimeoutError:
            raise ContractError("browser_timeout") from None
        except Error:
            raise ContractError("browser_launch_or_operation_failed") from None


def main():
    arguments = argparse.ArgumentParser(description="Skill内置Chromium网页访问与受控DOM提取")
    arguments.add_argument("--input", required=True)
    arguments.add_argument("--runtime", required=True)
    arguments.add_argument("--output", required=True)
    args = arguments.parse_args()
    return operate(load_json(args.input), args.runtime, args.output)


if __name__ == "__main__":
    cli(main)
