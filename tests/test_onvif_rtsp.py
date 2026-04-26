"""Tests for onvif-rtsp.

Use a small in-process HTTP server on an ephemeral port as the fake camera.
No real network and no third-party services touched.
"""

import importlib.util
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "onvif-rtsp"


def _load_module():
    loader = SourceFileLoader("onvif_rtsp", str(SCRIPT))
    spec = importlib.util.spec_from_loader("onvif_rtsp", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


orx = _load_module()


# ---------------------------------------------------------------------------
# Mock SOAP responses
# ---------------------------------------------------------------------------

def _caps_response(media_xaddr: str) -> bytes:
    return ("""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <tds:GetCapabilitiesResponse>
      <tds:Capabilities>
        <tt:Media>
          <tt:XAddr>""" + media_xaddr + """</tt:XAddr>
        </tt:Media>
      </tds:Capabilities>
    </tds:GetCapabilitiesResponse>
  </s:Body>
</s:Envelope>""").encode("utf-8")


def _profiles_response(tokens) -> bytes:
    inner = "".join(
        f'<trt:Profiles fixed="true" token="{t}"><tt:Name>P{i}</tt:Name></trt:Profiles>'
        for i, t in enumerate(tokens)
    )
    return ("""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetProfilesResponse>""" + inner + """</trt:GetProfilesResponse>
  </s:Body>
</s:Envelope>""").encode("utf-8")


def _stream_response(uri: str) -> bytes:
    return ("""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetStreamUriResponse>
      <trt:MediaUri>
        <tt:Uri>""" + uri + """</tt:Uri>
      </trt:MediaUri>
    </trt:GetStreamUriResponse>
  </s:Body>
</s:Envelope>""").encode("utf-8")


SOAP_FAULT_NOT_AUTHORIZED = ("""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Body>
    <s:Fault>
      <s:Code>
        <s:Value>s:Sender</s:Value>
        <s:Subcode>
          <s:Value xmlns:ter="http://www.onvif.org/ver10/error">ter:NotAuthorized</s:Value>
        </s:Subcode>
      </s:Code>
      <s:Reason><s:Text xml:lang="en">Sender not authorized</s:Text></s:Reason>
    </s:Fault>
  </s:Body>
</s:Envelope>""").encode("utf-8")


# ---------------------------------------------------------------------------
# Mock camera HTTP server
# ---------------------------------------------------------------------------

class FakeCam:
    """Single mock server hosting both a /device_service and a /media endpoint.

    Configure responses per path/action by setting `self.responses[path]` to a
    callable receiving the request body and returning (status, body_bytes).
    """

    def __init__(self):
        self.responses = {}
        self.requests = []  # list of (path, action, body)
        self._server = None
        self._thread = None

    def start(self):
        cam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return  # silence

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                ct = self.headers.get("Content-Type", "")
                m = re.search(r'action="([^"]+)"', ct)
                action = m.group(1) if m else ""
                cam.requests.append((self.path, action, body))
                handler = cam.responses.get(self.path)
                if handler is None:
                    self.send_response(404); self.end_headers(); return
                status, payload = handler(body, action)
                self.send_response(status)
                self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(args, stdin_text=None, env=None, timeout=10):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
        input=stdin_text, env=env, timeout=timeout,
    )


def _happy_cam():
    cam = FakeCam()
    cam.start()
    media_url = cam.url("/onvif/Media")
    cam.responses["/onvif/device_service"] = lambda b, a: (200, _caps_response(media_url))
    cam.responses["/onvif/Media"] = (
        lambda b, a:
            (200, _profiles_response(["MainProfile", "SubProfile"]))
            if a.endswith("GetProfiles")
            else (200, _stream_response("rtsp://127.0.0.1:554/Streaming/Channels/101"))
    )
    return cam


# ---------------------------------------------------------------------------
# Unit-level tests
# ---------------------------------------------------------------------------

class WSSecurityTests(unittest.TestCase):
    def test_nonce_changes_per_call(self):
        a = orx._make_security_header("u", "p")
        b = orx._make_security_header("u", "p")
        self.assertNotEqual(a, b)
        self.assertIn("<wsse:Username>u</wsse:Username>", a)
        # Password must NEVER be embedded in cleartext.
        self.assertNotIn(">p<", a)
        self.assertNotIn("p</wsse:Password>", a)

    def test_xml_escapes_username(self):
        # saxutils.escape only escapes &, <, > inside element text by default.
        # That's enough — " is only required to be escaped inside attribute values.
        h = orx._make_security_header('admin&<>', "x")
        self.assertIn("admin&amp;&lt;&gt;", h)


class ValidationTests(unittest.TestCase):
    def test_url_must_be_http(self):
        with self.assertRaises(orx._Err) as cm:
            orx._validate_url("foo://bar")
        self.assertEqual(cm.exception.code, 1)

    def test_credentials_half_set_raises(self):
        with self.assertRaises(orx._Err):
            orx._validate_credentials("admin", None)
        with self.assertRaises(orx._Err):
            orx._validate_credentials(None, "secret")

    def test_credentials_both_set_or_both_unset(self):
        self.assertEqual(orx._validate_credentials(None, None), (None, None))
        self.assertEqual(orx._validate_credentials("u", "p"), ("u", "p"))
        self.assertEqual(orx._validate_credentials("", ""), (None, None))


