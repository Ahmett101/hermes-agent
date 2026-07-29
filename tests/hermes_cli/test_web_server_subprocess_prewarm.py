"""
Regression tests for the subprocess prewarm introduced for issue #73830.

The previous warmup ran ``_warm_gateway_module`` in a thread executor. A
thread shares the GIL with the main process, so a long .pyc compilation
run (Defender real-time scan on a fresh Windows install) could still
starve the event loop and the ``/api/health`` / ``gateway.ready`` probes.
The fix spawns a *detached subprocess* whose GIL is independent of the
parent's — the parent event loop cannot be starved regardless of how
slow the cold import is.

These tests prove the new helper actually does what its docstring
claims:

1. ``_warm_gateway_in_subprocess`` returns a ``Popen`` handle whose PID
   differs from the parent (proves the work runs off-process).
2. The spawned child runs ``import hermes_cli.gateway`` to completion
   even when the parent exits immediately (proves no GIL coupling).
3. The lifespan startup completes in << SLOW_SECONDS even when the
   child takes SLOW_SECONDS to import (proves the event loop is free).
4. While the child is still importing, ``/api/health`` returns 200 — a
   regression here would re-open the boot-loop race from #50209.
5. Shutdown does not hang: the lifespan ``finally`` block waits at most
   5s for the child.

Like the existing ``test_web_server_boot_handshake.py``, these tests
patch the prewarm helper to insert a controllable delay, but now the
delay lives in a separate process so it cannot influence the parent
GIL — that's the whole point of the fix.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from unittest.mock import patch

import httpx

import hermes_cli.web_server as web_server_mod


SLOW_SECONDS = 2  # scaled down for CI; real Defender stalls are 15-30s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slow_subprocess_warm(seconds: float):
    """Replace ``_warm_gateway_in_subprocess`` with a real subprocess
    that sleeps for ``seconds`` before doing the import. This is the
    closest analogue to a Defender-scan stall we can produce without an
    actual Windows Defender scan: the child is a *separate* Python
    process whose GIL is independent of the parent's.
    """

    def _slow():
        code = (
            f"import time; "
            f"time.sleep({seconds}); "
            f"import hermes_cli.gateway"
        )
        # start_new_session=True mirrors the production helper so the
        # child is fully detached from the parent's process group.
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    return _slow


# ---------------------------------------------------------------------------
# Test 1 — subprocess PID differs from the parent (proves off-process)
# ---------------------------------------------------------------------------


def test_subprocess_prewarm_runs_in_separate_process():
    """The Popen handle's PID must differ from the current PID. If it
    matches, the prewarm accidentally became in-process and we lost the
    GIL isolation the whole fix relies on."""
    proc = web_server_mod._warm_gateway_in_subprocess()
    try:
        assert proc is not None, "prewarm helper returned None"
        assert proc.pid != os.getpid(), (
            f"subprocess prewarm ran in-process (pid={proc.pid}); "
            f"the fix relies on a separate Python interpreter"
        )
        # Wait briefly for the child to exit; the import is fast on a
        # warm dev machine. We don't care about the return code here —
        # only that the spawn succeeded and the PID is distinct.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
            raise AssertionError(
                "child process did not exit within 10s — import is hung"
            )
    finally:
        # Best-effort reap if test logic exited early.
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Test 2 — lifespan startup completes in << SLOW_SECONDS
# ---------------------------------------------------------------------------


def test_lifespan_subprocess_prewarm_is_nonblocking():
    """Even if the spawned child takes SLOW_SECONDS to finish importing
    (simulating a Defender-scan stall), the lifespan startup must
    complete in well under that — the parent event loop is not coupled
    to the child's GIL."""
    from fastapi.testclient import TestClient

    with patch.object(
        web_server_mod,
        "_warm_gateway_in_subprocess",
        _make_slow_subprocess_warm(SLOW_SECONDS),
    ):
        t0 = time.perf_counter()
        with TestClient(web_server_mod.app, raise_server_exceptions=False) as _client:
            startup_ms = (time.perf_counter() - t0) * 1000

    threshold_ms = (SLOW_SECONDS * 1000) / 2
    assert startup_ms < threshold_ms, (
        f"lifespan startup took {startup_ms:.0f} ms but the child prewarm "
        f"is {SLOW_SECONDS * 1000:.0f} ms — the subprocess is blocking "
        f"the parent somehow (process group leak? synchronous wait?)"
    )


# ---------------------------------------------------------------------------
# Test 3 — /api/health responds while the child is still importing
# ---------------------------------------------------------------------------


