"""Tests for tools/image_source.py — the unified vision image-source resolver.

Covers the delivery contract (data:/http/file/local/container source handling,
size cap, magic-byte sniff) AND the terminal-backend confinement security model
(GHSA-gpxw-6wxv-w3qq): under a non-local backend, host reads are confined to the
media caches and every other path is read inside the sandbox via exec-read.
"""

import base64
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64


def _reload(monkeypatch, hermes_home: Path):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import tools.image_source as isrc
    importlib.reload(isrc)
    return isrc


class TestDataUrl:
    @pytest.mark.asyncio
    async def test_valid_data_url_resolves_to_bytes(self, tmp_path, monkeypatch):
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        b64 = base64.b64encode(PNG).decode()
        res = await isrc.resolve_image_source(
            f"data:image/png;base64,{b64}", isrc.ResolveContext())
        assert res.data == PNG
        assert res.mime == "image/png"
        assert res.origin == "data"

    @pytest.mark.asyncio
    async def test_non_image_data_url_rejected(self, tmp_path, monkeypatch):
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        b64 = base64.b64encode(b"not an image").decode()
        with pytest.raises(isrc.NotAnImage):
            await isrc.resolve_image_source(
                f"data:text/plain;base64,{b64}", isrc.ResolveContext())


class TestLocalBackend:
    @pytest.mark.asyncio
    async def test_local_backend_reads_any_host_path(self, tmp_path, monkeypatch):
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        img = tmp_path / "outside" / "pic.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(PNG)
        res = await isrc.resolve_image_source(str(img), isrc.ResolveContext())
        assert res.data == PNG
        assert res.origin == "file"


    @pytest.mark.asyncio
    async def test_bare_relative_path_resolves(self, tmp_path, monkeypatch):
        """A cwd-relative bare filename ('pic.png') is a valid local source —
        main accepted it; the resolver must not regress it (PR review)."""
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        img = tmp_path / "pic.png"
        img.write_bytes(PNG)
        monkeypatch.chdir(tmp_path)
        res = await isrc.resolve_image_source("pic.png", isrc.ResolveContext())
        assert res.data == PNG
        assert res.origin == "file"


    @pytest.mark.asyncio
    async def test_svg_passes_through_for_rasterization(self, tmp_path, monkeypatch):
        """SVG has no raster magic bytes but is passed through with mime
        image/svg+xml so the vision call sites can rasterize it to PNG."""
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        svg = tmp_path / "art.svg"
        svg_bytes = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        svg.write_bytes(svg_bytes)
        res = await isrc.resolve_image_source(str(svg), isrc.ResolveContext())
        assert res.mime == "image/svg+xml"
        assert res.data == svg_bytes


