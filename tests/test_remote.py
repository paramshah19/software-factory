"""Phone view (PIN, cookie, allowed actions), keep-awake and the QR code."""

import http.client
import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import awake
import dashboard
import qr
import remote


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(port, method, path, body=None, cookie=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"X-SF-Dashboard": "1", "Content-Type": "application/json", **(headers or {})}
    if cookie:
        h["Cookie"] = cookie
    conn.request(method, path, json.dumps(body) if body is not None else None, h)
    r = conn.getresponse()
    data = r.read()
    out = (r.status, data, dict(r.getheaders()))
    conn.close()
    return out


@pytest.fixture
def servers(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard.factory, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "runs").mkdir()
    local = ThreadingHTTPServer(("127.0.0.1", free_port()), dashboard.Handler)
    local.daemon_threads = True
    threading.Thread(target=local.serve_forever, daemon=True).start()
    dashboard.PHONE.base_port = free_port() - 1
    yield local.server_address[1]
    dashboard.PHONE.stop()
    dashboard.AWAKE.stop()
    local.shutdown()
    local.server_close()


def turn_on(local):
    status, data, _ = call(local, "POST", "/api/remote", {"on": True})
    assert status == 200
    info = json.loads(data)
    assert info["on"] and len(info["pin"]) == 6
    return info, dashboard.PHONE.port


# ------------------------------------------------------------------ the phone view


def test_phone_view_is_off_by_default_and_closed_when_off(servers):
    assert json.loads(call(servers, "GET", "/api/remote")[1]) == {"on": False}
    assert dashboard.PHONE.port is None


def test_pin_signs_in_and_cookie_is_http_only(servers):
    info, port = turn_on(servers)
    assert call(port, "GET", "/")[0] == 401 and b"PIN" in call(port, "GET", "/")[1]   # login page, not the dashboard
    assert call(port, "GET", "/api/state")[0] == 401
    status, _, h = call(port, "GET", "/?pin=" + info["pin"])
    assert status == 303 and h["Location"] == "/"
    cookie = h["Set-Cookie"].split(";")[0]
    assert "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
    assert call(port, "GET", "/", cookie=cookie)[0] == 200
    assert call(port, "GET", "/api/state", cookie=cookie)[0] == 200
    assert json.loads(call(port, "GET", "/api/info", cookie=cookie)[1])["remote"] is True
    assert h["Referrer-Policy"] == "no-referrer"


def test_login_form_and_wrong_pins_lock_the_device(servers):
    info, port = turn_on(servers)
    wrong = "000000" if info["pin"] != "000000" else "111111"
    for _ in range(remote.LOCK_AFTER):
        assert call(port, "POST", "/api/login", {"pin": wrong})[0] == 401
    status, data, _ = call(port, "POST", "/api/login", {"pin": info["pin"]})   # even the right PIN waits during the lock
    assert status == 429 and b"try again" in data
    assert call(port, "POST", "/api/login", {"pin": info["pin"]}, headers={"X-SF-Dashboard": "0"})[0] in (403, 429)


def test_login_with_right_pin_sets_cookie(servers):
    info, port = turn_on(servers)
    status, _, h = call(port, "POST", "/api/login", {"pin": info["pin"]})
    assert status == 200 and h["Set-Cookie"].startswith("sf_remote=")


def test_phone_can_answer_gates_but_not_start_runs_store_keys_or_deploy(servers):
    info, port = turn_on(servers)
    cookie = call(port, "GET", "/?pin=" + info["pin"])[2]["Set-Cookie"].split(";")[0]
    for path in ("/api/new", "/api/keys", "/api/deploy", "/api/always", "/api/skills/upload", "/api/open", "/api/remote") * 8:   # repeated: a refusal must never drop the connection
        assert call(port, "POST", path, {"filler": "x" * 5000}, cookie=cookie)[0] == 403, path
    for path in ("/api/always", "/api/skills", "/api/deploy-options", "/api/remote"):
        assert call(port, "GET", path, cookie=cookie)[0] == 403, path
    assert call(port, "POST", "/api/approve", {"run": "nope"}, cookie=cookie)[0] == 404   # allowed, then "unknown run"
    assert call(port, "POST", "/api/approve", {"run": "nope"})[0] == 401                  # no cookie, no entry
    assert call(port, "POST", "/api/awake", {"on": False}, cookie=cookie)[0] == 200


def test_turning_it_off_closes_the_door_and_signs_everyone_out(servers):
    info, port = turn_on(servers)
    cookie = call(port, "GET", "/?pin=" + info["pin"])[2]["Set-Cookie"].split(";")[0]
    assert json.loads(call(servers, "POST", "/api/remote", {"on": False})[1]) == {"on": False}
    with pytest.raises(OSError):
        call(port, "GET", "/", cookie=cookie)
    _, port2 = turn_on(servers)   # a new PIN and a new set of signed-in phones
    assert call(port2, "GET", "/api/state", cookie=cookie)[0] == 401   # the old cookie is worthless


def test_rotate_replaces_the_pin_and_signs_phones_out(servers):
    info, port = turn_on(servers)
    cookie = call(port, "GET", "/?pin=" + info["pin"])[2]["Set-Cookie"].split(";")[0]
    new = json.loads(call(servers, "POST", "/api/remote", {"on": True, "rotate": True})[1])
    assert call(port, "GET", "/api/state", cookie=cookie)[0] == 401
    assert call(port, "GET", "/?pin=" + new["pin"])[0] == 303


