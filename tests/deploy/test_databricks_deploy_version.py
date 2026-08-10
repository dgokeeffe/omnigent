"""The Databricks Apps deploy pins the version the app actually reports.

The runtime reads ``omnigent.version.VERSION`` — ``/api/version``, the CLI
``--version`` and the host/runner hello frames all go through that constant, not
package metadata. A deploy that stamps only the pyprojects therefore uploads
wheels named ``<base>.postN`` while the running app still answers with the
unstamped base version (``0.9.0.dev0``), which is exactly the drift that made a
deployment unusable as a pinned release.

These tests pin the stamp, the restore, and the pre-upload guard.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"
_VERSION_PY = _ROOT / "omnigent" / "version.py"


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_databricks_deploy_version", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_version_py(path: Path, version: str) -> None:
    path.write_text(f'"""Docstring."""\n\nVERSION = "{version}"\n')


def _make_wheel(path: Path, version: str) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("omnigent/version.py", f'VERSION = "{version}"\n')
    return path


def test_repo_version_py_has_a_stampable_constant(deploy_mod: ModuleType) -> None:
    """The stamp regex must match the real file, not just a fixture."""
    assert len(deploy_mod._VERSION_ASSIGN_RE.findall(_VERSION_PY.read_text())) == 1


def test_version_py_path_points_at_the_runtime_constant(deploy_mod: ModuleType) -> None:
    assert deploy_mod._version_py_path() == _VERSION_PY


def test_set_version_in_version_py_rewrites_and_returns_original(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "version.py"
    _write_version_py(target, "0.9.0.dev0")

    original = deploy_mod.set_version_in_version_py(target, "0.9.0.post42")

    assert 'VERSION = "0.9.0.post42"' in target.read_text()
    assert 'VERSION = "0.9.0.dev0"' in original
    # The returned text is what `_restore_versions` writes back.
    deploy_mod._restore_versions({target: original})
    assert 'VERSION = "0.9.0.dev0"' in target.read_text()


def test_set_version_in_version_py_rejects_a_missing_constant(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "version.py"
    target.write_text("# no constant here\n")
    with pytest.raises(RuntimeError, match="could not rewrite VERSION"):
        deploy_mod.set_version_in_version_py(target, "0.9.0.post42")


def test_stamp_versions_covers_the_runtime_constant(
    deploy_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_stamp_versions` must stamp version.py alongside the three pyprojects."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "omnigent"\nversion = "0.9.0.dev0"\n')
    version_py = tmp_path / "omnigent" / "version.py"
    version_py.parent.mkdir()
    _write_version_py(version_py, "0.9.0.dev0")
    monkeypatch.setattr(deploy_mod, "_pyproject_paths", lambda: [pyproject])
    monkeypatch.setattr(deploy_mod, "_version_py_path", lambda: version_py)

    backups = deploy_mod._stamp_versions("0.9.0.post42")

    assert set(backups) == {pyproject, version_py}
    assert 'version = "0.9.0.post42"' in pyproject.read_text()
    assert 'VERSION = "0.9.0.post42"' in version_py.read_text()

    deploy_mod._restore_versions(backups)
    assert 'version = "0.9.0.dev0"' in pyproject.read_text()
    assert 'VERSION = "0.9.0.dev0"' in version_py.read_text()


def test_read_wheel_runtime_version(deploy_mod: ModuleType, tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path / "omnigent-0.9.0.post42-py3-none-any.whl", "0.9.0.post42")
    assert deploy_mod.read_wheel_runtime_version(wheel) == "0.9.0.post42"


def test_assert_wheel_runtime_version_accepts_a_matching_wheel(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    wheel = _make_wheel(tmp_path / "omnigent-0.9.0.post42-py3-none-any.whl", "0.9.0.post42")
    deploy_mod.assert_wheel_runtime_version(wheel, "0.9.0.post42")


def test_assert_wheel_runtime_version_rejects_an_unstamped_wheel(
    deploy_mod: ModuleType, tmp_path: Path
) -> None:
    """A `.postN`-named wheel carrying `dev0` is the regression to block."""
    wheel = _make_wheel(tmp_path / "omnigent-0.9.0.post42-py3-none-any.whl", "0.9.0.dev0")
    with pytest.raises(SystemExit, match="reports runtime version"):
        deploy_mod.assert_wheel_runtime_version(wheel, "0.9.0.post42")


def test_stamp_versions_restores_everything_when_a_later_file_is_malformed(
    deploy_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial stamp must not survive: the caller never gets the backups."""
    good = tmp_path / "pyproject.toml"
    good.write_text('[project]\nversion = "0.9.0.dev0"\n')
    bad = tmp_path / "sdk" / "pyproject.toml"
    bad.parent.mkdir()
    bad.write_text('[project]\nname = "missing-version"\n')
    version_py = tmp_path / "omnigent" / "version.py"
    version_py.parent.mkdir()
    _write_version_py(version_py, "0.9.0.dev0")
    monkeypatch.setattr(deploy_mod, "_pyproject_paths", lambda: [good, bad])
    monkeypatch.setattr(deploy_mod, "_version_py_path", lambda: version_py)

    with pytest.raises(RuntimeError, match="could not rewrite version"):
        deploy_mod._stamp_versions("0.9.0.post42")

    assert good.read_text() == '[project]\nversion = "0.9.0.dev0"\n'
    assert 'VERSION = "0.9.0.dev0"' in version_py.read_text()


