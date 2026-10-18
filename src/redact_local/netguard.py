"""In-process network guard: make any attempt to phone home fail loudly.

Neither Presidio nor spaCy documents any telemetry, but "don't trust, enforce".
``install()`` runs before Presidio, spaCy, PyMuPDF or RapidOCR is imported. It:

1. Sets environment switches that keep known callers offline:
   - ``TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS=""``. tldextract (used by Presidio's
     email recognizer) otherwise fetches the Public Suffix List on first use.
     With no URLs it uses the snapshot bundled in the package.
   - ``HF_HUB_OFFLINE=1`` / ``TRANSFORMERS_OFFLINE=1``, in case a Hugging Face
     library is ever pulled in.
   - ``PRESIDIO_DEVICE=cpu``, so Presidio doesn't probe for a GPU.
2. Replaces ``socket.socket.connect``/``connect_ex``/``sendto``,
   ``socket.create_connection``, ``getaddrinfo`` and the ``gethostby*`` lookups
   with a function that raises ``NetworkBlockedError`` naming the module that
   tried.

Every attempt is also recorded, so the CLI fails the run even if a library
catches the error and carries on.

Limits, stated plainly: this is in-process only. It covers Python code in this
process that goes through the ``socket`` module. It does not cover native code
that opens sockets on its own, or child processes. It is a tripwire, not a
firewall. For a hard guarantee, block the venv's python.exe in Windows Firewall.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

OFFLINE_ENV = {
    "TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS": "",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "PRESIDIO_DEVICE": "cpu",
    "DO_NOT_TRACK": "1",
}

# Modules that only carry a connection on behalf of someone else. The guard
# reports the first caller that is NOT one of these as the one responsible.
_TRANSPORT = (
    "socket", "ssl", "http.client", "urllib", "urllib3", "requests", "httpx",
    "httpcore", "h11", "asyncio", "selectors", "aiohttp", "anyio", "smart_open",
    "requests_file", "email.utils", "importlib", "runpy",
)


class NetworkBlockedError(ConnectionError):
    """Raised when code in this process tries to use the network."""


_lock = threading.Lock()
_attempts: list[dict] = []
_originals: dict[str, object] = {}
_installed = False


def _is_transport(module: str) -> bool:
    return any(module == t or module.startswith(t + ".") for t in _TRANSPORT)


def _callers() -> tuple[str, str]:
    """Return (immediate caller, first non-transport caller) module names."""
    frame = sys._getframe(2)
    immediate = None
    while frame is not None:
        module = frame.f_globals.get("__name__", "?")
        if module != __name__:
            if immediate is None:
                immediate = module
            if not _is_transport(module):
                return immediate, module
        frame = frame.f_back
    return immediate or "?", immediate or "?"


def _blocker(call: str):
    def blocked(*args, **kwargs):
        immediate, origin = _callers()
        if call.startswith("socket.connect"):
            target = args[1] if len(args) > 1 else None  # (self, address)
        elif call == "socket.sendto":
            target = args[-1] if len(args) > 2 else None  # address is last; never the payload
        else:
            target = args[0] if args else None  # host or (host, port)
        with _lock:
            _attempts.append(
                {"call": call, "module": origin, "via": immediate, "target": repr(target)}
            )
        # The target is kept out of the message on purpose: a lookup could in
        # principle be for a domain taken from a document. It goes to the
        # private run.log via attempts().
        raise NetworkBlockedError(
            f"redact-local blocked network access: module '{origin}' called {call}"
            + (f" (via '{immediate}')" if immediate != origin else "")
            + ". Nothing was sent. This run will be marked as failed."
        )

    blocked.__name__ = f"blocked_{call.replace('.', '_')}"
    return blocked


_PATCHES = [
    (socket.socket, "connect", "socket.connect"),
    (socket.socket, "connect_ex", "socket.connect_ex"),
    (socket.socket, "sendto", "socket.sendto"),
    (socket, "create_connection", "socket.create_connection"),
    (socket, "getaddrinfo", "socket.getaddrinfo"),
    (socket, "gethostbyname", "socket.gethostbyname"),
    (socket, "gethostbyname_ex", "socket.gethostbyname_ex"),
    (socket, "gethostbyaddr", "socket.gethostbyaddr"),
]


def install() -> None:
    """Set offline env switches and block socket use. Safe to call repeatedly."""
    global _installed
    for key, value in OFFLINE_ENV.items():
        os.environ[key] = value
    with _lock:
        if _installed:
            return
        for owner, attr, call in _PATCHES:
            _originals[call] = getattr(owner, attr)
            setattr(owner, attr, _blocker(call))
        _installed = True


def uninstall() -> None:
    """Restore the real socket functions (tests only)."""
    global _installed
    with _lock:
        if not _installed:
            return
        for owner, attr, call in _PATCHES:
            setattr(owner, attr, _originals[call])
        _installed = False


def is_installed() -> bool:
    return _installed


def attempts() -> list[dict]:
    """Every blocked attempt so far. ``target`` is for the private log only."""
    with _lock:
        return [dict(a) for a in _attempts]


def reset() -> None:
    with _lock:
        _attempts.clear()
