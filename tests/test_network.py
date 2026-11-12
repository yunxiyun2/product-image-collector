import contextlib
import hashlib
import importlib.util
import io
import ipaddress
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "network.py"
SPEC = importlib.util.spec_from_file_location("collector_network", MODULE_PATH)
network = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(network)


def records(host, port, *args):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = ipaddress.ip_address("93.184.216.34")
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    sockaddr = (str(address), port) if address.version == 4 else (str(address), port, 0, 0)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeSocket:
    def __init__(self, payload, clock=None, step=0, max_read=None):
        self.payload = memoryview(payload)
        self.position = 0
        self.clock = clock
        self.step = step
        self.max_read = max_read
        self.timeouts = []
        self.connected = []
        self.sent = []
        self.read_sizes = []
        self.closed = False
        self.handshakes = 0

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, address):
        self.connected.append(address)

    def do_handshake(self):
        self.handshakes += 1

    def sendall(self, data):
        self.sent.append(bytes(data))

    def recv_into(self, buffer):
        if self.clock:
            self.clock.now += self.step
        size = min(len(buffer), len(self.payload) - self.position)
        if self.max_read is not None:
            size = min(size, self.max_read)
        buffer[:size] = self.payload[self.position:self.position + size]
        self.position += size
        self.read_sizes.append(size)
        return size

    def close(self):
        self.closed = True