def test_stamp_versions_restores_when_version_py_is_malformed(
    deploy_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "pyproject.toml"
    good.write_text('[project]\nversion = "0.9.0.dev0"\n')
    version_py = tmp_path / "version.py"
    version_py.write_text("# no constant\n")
    monkeypatch.setattr(deploy_mod, "_pyproject_paths", lambda: [good])
    monkeypatch.setattr(deploy_mod, "_version_py_path", lambda: version_py)

    with pytest.raises(RuntimeError, match="could not rewrite VERSION"):
        deploy_mod._stamp_versions("0.9.0.post42")

    assert good.read_text() == '[project]\nversion = "0.9.0.dev0"\n'


class _FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode()
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_smoke_check_fails_when_the_live_version_differs(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy app reporting the base version must not pass the deploy."""
    import urllib.request

    wc = SimpleNamespace(
        config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer tok"})
    )
    seen: list[str] = []

    def _fake_urlopen(req: object, timeout: int = 0) -> _FakeResponse:
        url = req.full_url  # type: ignore[attr-defined]
        seen.append(url)
        if url.endswith("/health"):
            return _FakeResponse('{"status":"ok"}')
        return _FakeResponse('{"version": "0.9.0.dev0"}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    with pytest.raises(SystemExit, match=re.escape("0.9.0.dev0")):
        deploy_mod._smoke_check(wc, "https://app.databricksapps.com", "0.9.0.post42")
    assert seen[-1].endswith("/api/version")


def test_smoke_check_passes_when_the_live_version_matches(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.request

    wc = SimpleNamespace(
        config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer tok"})
    )

    def _fake_urlopen(req: object, timeout: int = 0) -> _FakeResponse:
        url = req.full_url  # type: ignore[attr-defined]
        body = '{"status":"ok"}' if url.endswith("/health") else '{"version": "0.9.0.post42"}'
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    deploy_mod._smoke_check(wc, "https://app.databricksapps.com", "0.9.0.post42")


@pytest.mark.parametrize(
    "override",
    [
        "https://collector.example.com/health",
        "http://app.databricksapps.com",
        "https://user:pw@app.databricksapps.com",
    ],
)
def test_smoke_url_override_cannot_redirect_the_workspace_token(
    deploy_mod: ModuleType, override: str
) -> None:
    """--app-url must never send the deployer's bearer to another origin."""
    with pytest.raises(SystemExit):
        deploy_mod.assert_smoke_url_trusted(override, "https://app.databricksapps.com")


def test_smoke_url_override_matching_the_app_resource_is_allowed(
    deploy_mod: ModuleType,
) -> None:
    deploy_mod.assert_smoke_url_trusted(
        "https://app.databricksapps.com/", "https://app.databricksapps.com"
    )


def test_smoke_url_without_a_resolved_url_requires_a_databricks_apps_host(
    deploy_mod: ModuleType,
) -> None:
    deploy_mod.assert_smoke_url_trusted("https://app.databricksapps.com", None)
    with pytest.raises(SystemExit, match="not a Databricks Apps host"):
        deploy_mod.assert_smoke_url_trusted("https://collector.example.com", None)