# ---------------------------------------------------------------------------
# End-to-end CLI tests via the fake camera
# ---------------------------------------------------------------------------

class CliHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.cam = _happy_cam()

    def tearDown(self):
        self.cam.stop()

    def test_happy_path(self):
        r = _run_cli([self.cam.url("/onvif/device_service")])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            r.stdout.strip(),
            "rtsp://127.0.0.1:554/Streaming/Channels/101",
        )

    def test_url_from_stdin(self):
        r = _run_cli([], stdin_text=self.cam.url("/onvif/device_service") + "\n")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            r.stdout.strip(),
            "rtsp://127.0.0.1:554/Streaming/Channels/101",
        )

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "url.txt")
            r = _run_cli(["-o", target, self.cam.url("/onvif/device_service")])
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(r.stdout, "")
            with open(target) as f:
                self.assertEqual(f.read(), "rtsp://127.0.0.1:554/Streaming/Channels/101\n")

    def test_credentials_emit_security_header(self):
        r = _run_cli([
            "--user", "admin", "--password", "topsecret",
            self.cam.url("/onvif/device_service"),
        ])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # All three requests should carry a Security header.
        seen = [body for _, _, body in self.cam.requests]
        self.assertEqual(len(seen), 3)
        for body in seen:
            self.assertIn(b"wsse:Security", body)
            # password must NEVER appear in cleartext on the wire
            self.assertNotIn(b"topsecret", body)


class CliErrorPathTests(unittest.TestCase):
    def test_http_401(self):
        cam = FakeCam(); cam.start()
        cam.responses["/onvif/device_service"] = lambda b, a: (401, b"")
        try:
            r = _run_cli([cam.url("/onvif/device_service")])
            self.assertEqual(r.returncode, 3, msg=r.stderr)
            self.assertIn("authentication failed", r.stderr)
        finally:
            cam.stop()

    def test_soap_fault_not_authorized(self):
        cam = FakeCam(); cam.start()
        cam.responses["/onvif/device_service"] = lambda b, a: (400, SOAP_FAULT_NOT_AUTHORIZED)
        try:
            r = _run_cli([cam.url("/onvif/device_service")])
            self.assertEqual(r.returncode, 3, msg=r.stderr)
            self.assertIn("authentication failed", r.stderr)
        finally:
            cam.stop()

    def test_no_profiles(self):
        cam = FakeCam(); cam.start()
        media_url = cam.url("/onvif/Media")
        cam.responses["/onvif/device_service"] = lambda b, a: (200, _caps_response(media_url))
        cam.responses["/onvif/Media"] = lambda b, a: (200, _profiles_response([]))
        try:
            r = _run_cli([cam.url("/onvif/device_service")])
            self.assertEqual(r.returncode, 4, msg=r.stderr)
            self.assertIn("no media profiles", r.stderr)
        finally:
            cam.stop()

    def test_missing_url_with_tty_stdin(self):
        # Force isatty=True by closing stdin (subprocess default is empty pipe;
        # we point stdin at /dev/null instead, then patch via env). The cleanest
        # way: just don't send stdin and check we get the right error if stdin
        # is a tty. In subprocess we can't easily simulate a tty without pty,
        # so we run the script with no stdin and rely on its stdin.isatty().
        # On most CI/test envs, stdin from subprocess is a closed/empty pipe
        # (isatty() == False), which would trigger the "empty stdin" error
        # rather than the tty error. Use a pty for a faithful test.
        try:
            import pty
        except ImportError:
            self.skipTest("pty not available")
        master, slave = pty.openpty()
        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPT)],
                stdin=slave, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(r.returncode, 1)
            self.assertIn("stdin is a terminal", r.stderr)
        finally:
            os.close(master); os.close(slave)

    def test_empty_stdin(self):
        r = _run_cli([], stdin_text="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no device URL on stdin", r.stderr)

    def test_malformed_url(self):
        r = _run_cli(["foo://bar"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("not an HTTP(S) URL", r.stderr)

    def test_password_without_user(self):
        r = _run_cli(["--password", "x", "http://1.2.3.4/onvif/device_service"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("--password given without --user", r.stderr)

    def test_user_without_password(self):
        r = _run_cli(["--user", "x", "http://1.2.3.4/onvif/device_service"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("--user given without --password", r.stderr)

    def test_connection_refused(self):
        # Bind+release a port so we know it's free, then point the script at it.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = _run_cli([f"http://127.0.0.1:{port}/onvif/device_service", "-t", "2"])
        self.assertEqual(r.returncode, 2, msg=r.stderr)


class CliMetaTests(unittest.TestCase):
    def test_version(self):
        r = _run_cli(["-V"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"{orx.PROG} {orx.VERSION}")

    def test_help(self):
        r = _run_cli(["-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("ONVIF", r.stdout)
        self.assertEqual(r.stderr, "")


if __name__ == "__main__":
    unittest.main()