def test_loopback_dashboard_refuses_other_host_names(servers):
    assert call(servers, "GET", "/api/info")[0] == 200
    assert call(servers, "GET", "/api/info", headers={"Host": "evil.example:8787"})[0] == 403
    assert call(servers, "POST", "/api/remote", {"on": True}, headers={"Host": "evil.example"})[0] == 403
    assert dashboard.PHONE.on is False


def test_a_pin_guessing_attack_replaces_the_pin():
    t = [0.0]
    auth = remote.RemoteAuth(clock=lambda: t[0])
    pin = auth.reset()
    for i in range(remote.ROTATE_AFTER):
        auth.login(f"10.0.0.{i}", "999999" if pin != "999999" else "888888")   # many devices, each under the lock limit
    assert auth.pin != pin and auth.rotated_by_attack
    assert auth.login("10.9.9.9", pin)[0] is None   # the PIN they were guessing against no longer opens anything


def test_lock_expires():
    t = [0.0]
    auth = remote.RemoteAuth(clock=lambda: t[0])
    pin = auth.reset()
    for _ in range(remote.LOCK_AFTER):
        auth.login("a", "x")
    assert auth.login("a", pin)[0] is None
    t[0] += remote.LOCK_SECONDS + 1
    token, _ = auth.login("a", pin)
    assert token and auth.valid(token) and not auth.valid("nope") and not auth.valid(None)


def test_lan_addresses_are_never_loopback():
    assert all(not ip.startswith("127.") for ip in remote.lan_addresses())


# ------------------------------------------------------------------ keep awake


def test_keep_awake_modes_and_limits(monkeypatch):
    calls = []
    monkeypatch.setattr(awake.KeepAwake, "how", staticmethod(lambda: "test"))
    monkeypatch.setattr(awake.KeepAwake, "_hold", lambda self: calls.append("hold"))
    monkeypatch.setattr(awake.KeepAwake, "_release", lambda self: calls.append("release"))
    busy = [True]
    k = awake.KeepAwake(busy=lambda: busy[0], tick=0.02)
    monkeypatch.setattr(awake, "IDLE_GRACE", 0.1)
    st = k.start("runs")
    assert st["on"] and st["mode"] == "runs" and st["until"] - st["started"] == awake.MAX_HOURS * 3600
    time.sleep(0.3)
    assert k.status()["on"]                      # a run is busy: stays on
    busy[0] = False
    for _ in range(100):
        if not k.status()["on"]:
            break
        time.sleep(0.05)
    st = k.status()
    assert not st["on"] and st["reason"] == "all runs finished" and calls == ["hold", "release"]
    assert k.start("hours", hours=99)["until"] - k.status()["started"] == awake.MAX_HOURS * 3600   # capped
    assert k.stop()["on"] is False and calls[-1] == "release"
    with pytest.raises(ValueError):
        k.start("forever")


def test_keep_awake_hours_mode_expires(monkeypatch):
    monkeypatch.setattr(awake.KeepAwake, "how", staticmethod(lambda: "test"))
    monkeypatch.setattr(awake.KeepAwake, "_hold", lambda self: None)
    monkeypatch.setattr(awake.KeepAwake, "_release", lambda self: None)
    k = awake.KeepAwake(tick=0.02)
    k.start("hours", hours=0.25)
    k.until = time.time() + 0.1
    for _ in range(100):
        if not k.status()["on"]:
            break
        time.sleep(0.05)
    assert not k.status()["on"] and k.status()["reason"] == "time was up"


def test_keep_awake_unsupported_system(monkeypatch):
    monkeypatch.setattr(awake.KeepAwake, "how", staticmethod(lambda: None))
    with pytest.raises(RuntimeError):
        awake.KeepAwake().start()


@pytest.mark.skipif(not hasattr(__import__("ctypes"), "windll"), reason="Windows only")
def test_keep_awake_really_asks_windows(monkeypatch):
    """The real call: SetThreadExecutionState returns the previous state (non-zero) when it works."""
    import ctypes
    k = awake.KeepAwake(tick=0.05)
    k.start("hours", hours=0.25)
    time.sleep(0.2)
    assert k.status()["on"]
    k.stop()
    assert not k.status()["on"] and ctypes.windll.kernel32.SetThreadExecutionState(awake.WIN_CONTINUOUS) != 0


# ------------------------------------------------------------------ QR code


def test_qr_matrix_shape_and_svg():
    m = qr.matrix("http://192.168.1.20:8788/?pin=123456")
    n = len(m)
    assert n == 29 and all(len(r) == n for r in m)                       # version 3
    assert all(m[0][i] for i in range(7)) and all(m[i][0] for i in range(7))   # finder pattern corner
    s = qr.svg("http://192.168.1.20:8788/?pin=123456")
    assert s.startswith("<svg") and "viewBox" in s and qr.svg("a") != s
    with pytest.raises(ValueError):
        qr.matrix("x" * 200)


def test_odd_pins_and_cookies_are_refused_not_crashes():
    auth = remote.RemoteAuth()
    auth.reset()
    assert auth.login("a", "é中😀")[0] is None and auth.login("a", "")[0] is None
    assert not auth.valid("é中") and not auth.valid("")