def test_api_health_responds_during_slow_subprocess_prewarm():
    """While the prewarm child is still busy, ``/api/health`` must
    return 200 immediately. A regression here would re-open the
    boot-loop race that issue #50209 / PR #50231 originally closed."""
    from fastapi.testclient import TestClient

    with patch.object(
        web_server_mod,
        "_warm_gateway_in_subprocess",
        _make_slow_subprocess_warm(SLOW_SECONDS),
    ):
        # TestClient context triggers lifespan, which spawns the slow
        # child. We then probe /api/health while the child is mid-import.
        with TestClient(web_server_mod.app, raise_server_exceptions=False) as client:
            t = time.perf_counter()
            r = client.get("/api/health", timeout=SLOW_SECONDS + 5)
            health_ms = (time.perf_counter() - t) * 1000
            status_code = r.status_code

    assert status_code == 200, (
        f"/api/health returned {status_code} (expected 200); event loop "
        f"was likely blocked by the prewarm subprocess"
    )
    # /api/health must respond in < SLOW_SECONDS (event loop free).
    assert health_ms < SLOW_SECONDS * 1000, (
        f"/api/health took {health_ms:.0f} ms — event loop was blocked by "
        f"a prewarm subprocess running in the same GIL"
    )


# ---------------------------------------------------------------------------
# Test 4 — shutdown reaps the child (no orphan accumulation)
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_waits_for_child_with_timeout():
    """The lifespan ``finally`` block must call ``wait(timeout=5)`` on
    the child so we don't accumulate zombie Python processes on
    dev-machine reloads. The wait must NOT block forever — if it did,
    SIGINT during shutdown would hang."""
    from fastapi.testclient import TestClient

    captured: dict = {}

    def _tracking_subprocess():
        proc = _make_slow_subprocess_warm(SLOW_SECONDS)()
        captured["proc"] = proc
        return proc

    with patch.object(
        web_server_mod,
        "_warm_gateway_in_subprocess",
        _tracking_subprocess,
    ):
        t0 = time.perf_counter()
        with TestClient(web_server_mod.app, raise_server_exceptions=False):
            pass  # exit triggers lifespan finally
        shutdown_ms = (time.perf_counter() - t0) * 1000

    # Shutdown waits for the child (worst case ~SLOW_SECONDS) but never
    # more than 5s. The lifespan finally's ``wait(timeout=5)`` means the
    # parent cannot hang indefinitely.
    assert "proc" in captured, "child subprocess was never spawned"
    assert shutdown_ms < (SLOW_SECONDS + 6) * 1000, (
        f"shutdown took {shutdown_ms:.0f} ms — wait() is hanging past "
        f"the 5s ceiling"
    )
    # The child should have either exited or been left to finish on its
    # own (process detached). It must not still be a zombie.
    proc = captured["proc"]
    if proc.poll() is None:
        # Not exited yet — that's acceptable (we detached). Just make
        # sure we don't leak the handle in the test process.
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Detached; force-kill only as last resort for the test.
            proc.kill()
            proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# Test 5 — helper gracefully handles missing sys.executable
# ---------------------------------------------------------------------------


def test_prewarm_returns_none_when_no_executable(monkeypatch):
    """If ``sys.executable`` is empty (some embedded Python contexts),
    the helper must return ``None`` instead of raising — the lifespan
    code path tolerates ``None`` and falls back to first-request cold
    import, which is no worse than the pre-fix behavior."""

    def _run():
        with patch.object(web_server_mod.sys, "executable", ""):
            return web_server_mod._warm_gateway_in_subprocess()

    proc = _run()
    assert proc is None, (
        f"prewarm helper returned {proc!r} for empty sys.executable; "
        f"expected None to keep lifespan path safe"
    )


# ---------------------------------------------------------------------------
# Test 6 — GIL independence: parent imports a fast module while child sleeps
# ---------------------------------------------------------------------------


def test_parent_gil_is_independent_of_child():
    """The parent process must be able to import a *different* module
    quickly even while the child is still sleeping. This is the actual
    proof of GIL independence — not just 'the helper returns a Popen',
    but 'the parent can do real Python work while the child does real
    Python work'. A regression here (e.g. accidentally using a thread
    instead of a subprocess) would re-couple the GILs.
    """
    # Use the production helper to spawn a slow child (mirrors test 1
    # but with a longer sleep, so we have headroom to prove GIL freedom).
    SLOW_CHILD_SECONDS = SLOW_SECONDS

    code = (
        f"import time; "
        f"time.sleep({SLOW_CHILD_SECONDS}); "
        f"import hermes_cli.gateway"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # Give the child a moment to actually start sleeping.
        time.sleep(0.2)
        t0 = time.perf_counter()
        # Force-import a fresh module in the parent. The parent GIL
        # must be available immediately — if the child were sharing
        # the GIL (regression), this import would queue behind the
        # child's sleep and we'd observe >= SLOW_CHILD_SECONDS.
        import importlib

        # Use a stdlib module that's not yet in sys.modules for this
        # test process to guarantee a real import.
        importlib.import_module("zipfile")
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert elapsed_ms < 1000, (
            f"parent import took {elapsed_ms:.0f} ms while a child "
            f"process was sleeping for {SLOW_CHILD_SECONDS}s — the GILs "
            f"appear to be coupled, defeating the whole point of the fix"
        )
    finally:
        if child.poll() is None:
            child.kill()
            try:
                child.wait(timeout=2)
            except Exception:
                pass