def response(body=b"", status=200, headers=None, chunked=False, length=None):
    fields = dict(headers or {})
    if chunked:
        fields["Transfer-Encoding"] = "chunked"
        body = b"".join(f"{len(piece):x}\r\n".encode() + piece + b"\r\n" for piece in (body[i:i + 40000] for i in range(0, len(body), 40000))) + b"0\r\n\r\n"
    else:
        fields.setdefault("Content-Length", str(len(body) if length is None else length))
    head = f"HTTP/1.1 {status} Test\r\n" + "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n"
    return head.encode("latin-1") + body


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / "image.bin"
        self.dns = patch.object(network.socket, "getaddrinfo", side_effect=records).start()
        self.addCleanup(patch.stopall)
        self.socket_factory = patch.object(network.socket, "socket", side_effect=AssertionError("Unexpected socket")).start()
        self.context_factory = patch.object(network.ssl, "create_default_context").start()
        self.context = self.context_factory.return_value
        self.context.wrap_socket.side_effect = lambda sock, **kwargs: sock

    def install(self, *sockets):
        self.socket_factory.side_effect = list(sockets)

    def fetch(self, url="https://example.com/image", **kwargs):
        return network.download_to(url, self.destination, kwargs.pop("deadline", time.monotonic() + 30), **kwargs)

    def assert_code(self, code, callback):
        with self.assertRaises(network.FetchError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        return caught.exception

    def assert_clean(self):
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_invalid_urls_do_not_resolve(self):
        urls = [None, "", "file:///etc/passwd", "ftp://example.com/a", "//example.com/a", "http:///a",
                "https://user:secret@example.com/a", "https://user@example.com/a", "https://@example.com/a",
                "https://example.com/a#", "https://example.com/a#fragment", "https://example.com/\r\nX:evil",
                "https://bad_host/a", "https://-bad.com/a", "https://bad..com/a", "https://example.com:0/a",
                "https://example.com:65536/a", "https://example.com:/a", "https://[::1", "https://[2606:4700::1111]evil/a",
                "https://[fe80::1%25en0]/a", "https://example.com\\@evil.com/a"]
        for url in urls:
            with self.subTest(url=url):
                self.assert_code("invalid_url", lambda url=url: network.validate_url(url))
        self.dns.assert_not_called()

    def test_nonpublic_literals_and_dns_addresses(self):
        addresses = ["127.0.0.1", "10.0.0.1", "172.16.1.1", "192.168.1.1", "169.254.169.254", "0.0.0.0",
                     "224.0.0.1", "240.0.0.1", "192.0.2.1", "100.64.0.1", "::1", "::", "fc00::1", "fe80::1",
                     "ff02::1", "2001:db8::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1"]
        for address in addresses:
            with self.subTest(address=address):
                literal = f"[{address}]" if ":" in address else address
                self.assert_code("unsafe_address", lambda literal=literal: network.validate_url(f"https://{literal}/a"))
                self.dns.side_effect = lambda host, port, *args, address=address: records("93.184.216.34", port) + records(address, port)
                self.assert_code("unsafe_address", lambda: network.validate_url("https://example.com/a"))
        self.socket_factory.assert_not_called()

    def test_valid_url_dns_all_addresses_and_idna(self):
        self.dns.side_effect = lambda host, port, *args: records("93.184.216.34", port) + records("2606:4700:4700::1111", port)
        validated = network.validate_url("https://例子.com:8443/图片?q=一")
        self.assertEqual(validated["hostname"], "xn--fsqu00a.com")
        self.assertEqual(validated["port"], 8443)
        self.assertEqual(len(validated["addresses"]), 2)
        self.assertTrue(validated["target"].startswith("/%"))
        self.dns.side_effect = records
        self.assertEqual(len(network.validate_url("https://[::ffff:8.8.8.8]/")["addresses"]), 1)

    def test_dns_failure_and_empty_answers(self):
        for side_effect, return_value in [(socket.gaierror("secret-url"), None), (None, [])]:
            self.dns.side_effect = side_effect
            self.dns.return_value = return_value
            self.assert_code("dns_error", lambda: self.fetch())
        self.assert_clean()

    def test_pin_tls_hostname_and_no_second_dns(self):
        sock = FakeSocket(response(b"payload"))
        self.install(sock)
        self.dns.side_effect = [records("93.184.216.34", 443), records("127.0.0.1", 443)]
        result = self.fetch()
        self.assertEqual(self.dns.call_count, 1)
        self.assertEqual(sock.connected, [("93.184.216.34", 443)])
        self.context.wrap_socket.assert_called_once_with(sock, server_hostname="example.com", do_handshake_on_connect=False)
        self.assertTrue(self.context.check_hostname)
        self.assertEqual(self.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(sock.handshakes, 1)
        self.assertIn(b"Host: example.com\r\n", b"".join(sock.sent))
        self.assertEqual(result["bytes"], 7)
        self.assertTrue(sock.closed)

    def test_ipv6_pin_and_http_without_tls(self):
        sock = FakeSocket(response(b"ok"))
        self.install(sock)
        self.fetch("http://[2606:4700:4700::1111]:8080/a")
        self.assertEqual(sock.connected, [("2606:4700:4700::1111", 8080, 0, 0)])
        self.assertIn(b"Host: [2606:4700:4700::1111]:8080", b"".join(sock.sent))
        self.context_factory.assert_not_called()

    def test_stream_sha_large_file_no_limit(self):
        body = bytes(range(256)) * 70000
        sock = FakeSocket(response(body, headers={"Content-Type": "image/png"}))
        self.install(sock)
        result = self.fetch()
        self.assertGreater(len(body), 15_000_000)
        self.assertEqual(result, {"final_url": "https://example.com/image", "redirects": [], "bytes": len(body),
                                  "sha256": hashlib.sha256(body).hexdigest(), "content_type": "image/png"})
        self.assertEqual(self.destination.read_bytes(), body)
        self.assertLessEqual(max(sock.read_sizes), 65536)
        self.assertGreater(len(sock.read_sizes), 200)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.destination])

    def test_chunked_stream_and_empty_response(self):
        for body, chunked in [(b"abc" * 90000, True), (b"", False)]:
            with self.subTest(chunked=chunked):
                self.install(FakeSocket(response(body, chunked=chunked)))
                result = self.fetch()
                self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(self.destination.read_bytes(), body)
                self.destination.unlink()

    def test_redirects_validate_and_pin_each_hop(self):
        sockets = [FakeSocket(response(status=302, headers={"Location": "/next"})),
                   FakeSocket(response(status=307, headers={"Location": "https://other.example/final"})),
                   FakeSocket(response(b"done"))]
        self.install(*sockets)
        self.dns.side_effect = [records("93.184.216.34", 443), records("8.8.8.8", 443), records("1.1.1.1", 443)]
        result = self.fetch()
        self.assertEqual(result["redirects"], ["https://example.com/next", "https://other.example/final"])
        self.assertEqual(result["final_url"], "https://other.example/final")
        self.assertEqual([sock.connected[0][0] for sock in sockets], ["93.184.216.34", "8.8.8.8", "1.1.1.1"])
        self.assertEqual(self.dns.call_count, 3)
        self.assertTrue(all(sock.closed for sock in sockets))

    def test_five_redirects_allowed_sixth_rejected(self):
        self.install(*(FakeSocket(response(status=301, headers={"Location": f"/{i}"})) for i in range(5)), FakeSocket(response(b"ok")))
        self.assertEqual(len(self.fetch()["redirects"]), 5)
        self.destination.unlink()
        self.install(*(FakeSocket(response(status=308, headers={"Location": f"/{i}"})) for i in range(6)))
        self.assert_code("too_many_redirects", lambda: self.fetch())
        self.assert_clean()

    def test_unsafe_redirects(self):
        cases = [("http://example.com/a", "https_downgrade"), ("https://127.0.0.1/a", "unsafe_address"),
                 ("https://user:secret@example.com/a", "invalid_url"), ("/a#secret", "invalid_redirect"),
                 ("file:///secret", "invalid_url"), (None, "invalid_redirect")]
        for location, code in cases:
            with self.subTest(location=location):
                self.install(FakeSocket(response(status=302, headers={"Location": location} if location else {})))
                before = self.socket_factory.call_count
                self.assert_code(code, lambda: self.fetch())
                self.assertEqual(self.socket_factory.call_count, before + 1)
                self.assert_clean()

    def test_redirect_dns_rebinding_to_private_rejected(self):
        self.install(FakeSocket(response(status=302, headers={"Location": "/next"})))
        self.dns.side_effect = [records("93.184.216.34", 443), records("127.0.0.1", 443)]
        self.assert_code("unsafe_address", lambda: self.fetch())
        self.assertEqual(self.socket_factory.call_count, 1)

    def test_status_mapping_no_retries(self):
        for status, code in [(401, "source_auth_required"), (407, "source_auth_required"), (403, "source_forbidden"),
                             (429, "source_rate_limited"), (404, "http_status"), (500, "http_status"), (300, "http_status")]:
            with self.subTest(status=status):
                self.install(FakeSocket(response(status=status)))
                before = self.socket_factory.call_count
                self.assert_code(code, lambda: self.fetch())
                self.assertEqual(self.socket_factory.call_count, before + 1)
                self.assert_clean()

    def test_referer_validation_and_no_environment_proxy_credentials_or_output(self):
        self.assert_code("invalid_url", lambda: self.fetch(referer="https://user:secret@example.com/a"))
        self.assert_code("invalid_url", lambda: self.fetch(referer="https://example.com/a\r\nAuthorization: secret"))
        self.socket_factory.assert_not_called()
        sock = FakeSocket(response(b"ok"))
        self.install(sock)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://user:secret@127.0.0.1:8080", "NETRC": "/secret"}), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.fetch(referer="https://example.com/page")
        request = b"".join(sock.sent)
        self.assertIn(b"Referer: https://example.com/page", request)
        self.assertNotIn(b"Authorization", request)
        self.assertNotIn(b"secret", request)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_deadline_expired_and_invalid(self):
        self.assert_code("deadline_exceeded", lambda: self.fetch(deadline=time.monotonic() - 1))
        for deadline in [float("inf"), float("nan"), "tomorrow"]:
            self.assert_code("invalid_deadline", lambda deadline=deadline: self.fetch(deadline=deadline))
        self.dns.assert_not_called()
        self.assert_clean()

    def test_slow_drip_body_deadline_and_timeout_shrinks(self):
        clock = Clock()
        sock = FakeSocket(response(b"a" * 200), clock=clock)
        original = sock.recv_into

        def slow(buffer):
            if sock.position >= sock.payload.tobytes().index(b"\r\n\r\n") + 4:
                sock.max_read = 1
                sock.step = 0.2
            else:
                sock.max_read = 1
            return original(buffer)

        sock.recv_into = slow
        self.install(sock)
        with patch.object(network.time, "monotonic", clock):
            self.assert_code("deadline_exceeded", lambda: self.fetch(deadline=101.0))
        self.assertGreater(sock.position, 40)
        self.assertLess(sock.position, len(sock.payload))
        self.assertTrue(all(a >= b for a, b in zip(sock.timeouts, sock.timeouts[1:])))
        self.assertLess(sock.timeouts[-1], sock.timeouts[0])
        self.assertTrue(sock.closed)
        self.assert_clean()

    def test_slow_headers_also_obey_deadline(self):
        clock = Clock()
        sock = FakeSocket(response(b"ok"), clock=clock, step=0.2, max_read=1)
        self.install(sock)
        with patch.object(network.time, "monotonic", clock):
            self.assert_code("deadline_exceeded", lambda: self.fetch(deadline=101.0))
        self.assertLess(sock.position, len(sock.payload))
        self.assert_clean()

    def test_dns_wait_obeys_deadline(self):
        released = threading.Event()
        finished = threading.Event()

        def blocked(host, port, *args):
            try:
                released.wait(2)
                return records(host, port)
            finally:
                finished.set()

        self.dns.side_effect = blocked
        try:
            start = time.monotonic()
            self.assert_code("deadline_exceeded", lambda: self.fetch(deadline=start + 0.05))
            self.assertLess(time.monotonic() - start, 1)
        finally:
            released.set()
            self.assertTrue(finished.wait(2))
        self.socket_factory.assert_not_called()
        self.assert_clean()

    def test_tls_connect_send_and_read_failures_are_sanitized(self):
        for action, error, code in [("connect", OSError("https://user:secret@host/?token=secret"), "io_error"),
                                    ("do_handshake", ssl.SSLCertVerificationError("token=secret"), "tls_error"),
                                    ("sendall", TimeoutError("token=secret"), "deadline_exceeded"),
                                    ("recv_into", OSError("token=secret"), "io_error")]:
            with self.subTest(action=action):
                sock = FakeSocket(response(b"ok"))
                setattr(sock, action, Mock(side_effect=error))
                self.install(sock)
                caught = self.assert_code(code, lambda: self.fetch())
                self.assertTrue(caught.__suppress_context__)
                self.assertTrue(sock.closed)
                self.assert_clean()

    def test_incomplete_and_malformed_body_cleanup(self):
        for payload, code in [(response(b"short", length=100), "incomplete_response"),
                              (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nxyz\r\n", "network_error")]:
            self.install(FakeSocket(payload))
            self.assert_code(code, lambda: self.fetch())
            self.assert_clean()

    def test_existing_destination_and_symlink_preserved(self):
        self.destination.write_bytes(b"existing")
        self.assert_code("destination_exists", lambda: self.fetch())
        self.assertEqual(self.destination.read_bytes(), b"existing")
        self.destination.unlink()
        self.destination.symlink_to(Path(self.directory.name) / "missing")
        self.assert_code("destination_exists", lambda: self.fetch())
        self.assertTrue(self.destination.is_symlink())
        self.socket_factory.assert_not_called()

    def test_concurrent_destination_not_overwritten(self):
        self.install(FakeSocket(response(b"download")))
        original_link = network.os.link

        def race(source, destination):
            Path(destination).write_bytes(b"concurrent")
            return original_link(source, destination)

        with patch.object(network.os, "link", side_effect=race):
            self.assert_code("destination_exists", lambda: self.fetch())
        self.assertEqual(self.destination.read_bytes(), b"concurrent")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.destination])

    def test_replace_failure_cleans_temporary(self):
        self.install(FakeSocket(response(b"ok")))
        with patch.object(network.os, "replace", side_effect=OSError("secret")):
            self.assert_code("io_error", lambda: self.fetch())
        self.assert_clean()

    def test_disk_write_failure_cleans_temporary(self):
        self.install(FakeSocket(response(b"ok")))
        original = network.os.fdopen

        class FailingOutput:
            def __init__(self, fd, mode):
                self.output = original(fd, mode)

            def __enter__(self):
                return self

            def write(self, data):
                raise OSError("disk full secret")

            def __exit__(self, *args):
                self.output.close()

        with patch.object(network.os, "fdopen", FailingOutput):
            self.assert_code("io_error", lambda: self.fetch())
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
