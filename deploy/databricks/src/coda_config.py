"""Pure managed-CoDA environment normalization for the Databricks App bootstrap."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping


def managed_coda_raw_config(env: Mapping[str, str]) -> dict[str, object] | None:
    """Build server sandbox config from pool or legacy environment variables."""
    pool_b64 = env.get("CODA_POOL_B64", "").strip()
    app_name = env.get("CODA_APP_NAME", "").strip()
    app_url = env.get("CODA_APP_URL", "").strip()
    server_url = env.get("OMNIGENT_PUBLIC_SERVER_URL", "").strip()
    legacy_values = (app_name, app_url)
    if pool_b64 and any(legacy_values):
        raise RuntimeError("CODA_POOL_B64 cannot be combined with legacy CoDA variables")
    if any(legacy_values) and not all(legacy_values):
        raise RuntimeError("partial legacy managed CoDA configuration")
    if (pool_b64 or all(legacy_values)) and not server_url:
        raise RuntimeError("OMNIGENT_PUBLIC_SERVER_URL is required with managed CoDA")
    if server_url and not (pool_b64 or all(legacy_values)):
        raise RuntimeError("managed CoDA App configuration is missing")
    if not server_url:
        return None
    if pool_b64:
        try:
            pool = json.loads(base64.urlsafe_b64decode(pool_b64).decode())
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("CODA_POOL_B64 must contain URL-safe base64 JSON") from exc
        coda: dict[str, object] = {"pool": pool}
    else:
        coda = {"app_name": app_name, "app_url": app_url}
    return {"provider": "coda", "server_url": server_url, "coda": coda}
