"""Regression tests for the Databricks Apps deploy script.

Covers the pieces that silently mis-deploy rather than crash:

- ``--no-otel`` only rewrites the *default* target, so pairing it with a
  custom ``--target`` must warn instead of shipping OTel-on;
- ``prod-no-otel`` must actually override every OTel variable it claims to;
- an ambient ``DATABRICKS_HOST`` must never outrank ``--profile`` (the CLI
  echoes it back from ``databricks auth env``, pinning the wrong workspace);
- a tz-naive token expiry must not blow up the runway check.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_PY = _ROOT / "deploy" / "databricks" / "deploy.py"
_BUNDLE_YML = _ROOT / "deploy" / "databricks" / "databricks.yml"

_REQUIRED_ARGS = [
    "--app-name",
    "omnigent",
    "--lakebase-branch",
    "projects/omnigent/branches/production",
    "--lakebase-database",
    "projects/omnigent/branches/production/databases/databricks-postgres",
    "--volume-name",
    "main.omnigent.artifacts",
]


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    """Load deploy.py by path (deploy/ is not an installed package)."""
    spec = importlib.util.spec_from_file_location("_databricks_deploy", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse(deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, *extra: str) -> Any:
    monkeypatch.setattr(sys, "argv", ["deploy.py", *_REQUIRED_ARGS, *extra])
    return deploy_mod._parse_args()


def test_no_otel_switches_default_target(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _parse(deploy_mod, monkeypatch, "--no-otel")
    assert args.target == "prod-no-otel"
    assert "warning" not in capsys.readouterr().out


def test_no_otel_warns_on_target_without_overrides(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A custom target keeps OTel on; the flag must not fail silently."""
    args = _parse(deploy_mod, monkeypatch, "--no-otel", "--target", "staging")
    assert args.target == "staging"
    out = capsys.readouterr().out
    assert "warning: --no-otel has no effect" in out
    assert "staging" in out


