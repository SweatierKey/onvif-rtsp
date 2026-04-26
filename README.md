# onvif-rtsp

Given an ONVIF device service URL, query the camera and print the RTSP URI of
its first media profile. One URL in, one URL out.

## Install

    chmod +x onvif-rtsp
    cp onvif-rtsp ~/.local/bin/    # or /usr/local/bin/
    pip install -r requirements.txt

## Usage

Resolve a single device, with credentials:

    onvif-rtsp --user admin --password segreta http://192.168.1.64/onvif/device_service

Pipe from `onvif-discover` (one URL per line):

    onvif-discover \
      | xargs -I{} onvif-rtsp --user admin --password segreta {}

Write the result to a file:

    onvif-rtsp --user admin --password segreta \
        -o cam.url \
        http://192.168.1.64/onvif/device_service

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `DEVICE_URL` (positional) | from stdin | ONVIF device service URL (must start with `http://` or `https://`) |
| `--user USER` | unset | ONVIF username; must be paired with `--password` |
| `--password PASSWORD` | unset | ONVIF password; must be paired with `--user` |
| `--inject-credentials` | off | prepend URL-encoded `user:password@` to the RTSP URL (requires `--user`/`--password`; overwrites any userinfo already in the URL and warns on stderr if it does so) |
| `-t`, `--timeout SECONDS` | `10.0` | per-request HTTP timeout |
| `-o`, `--output FILE` | stdout | write the RTSP URL to FILE instead of stdout |
| `-v`, `--verbose` | off | log progress on stderr (passwords are never logged) |
| `-V`, `--version` | | print version and exit |
| `-h`, `--help` | | show help and exit |

If `DEVICE_URL` is omitted, the first non-empty line on stdin is used. If a
positional URL **and** a stdin pipe are both given, the positional argument
wins (standard Unix convention).

The script makes three SOAP calls — `GetCapabilities` (Media), `GetProfiles`,
`GetStreamUri` (RTP-Unicast / RTSP) — and prints whatever URI the device
returns, verbatim, followed by a newline.

When credentials are provided, every request carries a fresh WS-Security
UsernameToken with PasswordDigest (SHA-1). Nonces and timestamps are
regenerated per request.

By default the RTSP URL is printed exactly as the device returned it (no
credentials embedded). If your camera requires HTTP-style auth in the RTSP
URL itself, pass `--inject-credentials` to get
`rtsp://user:password@host/...` (user and password are percent-encoded).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage error (missing/bad arguments, unwritable `-o`, half-set credentials, malformed URL, missing `requests` module) |
| 2 | network error (connection refused, host unreachable, DNS failure, timeout) |
| 3 | authentication failure (HTTP 401 or SOAP `NotAuthorized` fault) |
| 4 | protocol error (non-XML response, unexpected SOAP fault, missing fields, no profiles, etc.) |
| 130 | interrupted with Ctrl-C |

## Dependencies

- Python 3.8+
- [`requests`](https://pypi.org/project/requests/) (see `requirements.txt`)

The script intentionally avoids `python-onvif-zeep`: the SOAP envelopes we need
are short enough to write by hand, and `zeep`/`lxml` are notoriously fragile on
real cameras.

## Place in the chain

`onvif-rtsp` sits after `onvif-discover` and before `go2rtc-gen` /
`rtsp-play` / `rtsp-record`:

    onvif-discover → onvif-rtsp → go2rtc-gen → rtsp-play / rtsp-record → footage-merge

It consumes one ONVIF device service URL (positional or stdin) and emits one
RTSP URL.