class TestNonLocalBackendConfinement:
    """The security model: under a sandbox backend, host reads are confined to
    the media caches; every other path is read inside the sandbox."""

    @pytest.mark.asyncio
    async def test_media_cache_path_host_read(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        cached = home / "cache" / "images" / "inbound.png"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(PNG)
        # No sandbox env needed — a cache path is host-read directly.
        res = await isrc.resolve_image_source(str(cached), isrc.ResolveContext())
        assert res.data == PNG
        assert res.origin == "file"

    @pytest.mark.asyncio
    async def test_desktop_upload_images_dir_host_read(self, tmp_path, monkeypatch):
        """Desktop/clipboard uploads under ``HERMES_HOME/images`` are host-read.

        Regression for #69575: uploads land in the flat top-level ``images/``
        dir (not ``cache/images``). Under a sandbox backend the vision resolver
        must permit reading them host-side — otherwise it falls through to the
        task-id-less sandbox reader and fails with "not reachable inside the
        sandbox".
        """
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        upload = home / "images" / "upload_20260722_181019_1.png"
        upload.parent.mkdir(parents=True)
        upload.write_bytes(PNG)
        # No sandbox env: an uploads path must be host-read directly, not routed
        # to the in-sandbox exec-read.
        res = await isrc.resolve_image_source(str(upload), isrc.ResolveContext())
        assert res.data == PNG
        assert res.origin == "file"

    @pytest.mark.asyncio
    async def test_host_secret_outside_cache_routes_to_sandbox_not_host(self, tmp_path, monkeypatch):
        """A non-cache host path (e.g. /etc/passwd) must NOT be host-read — it
        routes to the in-sandbox exec-read, which reads the CONTAINER's file."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        # A real host file outside the caches, holding a "secret".
        secret = tmp_path / "id_rsa"
        secret.write_bytes(b"HOST-PRIVATE-KEY-DO-NOT-LEAK")

        # Fake sandbox env: its exec-read returns a *different* (container) image,
        # proving we read the container filesystem, not the host secret.
        container_png_b64 = base64.b64encode(PNG).decode()
        calls = {}

        def fake_execute(cmd, **kw):
            calls["cmd"] = cmd
            return {"returncode": 0, "output": container_png_b64}

        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            res = await isrc.resolve_image_source(str(secret), isrc.ResolveContext(task_id="t1"))

        # Read came from the sandbox exec-read, returning the container image —
        # the host secret bytes never appear.
        assert res.origin == "container"
        assert res.data == PNG
        assert b"HOST-PRIVATE-KEY" not in res.data
        assert "head -c" in calls["cmd"] and "< " in calls["cmd"]  # bounded, redirect-safe form

    @pytest.mark.asyncio
    async def test_non_cache_path_fails_closed_without_sandbox(self, tmp_path, monkeypatch):
        """No active sandbox env -> refuse rather than fall back to a host read."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        secret = tmp_path / "id_rsa"
        secret.write_bytes(b"HOST-PRIVATE-KEY")

        with patch("tools.image_source._get_active_env", return_value=None):
            with pytest.raises(isrc.SourceNotFound):
                await isrc.resolve_image_source(str(secret), isrc.ResolveContext(task_id="t1"))

    @pytest.mark.asyncio
    async def test_symlink_in_cache_pointing_outside_is_not_host_read(self, tmp_path, monkeypatch):
        """A symlink planted inside a cache dir that points at a host secret must
        not be host-read (resolve() escapes the cache) — it routes to sandbox."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        secret = tmp_path / "outside" / "id_rsa"
        secret.parent.mkdir(parents=True)
        secret.write_bytes(b"HOST-PRIVATE-KEY")
        cache_dir = home / "cache" / "images"
        cache_dir.mkdir(parents=True)
        link = cache_dir / "sneaky.png"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported")

        # Fails closed (no sandbox) rather than host-reading the symlink target.
        with patch("tools.image_source._get_active_env", return_value=None):
            with pytest.raises(isrc.SourceNotFound):
                await isrc.resolve_image_source(str(link), isrc.ResolveContext(task_id="t1"))


class TestExecReadSafety:
    @pytest.mark.asyncio
    async def test_exec_read_is_bounded_and_redirect_safe(self, tmp_path, monkeypatch):
        """Leading-dash paths go through an input redirect (no argv exposure)
        and the read is size-bounded via head -c."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        captured = {}

        def fake_execute(cmd, **kw):
            captured["cmd"] = cmd
            return {"returncode": 0, "output": base64.b64encode(PNG).decode()}

        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            await isrc.resolve_image_source(
                "/workspace/-i-etc-shadow.png", isrc.ResolveContext(task_id="t1"))
        assert f"head -c {isrc._MAX_INGEST_BYTES + 1} < " in captured["cmd"]
        assert "'-i-etc-shadow.png'" in captured["cmd"] or "-i-etc-shadow.png" in captured["cmd"]


    @pytest.mark.asyncio
    async def test_exec_read_nonzero_returncode_raises(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        def fake_execute(cmd, **kw):
            return {"returncode": 1, "output": ""}

        # Existing test: env already present, exec fails.
        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            with pytest.raises(isrc.SourceNotFound):
                await isrc.resolve_image_source(
                    "/workspace/nope.png", isrc.ResolveContext(task_id="t1"))

    @pytest.mark.asyncio
    async def test_lazy_env_acquisition_no_env_then_create(
        self, tmp_path, monkeypatch
    ):
        """#76566: first call with NO active env (the reported fail path)
        must lazy-create a sandbox env and succeed — the matcher Teknium
        pointed at (tools/terminal_tool.get_active_env is lookup-only).
        No patch on _get_active_env; we drive the real lazy-create path
        by seeding an empty _active_environments and a stub
        _create_environment that records the call."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        import tools.terminal_tool as tt
        import threading, time

        b64 = base64.b64encode(PNG).decode()

        class _StubEnv:
            def __init__(self):
                self._calls = 0
            def execute(self, cmd, **kw):
                self._calls += 1
                return {"returncode": 0, "output": b64}

        stub = _StubEnv()
        created = []
        # Start with EMPTY _active_environments — exactly the reported path.
        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_creation_locks", {})
        monkeypatch.setattr(tt, "_creation_locks_lock", threading.Lock())
        monkeypatch.setattr(tt, "_env_lock", threading.Lock())
        monkeypatch.setattr(tt, "_last_activity", {})
        monkeypatch.setattr(tt, "_task_env_overrides", {})
        monkeypatch.setattr(
            tt, "_resolve_container_task_id", lambda tid: tid or "default"
        )
        monkeypatch.setattr(
            tt, "_get_env_config",
            lambda: {"env_type": "docker", "docker_image": "py:3.11",
                     "cwd": "/workspace", "timeout": 60, "host_cwd": ""},
        )
        monkeypatch.setattr(
            tt, "_create_environment",
            lambda **kw: (created.append(kw), stub)[1],
        )
        started = []
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: started.append(1))
        monkeypatch.setattr(tt, "get_active_env", lambda tid: tt._active_environments.get(tid))

        res = await isrc.resolve_image_source(
            "/workspace/cold.png", isrc.ResolveContext(task_id="t1"))

        # Lazy acquisition created exactly one env, called it once, succeeded.
        assert len(created) == 1
        assert created[0]["env_type"] == "docker"
        assert created[0]["task_id"] == "t1"
        assert stub._calls == 1
        assert started == [1]
        assert res.origin == "container"
        assert res.data == PNG
        # Env is now registered for the next caller (no more creation needed).
        assert "t1" in tt._active_environments

    @pytest.mark.asyncio
    async def test_lazy_env_acquisition_local_backend_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """#76566: under the LOCAL backend the path stays on the host —
        nothing to acquire, no sandbox read. The resolver raises
        SourceNotFound (no host file, nothing to fall back to), and that
        message must NOT pretend the file is unreachable *inside the
        sandbox* — there is no sandbox under a local backend."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "local")  # not docker

        with pytest.raises(isrc.SourceNotFound) as excinfo:
            await isrc.resolve_image_source(
                "/workspace/x.png", isrc.ResolveContext(task_id="t1"))
        # Local backend: "media file not found", NOT "inside the sandbox".
        # The sandbox-unreachable message must only fire for non-local backends.
        msg = str(excinfo.value)
        assert "media file not found" in msg, msg
        assert "inside the sandbox" not in msg, msg

    @pytest.mark.asyncio
    async def test_lazy_env_acquisition_create_failure_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """#76566: if _create_environment raises (daemon down, image pull
        failure, ...), _get_active_env returns None and the caller fails
        closed with the same fail-closed message — never an opaque
        exception that leaks the create path."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        import tools.terminal_tool as tt
        import threading

        monkeypatch.setattr(tt, "_active_environments", {})
        monkeypatch.setattr(tt, "_creation_locks", {})
        monkeypatch.setattr(tt, "_creation_locks_lock", threading.Lock())
        monkeypatch.setattr(tt, "_env_lock", threading.Lock())
        monkeypatch.setattr(tt, "_last_activity", {})
        monkeypatch.setattr(tt, "_task_env_overrides", {})
        monkeypatch.setattr(
            tt, "_resolve_container_task_id", lambda tid: tid or "default"
        )
        monkeypatch.setattr(
            tt, "_get_env_config",
            lambda: {"env_type": "docker", "docker_image": "py:3.11",
                     "cwd": "/workspace", "timeout": 60, "host_cwd": ""},
        )
        def _boom(**kw):
            raise RuntimeError("daemon down")
        monkeypatch.setattr(tt, "_create_environment", _boom)
        monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
        monkeypatch.setattr(tt, "get_active_env", lambda tid: tt._active_environments.get(tid))

        with pytest.raises(isrc.SourceNotFound) as excinfo:
            await isrc.resolve_image_source(
                "/workspace/x.png", isrc.ResolveContext(task_id="t1"))
        assert "not reachable inside the sandbox" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_exec_read_retries_cold_start_then_succeeds(self, tmp_path, monkeypatch):
        """#76566: even with the env acquired, the very first exec against
        a fresh container can come back empty/non-zero while an immediate
        retry succeeds. Keep the single retry so the agent doesn't see
        'could not read inside the sandbox' on a file that is fully
        readable on the second attempt."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        calls = {"n": 0}
        b64 = base64.b64encode(PNG).decode()

        def fake_execute(cmd, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"returncode": 1, "output": ""}
            return {"returncode": 0, "output": b64}

        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            res = await isrc.resolve_image_source(
                "/workspace/cold.png", isrc.ResolveContext(task_id="t1"))
        assert res.origin == "container"
        assert res.data == PNG
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_exec_read_failure_includes_diagnostic(
        self, tmp_path, monkeypatch
    ):
        """#76566: when the exec read still fails after the retry, fold
        the container's first stderr/stdout line into the raised error so
        the user can tell 'no such file' from 'permission denied'."""
        home = tmp_path / "hermes"
        isrc = _reload(monkeypatch, home)
        monkeypatch.setenv("TERMINAL_ENV", "docker")

        def fake_execute(cmd, **kw):
            return {"returncode": 1, "output": "head: can't open '/x': No such file or directory"}

        with patch("tools.image_source._get_active_env",
                   return_value=SimpleNamespace(execute=fake_execute)):
            with pytest.raises(isrc.SourceNotFound) as excinfo:
                await isrc.resolve_image_source(
                    "/workspace/missing.png", isrc.ResolveContext(task_id="t1"))
        assert "No such file or directory" in str(excinfo.value)


class TestSvgNormalization:
    """SVG resolves end-to-end: the resolver passes it through as
    image/svg+xml and the vision call sites rasterize it to PNG via
    _normalize_to_supported_image (PR #52688, folded in)."""

    @pytest.mark.asyncio
    async def test_svg_rasterized_when_converter_available(self, tmp_path, monkeypatch):
        from tools import vision_tools as vt
        isrc = _reload(monkeypatch, tmp_path / "hermes")
        monkeypatch.setenv("TERMINAL_ENV", "local")
        svg = tmp_path / "art.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')

        def fake_rasterize(svg_path, out_path):
            out_path.write_bytes(PNG)
            return True

        with patch.object(vt, "_rasterize_svg_to_png", side_effect=fake_rasterize):
            res = await isrc.resolve_image_source(str(svg), isrc.ResolveContext())
            assert res.mime == "image/svg+xml"
            path, mime, err = vt._normalize_to_supported_image(svg, "image/svg+xml")
        assert err is None
        assert mime == "image/png"
        assert path.read_bytes() == PNG
        path.unlink()

    def test_svg_actionable_error_when_no_converter(self, tmp_path, monkeypatch):
        from tools import vision_tools as vt
        _reload(monkeypatch, tmp_path / "hermes")
        svg = tmp_path / "art.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        with patch.object(vt, "_rasterize_svg_to_png", return_value=False):
            path, mime, err = vt._normalize_to_supported_image(svg, "image/svg+xml")
        assert path is None
        assert "rasterizer" in err
