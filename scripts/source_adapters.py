import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from run_state import MATERIALS, ContractError, load_json, normalize

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Node:
    def __init__(self, tag="", attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children = []
        self.parts = []

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self):
        return "".join(self.parts) + "".join(child.text() for child in self.children)


class Document(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, dict(attrs), self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.parts.append(data)


def public_shape(url):
    if not isinstance(url, str):
        raise ContractError("invalid_url")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ContractError("invalid_url")
    return url


def token_matches(node, token):
    pattern = r"(?P<tag>[a-zA-Z][\w-]*)?(?P<id>#[\w-]+)?(?P<classes>(?:\.[\w-]+)*)(?:\[(?P<attr>[\w-]+)\])?"
    match = re.fullmatch(pattern, token)
    if not match or not token:
        raise ContractError("unsupported_selector")
    tag, identity, classes, attr = match.group("tag", "id", "classes", "attr")
    if tag and tag.lower() in {"body", "html"}:
        raise ContractError("unsafe_scope")
    if node.tag in {"body", "html"}:
        return False
    return (not tag or node.tag == tag.lower()) and (not identity or node.attrs.get("id") == identity[1:]) and set(re.findall(r"\.([\w-]+)", classes)).issubset(set(node.attrs.get("class", "").split())) and (not attr or attr in node.attrs)


def matches(node, selector):
    tokens = selector.split()
    if not tokens or any(token in {"body", "html", "*"} for token in tokens):
        raise ContractError("unsafe_scope")
    if not token_matches(node, tokens[-1]):
        return False
    ancestor = node.parent
    for token in reversed(tokens[:-1]):
        while ancestor and not token_matches(ancestor, token):
            ancestor = ancestor.parent
        if ancestor is None:
            return False
        ancestor = ancestor.parent
    return True


def selectors_for(page_url):
    catalog = load_json(Path(__file__).resolve().parent.parent / "references" / "source-catalog.json")
    host = urlsplit(page_url).hostname or ""
    for item in catalog["sites"]:
        if host == item["domain"] or host.endswith("." + item["domain"]):
            return item["selectors"]
    return []


def inspect_payload(product, payload):
    page = public_shape(payload.get("source_page_url"))
    markup = payload.get("html")
    name = payload.get("page_name")
    scope = payload.get("scope_evidence")
    if not isinstance(markup, str) or not markup or not isinstance(name, str) or not name or not isinstance(scope, str) or not scope.strip():
        raise ContractError("source_evidence_required")
    doc = Document(markup)
    if name not in doc.root.text():
        raise ContractError("page_name_not_present")
    method = "exact_or_normalized"
    if normalize(product["product_name"]) != normalize(name):
        alias = payload.get("alias_evidence")
        if not isinstance(alias, dict) or not isinstance(alias.get("quote"), str) or not alias["quote"]:
            raise ContractError("name_mismatch")
        public_shape(alias.get("url"))
        if alias.get("url") != page or alias["quote"] not in doc.root.text() or product["product_name"] not in alias["quote"] or name not in alias["quote"]:
            raise ContractError("alias_evidence_not_present")
        method = "evidence_alias"
    selectors = payload.get("selectors") or selectors_for(page)
    if not isinstance(selectors, list) or not selectors or any(not isinstance(selector, str) for selector in selectors):
        raise ContractError("scope_selector_required")
    nodes = [node for node in doc.root.walk() if any(matches(node, selector) for selector in selectors)]
    if not nodes:
        raise ContractError("unsupported_template")
    attribute = payload.get("image_attribute")
    if attribute is not None and (not isinstance(attribute, str) or not re.fullmatch(r"[\w-]+", attribute)):
        raise ContractError("invalid_image_attribute")
    candidates = []
    seen = set()
    for container in nodes:
        for node in container.walk():
            values = []
            if attribute:
                if node.attrs.get(attribute):
                    values.append(node.attrs[attribute])
            elif node.attrs.get("data-current-src"):
                values.append(node.attrs["data-current-src"])
            elif node.attrs.get("data-background-image"):
                values.append(node.attrs["data-background-image"])
            elif node.tag in {"img", "source"}:
                srcset = node.attrs.get("srcset", "")
                if srcset:
                    entries = [item.strip().split() for item in srcset.split(",")]
                    scored = [(float(item[1][:-1]) if len(item) == 2 and re.fullmatch(r"[\d.]+[wx]", item[1]) else 0, item[0]) for item in entries if item]
                    if scored:
                        values.append(max(scored)[1])
                if not values:
                    for key in ("data-original", "data-src", "data-lazy-src", "src"):
                        if node.attrs.get(key) and not node.attrs[key].startswith("data:"):
                            values.append(node.attrs[key])
                            break
            for candidate in values:
                url = urljoin(page, candidate)
                try:
                    public_shape(url)
                except ContractError:
                    continue
                if url not in seen:
                    seen.add(url)
                    candidates.append({"original_image_url": url, "source_page_url": page})
    if payload.get("material_type", "main_screenshot") not in MATERIALS:
        raise ContractError("invalid_material_type")
    if payload.get("sku_name") is not None and not isinstance(payload["sku_name"], str):
        raise ContractError("invalid_sku_name")
    title = next((node.text().strip() for node in doc.root.walk() if node.tag == "title"), "")
    return {"source_id": "source-" + uuid.uuid4().hex, "source_page_url": page, "page_name": name, "page_title": title, "name_match": method, "alias_evidence": payload.get("alias_evidence"), "scope_evidence": scope, "selectors": selectors, "image_attribute": attribute, "candidates": candidates, "evidence_valid": True, "company_policy": "record_only", "page_company": payload.get("page_company"), "material_type": payload.get("material_type", "main_screenshot"), "sku_name": payload.get("sku_name")}