def test_no_otel_quiet_on_explicit_no_otel_target(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _parse(deploy_mod, monkeypatch, "--no-otel", "--target", "prod-no-otel")
    assert args.target == "prod-no-otel"
    assert "warning" not in capsys.readouterr().out


def test_bundle_no_otel_target_overrides_every_otel_var(deploy_mod: ModuleType) -> None:
    """The target --no-otel selects must really turn the tracer off."""
    assert deploy_mod._target_overrides_otel_vars("prod-no-otel") is True
    assert deploy_mod._target_overrides_otel_vars("prod") is False

    bundle = yaml.safe_load(_BUNDLE_YML.read_text())
    overrides = bundle["targets"]["prod-no-otel"]["variables"]
    assert overrides["app_command"]["default"] == ["python", "app.py"]
    assert overrides["otel_export_destinations"]["default"] == []
    env_names = {entry["name"] for entry in overrides["app_env"]["default"]}
    assert "OTEL_TRACES_SAMPLER" not in env_names


def test_bundle_env_only_references_declared_vars(deploy_mod: ModuleType) -> None:
    """No app env entry may interpolate a variable the bundle doesn't declare."""
    bundle = yaml.safe_load(_BUNDLE_YML.read_text())
    declared = set(bundle["variables"])
    env_defaults = [bundle["variables"]["app_env"]["default"]]
    for target in bundle["targets"].values():
        app_env = (target.get("variables") or {}).get("app_env")
        if app_env:
            env_defaults.append(app_env["default"])
    for entries in env_defaults:
        for entry in entries:
            value = str(entry.get("value", ""))
            if value.startswith("${var."):
                assert value.removeprefix("${var.").removesuffix("}") in declared, entry


def test_profile_clears_ambient_host(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--profile owns the workspace, so a stale exported host must go."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://stale.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "stale")
    args = _parse(deploy_mod, monkeypatch, "--profile", "myprof")
    deploy_mod._clear_env_vars(keep=deploy_mod._host_env_keep(args))
    assert "DATABRICKS_HOST" not in deploy_mod.os.environ
    assert "DATABRICKS_TOKEN" not in deploy_mod.os.environ


def test_profileless_deploy_keeps_host_env_auth(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --profile, DATABRICKS_HOST is the auth input, not a leak."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://env-auth.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "stale")
    args = _parse(deploy_mod, monkeypatch)
    deploy_mod._clear_env_vars(keep=deploy_mod._host_env_keep(args))
    assert deploy_mod.os.environ["DATABRICKS_HOST"] == "https://env-auth.cloud.databricks.com"
    assert "DATABRICKS_TOKEN" not in deploy_mod.os.environ


def test_bundle_vars_are_comma_free(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`databricks bundle --var` rejects a comma inside a value assignment."""
    args = _parse(deploy_mod, monkeypatch)
    for value in deploy_mod._bundle_vars(args):
        if value == "--var":
            continue
        assert "," not in value, f"comma in --var {value!r} would fail the CLI parser"


def test_uc_grant_failure_is_non_fatal(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The app boots without traversal grants; the smoke check is the gate."""
    args = _parse(deploy_mod, monkeypatch)

    def fake_run(cmd: list[str], **_: object) -> None:
        raise deploy_mod.subprocess.CalledProcessError(
            1, cmd, stderr="PERMISSION_DENIED: requires MANAGE on catalog"
        )

    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)
    deploy_mod._ensure_app_sp_uc_traversal(args, "app-sp-1234")

    out = capsys.readouterr().out
    assert "warning: USE_CATALOG grant on main failed" in out
    assert "warning: USE_SCHEMA grant on main.omnigent failed" in out


def test_bundle_vars_include_managed_coda_configuration(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Managed CoDA wiring must reach the bundle, not just the CLI."""
    args = _parse(
        deploy_mod,
        monkeypatch,
        "--coda-app-name",
        "coda-main",
        "--coda-app-url",
        "https://coda.example.com",
        "--omnigent-public-server-url",
        "https://omnigent.example.com",
    )

    pairs = deploy_mod._bundle_vars(args)

    assert "coda_app_name=coda-main" in pairs
    assert "coda_app_url=https://coda.example.com" in pairs
    assert "omnigent_public_server_url=https://omnigent.example.com" in pairs


def test_parse_args_rejects_partial_managed_coda_configuration(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit, match="2"):
        _parse(deploy_mod, monkeypatch, "--coda-app-name", "coda-main")


def test_no_otel_target_keeps_managed_coda_wiring(deploy_mod: ModuleType) -> None:
    """--no-otel drops the tracer, never the managed CoDA env."""
    bundle = yaml.safe_load(_BUNDLE_YML.read_text())
    expected = {"CODA_APP_NAME", "CODA_APP_URL", "OMNIGENT_PUBLIC_SERVER_URL"}
    for entries in (
        bundle["variables"]["app_env"]["default"],
        bundle["targets"]["prod-no-otel"]["variables"]["app_env"]["default"],
    ):
        assert expected <= {entry["name"] for entry in entries}


def test_bundle_declares_compute_size_and_the_direct_engine(deploy_mod: ModuleType) -> None:
    """The bundle owns compute_size; the Terraform engine silently dropped it."""
    bundle = yaml.safe_load(_BUNDLE_YML.read_text())
    assert bundle["bundle"]["engine"] == "direct"
    assert bundle["resources"]["apps"]["omnigent"]["compute_size"] == "${var.compute_size}"
    assert "compute_size" in bundle["variables"]


def test_bundle_vars_pass_the_requested_compute_size(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _parse(deploy_mod, monkeypatch, "--compute-size", "SMALL")
    assert "compute_size=SMALL" in deploy_mod._bundle_vars(args)


def test_assert_compute_size_rejects_a_downsized_app(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy asking for LARGE must not quietly finish on MEDIUM."""
    from types import SimpleNamespace

    wc = SimpleNamespace(
        apps=SimpleNamespace(
            get=lambda name: SimpleNamespace(compute_size=SimpleNamespace(value="MEDIUM"))
        )
    )
    with pytest.raises(SystemExit, match="MEDIUM"):
        deploy_mod.assert_compute_size(wc, "omnigent", "LARGE")


def test_assert_compute_size_accepts_a_matching_app(deploy_mod: ModuleType) -> None:
    from types import SimpleNamespace

    wc = SimpleNamespace(
        apps=SimpleNamespace(
            get=lambda name: SimpleNamespace(compute_size=SimpleNamespace(value="LARGE"))
        )
    )
    deploy_mod.assert_compute_size(wc, "omnigent", "LARGE")


def test_assert_direct_engine_accepts_the_committed_bundle(deploy_mod: ModuleType) -> None:
    deploy_mod.assert_direct_engine(_BUNDLE_YML)


@pytest.mark.parametrize("engine", ["terraform", None])
def test_assert_direct_engine_refuses_anything_else(
    deploy_mod: ModuleType, tmp_path: Path, engine: str | None
) -> None:
    """Terraform drops the app's compute_size, so the deploy must not proceed."""
    bundle = yaml.safe_load(_BUNDLE_YML.read_text())
    if engine is None:
        bundle["bundle"].pop("engine", None)
    else:
        bundle["bundle"]["engine"] = engine
    path = tmp_path / "databricks.yml"
    path.write_text(yaml.safe_dump(bundle))

    with pytest.raises(SystemExit, match="requires 'direct'"):
        deploy_mod.assert_direct_engine(path)


def test_ambient_bundle_engine_is_cleared(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell export must not be able to pick the engine for a deploy."""
    monkeypatch.setenv("DATABRICKS_BUNDLE_ENGINE", "terraform")
    args = _parse(deploy_mod, monkeypatch, "--profile", "myprof")
    deploy_mod._clear_env_vars(keep=deploy_mod._host_env_keep(args))
    assert "DATABRICKS_BUNDLE_ENGINE" not in deploy_mod.os.environ
