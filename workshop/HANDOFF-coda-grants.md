# Handoff — team host grants, run FROM CoDA

**TL;DR:** Yes — the per-team Omnigent `use`/`manage` grants CAN be run from
inside each CoDA, using that CoDA's own app-SP OAuth token. The server scopes
host-permission writes to the **host owner** (the SP), so each CoDA can grant
**only on itself** — which is exactly the segregation we want. Proven live:
`coda-01` (Team T1) is already granted this way.

What still needs your **admin** identity (NOT doable from CoDA): creating
`coda-03..coda-08` and their two IAM grants (workspace RBAC + the OIDC-proxied
create path). Once a CoDA exists, is deployed, and has PAT-bootstrapped so it
registers as a host, the grants run from that CoDA.

---

## Why the earlier `curl` failed but this works

- The Omnigent server sits behind the Databricks Apps **OIDC proxy**. It rejects
  PATs and plain tokens (302 → sign-in HTML).
- It DOES accept an **SP OAuth (oauth-m2m)** bearer. `databricks auth token`
  can't mint one (U2M only) — you must use the **SDK** `Config(...).authenticate()`
  with the SP client_id/secret. The `omnigents-host` profile in
  `~/.databrickscfg` (written on boot) holds those creds.
- Host ownership: the tunnel authenticates as the SP, so the SP is the host
  **owner** and `check_host_access` lets the owner manage its own shares even
  though `/v1/me` reports `is_admin:false`.

## host_id is deterministic

```
host_id = "host_" + sha256("coda-omnigents-host:" + <app_sp_client_id>).hexdigest()[:32]
```
(matches `omnigents_host._stable_host_identity()`).

| Host | SP client_id | host_id |
|---|---|---|
| coda-01 | 8b071759-0920-4d18-bcf6-27a57c4904f1 | host_0956a0230a3678f6d31435ed0007a921 |
| coda-02 | 1ed23afe-dda5-4681-b348-dbfd68d8140e | host_0ae991748222e5f011c9fbecbb127aa4 |
| coda-03..08 | (from `databricks apps get coda-0N`) | derive with the formula |

---

## Commands — run inside each CoDA terminal

The app venv has the SDK. From the CoDA terminal:

```bash
cd ~/... /workshop            # wherever coda_self_grant.py + team_roster.json live
VENV=/app/python/source_code/.venv/bin/python
SERVER=https://omnigent-7405614666872455.15.azure.databricksapps.com

# Grant this CoDA's team, straight from the roster (host matched by its own SP):
$VENV coda_self_grant.py --server $SERVER --roster team_roster.json --team T1

# ...or grant explicitly without the roster:
$VENV coda_self_grant.py --server $SERVER \
  --grant asanga.wickramasinghe@coles.com.au:manage \
  --grant hariharasudhan.j@coles.com.au:use \
  --grant shree.acharya@coles.com.au:use

# Just show current grants on this host:
$VENV coda_self_grant.py --server $SERVER --list
```

Run T1 on coda-01, T2 on coda-02, … T8 on coda-08. Each CoDA can only grant
itself, so there is no risk of one team's CoDA touching another team's shares.

The grantee ids are the `@coles.com.au` emails the Apps proxy sends as
`X-Forwarded-Email`. The PUT auto-creates the user row (`ensure_user`), so you
can grant guests before they ever log in.

---

## Full sequence for the whole fleet

1. **[admin]** Create + wire + deploy the missing hosts:
   `./provision_coda_hosts.sh coda-02 coda-03 coda-04 coda-05 coda-06 coda-07 coda-08`
   (coda-02 exists but isn't deployed; 03–08 are new.)
2. **[admin]** Paste your PAT into each new CoDA terminal once (bootstraps
   `run_setup` → installs claude/tmux, adopts creds → host registers).
3. **[from each CoDA]** Run `coda_self_grant.py --team T<N>` for that host.
4. **[verify]** On any CoDA: `coda_self_grant.py --list`, or as admin in the UI
   Settings → Hosts, or `GET /v1/hosts?all=true`.

## Roster (team → host)

See `team_roster.json`. T1→coda-01 … T8→coda-08; one `manage` + rest `use`
per team; 22 confirmed guests, 22 grants.

## Alternative: the built-in `/api/omnigent-host/share` endpoint

The CoDA app already ships `omnigents_host.share_and_launch()` (owner-gated
`/api/omnigent-host/share`), which does the same SP-token → PUT grant for a
single user and optionally launches a runner. `coda_self_grant.py` is the
batch/roster-driven version of that, runnable from a shell.
