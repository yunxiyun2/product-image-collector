import hashlib
import http.client
import io
import ipaddress
import math
import os
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit


class FetchError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError("deadline_exceeded")
    return remaining


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise FetchError("unsafe_address") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_global or any((address.is_loopback, address.is_private,
                                    address.is_link_local, address.is_reserved,
                                    address.is_multicast, address.is_unspecified)):
        raise FetchError("unsafe_address")
    return address


def _parse_url(url):
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise FetchError("invalid_url")
    if "#" in url or "\\" in url:
        raise FetchError("invalid_url")
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise FetchError("invalid_url")
        if parts.username is not None or parts.password is not None:
            raise FetchError("invalid_url")
        host = parts.hostname
        port = parts.port
        if not host or "%" in host or parts.netloc.endswith(":"):
            raise FetchError("invalid_url")
        if port is not None and not 1 <= port <= 65535:
            raise FetchError("invalid_url")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ":" in host or "[" in parts.netloc or "]" in parts.netloc:
                raise FetchError("invalid_url")
            host = host.encode("idna").decode("ascii").lower()
            labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
            if len(host.rstrip(".")) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
                raise FetchError("invalid_url")
        else:
            _public_ip(str(address))
            if address.version == 6:
                if not re.fullmatch(r"\[[0-9A-Fa-f:.]+\](?::[0-9]+)?", parts.netloc):
                    raise FetchError("invalid_url")
            elif "[" in parts.netloc or "]" in parts.netloc:
                raise FetchError("invalid_url")
            host = str(address)
    except (ValueError, UnicodeError):
        raise FetchError("invalid_url") from None
    return parts, host, port or (443 if parts.scheme == "https" else 80)


def _resolve(host, port, deadline=None):
    def lookup():
        return socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)

    if deadline is None:
        try:
            records = lookup()
        except OSError:
            raise FetchError("dns_error") from None
    else:
        results = queue.Queue(maxsize=1)

        def worker():
            try:
                results.put((True, lookup()))
            except OSError:
                results.put((False, None))

        _remaining(deadline)
        threading.Thread(target=worker, daemon=True).start()
        try:
            success, records = results.get(timeout=_remaining(deadline))
        except queue.Empty:
            raise FetchError("deadline_exceeded") from None
        _remaining(deadline)
        if not success:
            raise FetchError("dns_error")
    if not records:
        raise FetchError("dns_error")
    addresses = []
    for family, kind, protocol, _, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP):
            raise FetchError("unsafe_address")
        _public_ip(sockaddr[0])
        if sockaddr[1] != port or (family == socket.AF_INET6 and (len(sockaddr) != 4 or sockaddr[3] != 0)):
            raise FetchError("unsafe_address")
        if ipaddress.ip_address(sockaddr[0]).version != (4 if family == socket.AF_INET else 6):
            raise FetchError("unsafe_address")
        addresses.append((family, sockaddr))
    return tuple(addresses)


def _validate(url, deadline=None):
    parts, host, port = _parse_url(url)
    return {"url": url, "scheme": parts.scheme, "hostname": host, "port": port,
            "target": quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~") +
                      ("?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~") if parts.query else ""),
            "addresses": _resolve(host, port, deadline)}


def validate_url(url):
    return _validate(url)


class _DeadlineReader(io.RawIOBase):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    def readable(self):
        return True

    def readinto(self, buffer):
        self.transport.sock.settimeout(_remaining(self.transport.deadline))
        count = self.transport.sock.recv_into(memoryview(buffer)[:65536])
        _remaining(self.transport.deadline)
        return count


