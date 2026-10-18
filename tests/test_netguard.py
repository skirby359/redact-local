"""The network guard blocks connection attempts and the pipeline runs with it on."""

from __future__ import annotations

import os
import socket

import pytest
from conftest import LETTER, run

from redact_local import netguard


@pytest.fixture(autouse=True)
def _clean_record():
    netguard.reset()
    yield
    netguard.reset()


def test_guard_is_on_and_env_is_offline():
    assert netguard.is_installed()
    assert os.environ["TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS"] == ""
    assert os.environ["HF_HUB_OFFLINE"] == "1"


@pytest.mark.parametrize("attempt", [
    lambda: socket.create_connection(("192.0.2.1", 443), timeout=1),
    lambda: socket.getaddrinfo("example.com", 443),
    lambda: socket.gethostbyname("example.com"),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("192.0.2.1", 80)),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.1", 53)),
])
def test_connection_attempts_are_blocked_and_named(attempt):
    with pytest.raises(netguard.NetworkBlockedError) as info:
        attempt()
    assert "test_netguard" in str(info.value)
    assert "example.com" not in str(info.value) and "192.0.2.1" not in str(info.value)
    rec = netguard.attempts()
    assert len(rec) == 1 and rec[0]["module"] == "test_netguard"


def test_urllib_attempt_is_attributed_to_the_caller():
    import urllib.request

    with pytest.raises(OSError):  # urllib wraps the guard's error in URLError
        urllib.request.urlopen("http://192.0.2.1/", timeout=1)
    assert netguard.attempts()[0]["module"] == "test_netguard"


def test_tldextract_uses_the_bundled_snapshot():
    import tldextract

    assert tldextract.extract("mail.example.co.uk").suffix == "co.uk"
    assert netguard.attempts() == []


def test_full_pipeline_runs_with_guard_on(matter):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    result = run(matter)
    assert netguard.is_installed()
    assert netguard.attempts() == []
    assert result.blocked == [] and result.summary["network_attempts_blocked"] == 0
