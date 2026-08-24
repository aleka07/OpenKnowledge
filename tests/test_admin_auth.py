"""Auth machinery of the admin panel: passwords, sessions, CSRF, login flow.

These run fully in-process (Starlette TestClient) — no server, no DB.
Only routes that don't touch Postgres are exercised here.
"""

import time

import pytest
from starlette.testclient import TestClient

from okb import admin
from okb.admin import (app, hash_password, make_session, verify_password,
                       _sign, _unsign)
from okb.config import settings


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    monkeypatch.setattr(settings, "admin_password_hash", hash_password("correct-horse"))
    monkeypatch.setattr(settings, "admin_secret", "test-secret-not-for-prod")
    admin._login_failures.clear()


@pytest.fixture()
def client():
    # https base_url so the Secure session cookie is kept by the test client
    return TestClient(app, base_url="https://testserver")


# --- passwords --------------------------------------------------------------

def test_password_roundtrip():
    stored = hash_password("s3cret")
    assert verify_password("s3cret", stored)
    assert not verify_password("wrong", stored)


def test_malformed_stored_hash_never_verifies():
    for bad in ["", "plaintext", "scrypt$zz$zz", "md5$a$b"]:
        assert not verify_password("anything", bad)


# --- sessions ---------------------------------------------------------------

def test_session_sign_unsign_roundtrip():
    data = _unsign(make_session())
    assert data is not None and "csrf" in data


def test_tampered_session_rejected():
    token = make_session()
    assert _unsign(token[:-4] + "AAAA") is None


def test_expired_session_rejected():
    stale = _sign(b'{"exp": 1, "csrf": "x"}')
    assert _unsign(stale) is None


def test_session_signed_with_other_secret_rejected(monkeypatch):
    token = make_session()
    monkeypatch.setattr(settings, "admin_secret", "rotated")
    assert _unsign(token) is None  # rotating the secret logs everyone out


# --- http flow --------------------------------------------------------------

def test_unauthenticated_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_healthz_is_public(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_wrong_password_no_cookie(client):
    r = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_good_password_sets_session(client):
    r = client.post("/login", data={"password": "correct-horse"},
                    follow_redirects=False)
    assert r.status_code == 303 and admin.SESSION_COOKIE in r.cookies


def test_login_rate_limit(client):
    for _ in range(admin.LOGIN_MAX_FAILURES):
        client.post("/login", data={"password": "nope"})
    r = client.post("/login", data={"password": "correct-horse"},
                    follow_redirects=False)
    assert r.status_code == 200  # locked out even with the right password


def test_post_without_csrf_rejected(client):
    # follow_redirects=False: the post-login redirect lands on the dashboard,
    # which needs the DB — auth machinery itself must not
    client.post("/login", data={"password": "correct-horse"}, follow_redirects=False)
    r = client.post("/actions/ingest", data={"limit": "10"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_post_with_forged_csrf_rejected(client):
    client.post("/login", data={"password": "correct-horse"}, follow_redirects=False)
    r = client.post("/actions/ingest", data={"limit": "10", "csrf": "forged"},
                    follow_redirects=False)
    assert r.status_code == 403
