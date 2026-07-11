#!/usr/bin/env python3
"""coda_self_grant.py — grant this CoDA's team on THIS CoDA host, from CoDA.

Run this INSIDE a CoDA app terminal (or `databricks apps` exec). It:
  1. Reads the app-SP OAuth creds from the ``omnigents-host`` profile in
     ``~/.databrickscfg`` (written on boot by omnigents_host.py).
  2. Mints an SP OAuth (oauth-m2m) bearer via the Databricks SDK — the token
     type the Databricks Apps proxy in front of the Omnigent server accepts.
     (NOTE: ``databricks auth token`` CANNOT do this — it is U2M only.)
  3. Derives this host's deterministic host_id from the SP client_id
     (``host_{sha256("coda-omnigents-host:"+client_id)[:32]}``), matching
     omnigents_host._stable_host_identity().
  4. PUTs ``/v1/hosts/{host_id}/permissions/{user}`` for each grant.

The server scopes host-permission writes to the HOST OWNER (this SP), so this
CoDA can grant ONLY on itself — it cannot touch another team's host. That IS
the segregation: run this on each CoDA with that team's grants.

Usage (from a CoDA terminal):
    # explicit grants on the CLI:
    python3 coda_self_grant.py \
        --server https://omnigent-....azure.databricksapps.com \
        --grant asanga.wickramasinghe@coles.com.au:manage \
        --grant hariharasudhan.j@coles.com.au:use \
        --grant shree.acharya@coles.com.au:use

    # or drive from the roster by team (matches this host to a team by name):
    python3 coda_self_grant.py --server <url> --roster team_roster.json --team T1

    # list current grants only:
    python3 coda_self_grant.py --server <url> --list
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def sp_creds(profile: str = "omnigents-host") -> dict[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(Path.home() / ".databrickscfg")
    if profile not in cfg:
        sys.exit(f"profile [{profile}] not found in ~/.databrickscfg — is this a CoDA host?")
    p = cfg[profile]
    return {"host": p["host"], "client_id": p["client_id"], "client_secret": p["client_secret"]}


def mint_token(creds: dict[str, str]) -> str:
    try:
        from databricks.sdk.core import Config
    except ModuleNotFoundError:
        sys.exit("databricks-sdk not importable — run with the app venv "
                 "(e.g. /app/python/source_code/.venv/bin/python).")
    cfg = Config(host=creds["host"], client_id=creds["client_id"],
                 client_secret=creds["client_secret"], auth_type="oauth-m2m")
    tok = cfg.authenticate().get("Authorization", "").removeprefix("Bearer ").strip()
    if not tok:
        sys.exit("could not mint SP OAuth token")
    return tok


def host_id_for(client_id: str) -> str:
    return "host_" + hashlib.sha256(f"coda-omnigents-host:{client_id}".encode()).hexdigest()[:32]


def call(server: str, token: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{server.rstrip('/')}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", required=True, help="Omnigent server URL")
    ap.add_argument("--profile", default="omnigents-host")
    ap.add_argument("--grant", action="append", default=[], metavar="EMAIL:LEVEL",
                    help="repeatable; LEVEL = view|use|manage")
    ap.add_argument("--roster", help="team_roster.json to read grants from")
    ap.add_argument("--team", help="team id in the roster (e.g. T1); host is matched by SP")
    ap.add_argument("--list", action="store_true", help="only list current grants")
    ap.add_argument("--no-facilitators", action="store_true",
                    help="skip the roster's facilitators (who are otherwise granted on every host)")
    args = ap.parse_args()

    creds = sp_creds(args.profile)
    token = mint_token(creds)
    hid = host_id_for(creds["client_id"])

    me_s, me_b = call(args.server, token, "GET", "/v1/me")
    if me_s != 200 or not me_b.lstrip().startswith("{"):
        sys.exit(f"server did not accept the SP token ({me_s}). Is --server correct and the host registered?")
    print(f"host_id: {hid}")
    print(f"identity: {me_b.strip()}")

    if args.list:
        s, b = call(args.server, token, "GET", f"/v1/hosts/{hid}/permissions")
        print(f"[{s}] {b}")
        return 0 if s == 200 else 1

    grants: list[tuple[str, str]] = []
    for g in args.grant:
        u, _, lv = g.partition(":")
        grants.append((u.strip(), (lv.strip() or "use")))
    if args.roster:
        roster = json.load(open(args.roster))
        # Facilitators are granted on EVERY host (kept even with --team) so an
        # operator keeps UI control of each team's shares.
        if not args.no_facilitators:
            for f in roster.get("facilitators", []):
                grants.append((f["user_id"], f["level"]))
        team_rows = [t for t in roster["teams"] if not args.team or t["team"] == args.team]
        for t in team_rows:
            for gg in t["grants"]:
                grants.append((gg["user_id"], gg["level"]))

    if not grants:
        sys.exit("no grants given — use --grant EMAIL:LEVEL or --roster/--team")

    rc = 0
    for u, lv in grants:
        s, b = call(args.server, token, "PUT", f"/v1/hosts/{hid}/permissions/{u}", {"level": lv})
        ok = s == 200
        rc |= 0 if ok else 1
        print(f"  {'OK ' if ok else 'ERR'} [{lv:6}] {u}  ->  {s} {b[:160]}")

    s, b = call(args.server, token, "GET", f"/v1/hosts/{hid}/permissions")
    print(f"\nreadback [{s}]: {b}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
