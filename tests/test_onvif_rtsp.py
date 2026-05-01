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


class ResolveCredentialsTests(unittest.TestCase):
    def test_cli_only(self):
        self.assertEqual(orx._resolve_credentials("u", "p", env={}), ("u", "p"))

    def test_env_only(self):
        env = {"ONVIF_USER": "envu", "ONVIF_PASSWORD": "envp"}
        self.assertEqual(orx._resolve_credentials(None, None, env=env), ("envu", "envp"))

    def test_cli_overrides_env(self):
        env = {"ONVIF_USER": "envu", "ONVIF_PASSWORD": "envp"}
        self.assertEqual(orx._resolve_credentials("cli", "clip", env=env), ("cli", "clip"))

    def test_half_set_env_raises(self):
        with self.assertRaises(orx._Err):
            orx._resolve_credentials(None, None, env={"ONVIF_USER": "u"})
        with self.assertRaises(orx._Err):
            orx._resolve_credentials(None, None, env={"ONVIF_PASSWORD": "p"})

    def test_empty_env_treated_as_unset(self):
        self.assertEqual(
            orx._resolve_credentials(None, None,
                                     env={"ONVIF_USER": "", "ONVIF_PASSWORD": ""}),
            (None, None),
        )


class SelectProfileTokenTests(unittest.TestCase):
    profiles = [("tokA", "MainStream"), ("tokB", "SubStream"), ("tokC", "Audio")]

    def test_default_first(self):
        self.assertEqual(orx.select_profile_token(self.profiles, 0, None), "tokA")

    def test_index(self):
        self.assertEqual(orx.select_profile_token(self.profiles, 1, None), "tokB")

    def test_name_match(self):
        self.assertEqual(orx.select_profile_token(self.profiles, 0, "SubStream"), "tokB")

    def test_token_match_via_name(self):
        # Real-world: some integrators only know the token, not the friendly name.
        self.assertEqual(orx.select_profile_token(self.profiles, 0, "tokC"), "tokC")

    def test_name_wins_over_index(self):
        self.assertEqual(orx.select_profile_token(self.profiles, 99, "MainStream"), "tokA")

    def test_index_out_of_range(self):
        with self.assertRaises(orx.ProtocolError):
            orx.select_profile_token(self.profiles, 5, None)

    def test_name_not_found(self):
        with self.assertRaises(orx.ProtocolError):
            orx.select_profile_token(self.profiles, 0, "nope")


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


class InjectCredentialsUnitTests(unittest.TestCase):
    def test_basic_injection(self):
        new, replaced = orx.inject_credentials(
            "rtsp://192.168.0.73/live/ch00_1", "admin", "admin",
        )
        self.assertEqual(new, "rtsp://admin:admin@192.168.0.73/live/ch00_1")
        self.assertFalse(replaced)

    def test_preserves_port_and_query(self):
        new, _ = orx.inject_credentials(
            "rtsp://cam:554/path?x=1", "u", "p",
        )
        self.assertEqual(new, "rtsp://u:p@cam:554/path?x=1")

    def test_url_encodes_special_chars(self):
        new, _ = orx.inject_credentials(
            "rtsp://cam/s", "us er", "p@ss:wd/!",
        )
        # @, :, /, space, ! must be percent-encoded inside userinfo.
        self.assertEqual(new, "rtsp://us%20er:p%40ss%3Awd%2F%21@cam/s")

    def test_overwrites_existing_userinfo(self):
        new, replaced = orx.inject_credentials(
            "rtsp://old:old@cam/s", "new", "new",
        )
        self.assertEqual(new, "rtsp://new:new@cam/s")
        self.assertTrue(replaced)


class CliInjectCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.cam = _happy_cam()

    def tearDown(self):
        self.cam.stop()

    def test_inject_prepends_creds(self):
        r = _run_cli([
            "--user", "admin", "--password", "admin",
            "--inject-credentials",
            self.cam.url("/onvif/device_service"),
        ])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            r.stdout.strip(),
            "rtsp://admin:admin@127.0.0.1:554/Streaming/Channels/101",
        )

    def test_inject_without_credentials_errors(self):
        r = _run_cli([
            "--inject-credentials",
            self.cam.url("/onvif/device_service"),
        ])
        self.assertEqual(r.returncode, 1)
        self.assertIn("--inject-credentials requires credentials", r.stderr)

    def test_credentials_via_env(self):
        # Same as test_inject_prepends_creds but credentials come via env
        # vars instead of CLI flags — what nvrd uses to keep them out of
        # `/proc/*/cmdline`.
        env = dict(os.environ)
        env["ONVIF_USER"] = "admin"
        env["ONVIF_PASSWORD"] = "admin"
        r = _run_cli([
            "--inject-credentials",
            self.cam.url("/onvif/device_service"),
        ], env=env)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            r.stdout.strip(),
            "rtsp://admin:admin@127.0.0.1:554/Streaming/Channels/101",
        )

    def test_password_via_env_alone_errors(self):
        env = dict(os.environ); env["ONVIF_PASSWORD"] = "secret"
        r = _run_cli([self.cam.url("/onvif/device_service")], env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("password given without username", r.stderr)


class CliProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cam = _happy_cam()

    def tearDown(self):
        self.cam.stop()

    def test_default_picks_first_profile(self):
        r = _run_cli([self.cam.url("/onvif/device_service")])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # The fake server returns the same Uri regardless of token, so what
        # we want to check is which token GetStreamUri was called with.
        stream_bodies = [b for path, action, b in self.cam.requests
                         if action.endswith("GetStreamUri")]
        self.assertEqual(len(stream_bodies), 1)
        self.assertIn(b"<trt:ProfileToken>MainProfile</trt:ProfileToken>",
                      stream_bodies[0])

    def test_profile_index_one_picks_substream(self):
        r = _run_cli(["--profile-index", "1",
                      self.cam.url("/onvif/device_service")])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        stream_bodies = [b for path, action, b in self.cam.requests
                         if action.endswith("GetStreamUri")]
        self.assertIn(b"<trt:ProfileToken>SubProfile</trt:ProfileToken>",
                      stream_bodies[0])

    def test_profile_name_match(self):
        r = _run_cli(["--profile-name", "P1",  # P1 is the second one
                      self.cam.url("/onvif/device_service")])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        stream_bodies = [b for path, action, b in self.cam.requests
                         if action.endswith("GetStreamUri")]
        self.assertIn(b"<trt:ProfileToken>SubProfile</trt:ProfileToken>",
                      stream_bodies[0])

    def test_profile_index_out_of_range(self):
        r = _run_cli(["--profile-index", "9",
                      self.cam.url("/onvif/device_service")])
        self.assertEqual(r.returncode, 4)
        self.assertIn("out of range", r.stderr)

    def test_list_profiles(self):
        r = _run_cli(["--list-profiles",
                      self.cam.url("/onvif/device_service")])
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # One TOKEN<TAB>NAME line per profile.
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "MainProfile\tP0")
        self.assertEqual(lines[1], "SubProfile\tP1")
        # GetStreamUri must NOT have been called in --list-profiles mode.
        actions = [action for path, action, b in self.cam.requests]
        self.assertNotIn(
            "http://www.onvif.org/ver10/media/wsdl/GetStreamUri",
            actions,
        )


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
        self.assertIn("password given without username", r.stderr)

    def test_user_without_password(self):
        r = _run_cli(["--user", "x", "http://1.2.3.4/onvif/device_service"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("username given without password", r.stderr)

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