class _DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def makefile(self, mode, buffering=None):
        if mode != "rb":
            raise FetchError("network_error")
        return io.BufferedReader(_DeadlineReader(self))

    def sendall(self, data):
        self.sock.settimeout(_remaining(self.deadline))
        self.sock.sendall(data)
        _remaining(self.deadline)

    def close(self):
        self.sock.close()


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, validated, deadline):
        self.default_port = 443 if validated["scheme"] == "https" else 80
        super().__init__(validated["hostname"], validated["port"], timeout=_remaining(deadline))
        self.validated = validated
        self.deadline = deadline
        self.transport = None

    def connect(self):
        family, address = self.validated["addresses"][0]
        sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            sock.settimeout(_remaining(self.deadline))
            sock.connect(address)
            _remaining(self.deadline)
            if self.validated["scheme"] == "https":
                context = ssl.create_default_context()
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                sock.settimeout(_remaining(self.deadline))
                sock = context.wrap_socket(sock, server_hostname=self.validated["hostname"], do_handshake_on_connect=False)
                sock.settimeout(_remaining(self.deadline))
                sock.do_handshake()
                _remaining(self.deadline)
            self.transport = _DeadlineSocket(sock, self.deadline)
            self.sock = self.transport
        except BaseException:
            sock.close()
            raise

    def close(self):
        super().close()
        if self.transport is not None:
            self.transport.close()
            self.transport = None


def _publish(temporary, destination, deadline):
    _remaining(deadline)
    with tempfile.TemporaryDirectory(prefix=".fetch-publish-", dir=destination.parent) as directory:
        completed = Path(directory) / "complete"
        os.replace(temporary, completed)
        _remaining(deadline)
        try:
            os.link(completed, destination)
        except FileExistsError:
            raise FetchError("destination_exists") from None


def download_to(url, destination: Path, deadline: float, referer=None) -> dict:
    temporary = None
    try:
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise FetchError("invalid_deadline")
        _remaining(deadline)
        destination = Path(destination)
        if os.path.lexists(destination):
            raise FetchError("destination_exists")
        if referer is not None:
            _validate(referer, deadline)
            try:
                referer.encode("latin-1")
            except UnicodeError:
                raise FetchError("invalid_referer") from None
        current = url
        redirects = []
        digest = hashlib.sha256()
        size = 0
        while True:
            validated = _validate(current, deadline)
            connection = _PinnedConnection(validated, deadline)
            response = None
            try:
                headers = {"Accept-Encoding": "identity", "Connection": "close", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36", "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
                if referer is not None:
                    headers["Referer"] = referer
                connection.request("GET", validated["target"], headers=headers)
                response = connection.getresponse()
                _remaining(deadline)
                status = response.status
                if status in (301, 302, 303, 307, 308):
                    if len(redirects) >= 5:
                        raise FetchError("too_many_redirects")
                    location = response.getheader("Location")
                    if not location:
                        raise FetchError("invalid_redirect")
                    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in location) or "#" in location or "\\" in location:
                        raise FetchError("invalid_redirect")
                    next_url = urljoin(current, location)
                    next_parts, _, _ = _parse_url(next_url)
                    if validated["scheme"] == "https" and next_parts.scheme == "http":
                        raise FetchError("https_downgrade")
                    redirects.append(next_url)
                    current = next_url
                    continue
                if status in (401, 407):
                    raise FetchError("source_auth_required")
                if status == 403:
                    raise FetchError("source_forbidden")
                if status == 429:
                    raise FetchError("source_rate_limited")
                if not 200 <= status < 300:
                    raise FetchError("http_status")
                content_type = response.getheader("Content-Type", "")
                fd, name = tempfile.mkstemp(prefix=".fetch-", dir=destination.parent)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as output:
                    while True:
                        _remaining(deadline)
                        chunk = response.read1(65536)
                        _remaining(deadline)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        _remaining(deadline)
                if response.length not in (None, 0):
                    raise FetchError("incomplete_response")
                _publish(temporary, destination, deadline)
                temporary = None
                return {"final_url": current, "redirects": redirects, "bytes": size,
                        "sha256": digest.hexdigest(), "content_type": content_type}
            finally:
                try:
                    if response is not None:
                        response.close()
                finally:
                    connection.close()
    except FetchError:
        raise
    except TimeoutError:
        raise FetchError("deadline_exceeded") from None
    except ssl.SSLError:
        raise FetchError("tls_error") from None
    except http.client.HTTPException:
        raise FetchError("network_error") from None
    except OSError:
        raise FetchError("io_error") from None
    except (ValueError, TypeError):
        raise FetchError("invalid_input") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
