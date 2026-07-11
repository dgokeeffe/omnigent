# Workshop host provisioning + team segregation

Goal: give the **8 workshop teams** one shared **CoDA host** each, connected to
the live **Omnigent server** (`omnigent`), with the team boundary enforced by
**per-user `use` grants on the Omnigent side**. Joint access *within* a CoDA is
intentional (shared terminal, your PAT injected) — segregation lives in the
Omnigent host shares, not in per-CoDA identity.

All privileged steps run as the **`daveok` workspace admin** (not from inside a
CoDA container — a host SP cannot create apps, issue IAM grants, or call the
server's admin API).

---

## Files

| File | What it does | Who runs it |
|---|---|---|
| `provision_coda_hosts.sh` | Create `coda-03..coda-08`, IAM-grant each SP, deploy coda-01's source so they self-register as hosts. | admin |
| `grant_omnigent_host.sh` | (dependency) grants a CoDA SP `CAN_USE` on `omnigent` + `READ_VOLUME` on the wheel volume. Pulled verbatim from the live coda-01 source. | admin |
| `team_roster.json` | The 8-team → host → user/level mapping (source of truth). | — |
| `grant_team_hosts.sh` | Apply the per-team `use`/`manage` grants via `PUT /v1/hosts/{id}/permissions/{user}`. | admin/manage |

---

## Live facts (verified 2026-07-10)

- **Omnigent server app:** `omnigent` — `https://omnigent-7405614666872455.15.azure.databricksapps.com` (RUNNING).
- **Existing shared hosts:** `coda-01` (SP `8b071759…`), `coda-02` (SP `1ed23afe…`) — both ACTIVE. `coda-02` has no deployment yet.
- **Wheel volume:** `edp_aisandbox_aisandbox_dev.daveok.artifacts` (`/wheels` holds omnigent 0.5.0).
- **Host self-registration:** coda-01's `app.yaml` sets `OMNIGENTS_SERVER_URL`, `OMNIGENTS_WHEEL_SPEC`, `ENABLE_SP_APIKEYHELPER=true`, `CODA_DISABLE_OWNER_CHECK=true` (shared-terminal mode). On boot `start_host()` dials the server as the app SP and registers.

---

## Run order

```bash
export DATABRICKS_CONFIG_PROFILE=daveok   # admin identity
cd workshop

# 1. Create + wire + deploy coda-03..coda-08 (coda-01/02 already exist).
#    Deploy coda-02 too if it isn't running yet:
./provision_coda_hosts.sh coda-02 coda-03 coda-04 coda-05 coda-06 coda-07 coda-08

# 2. For EACH host: open its terminal and paste your PAT once (bootstraps
#    run_setup — installs claude/tmux, adopts creds). Then the host registers.

# 3. Confirm all 8 hosts are online on the server (admin fleet view):
#    Omnigent UI -> Settings -> Hosts   (or GET /v1/hosts?all=true)

# 4. Apply the per-team Omnigent grants (segregation boundary):
OMNIGENT_TOKEN="<your user bearer for the omnigent server>" ./grant_team_hosts.sh
#    or:  OMNIGENT_PROFILE=<profile-the-server-accepts> ./grant_team_hosts.sh
```

`grant_team_hosts.sh` resolves each host **name → host_id** from
`/v1/hosts?all=true`, so run it only after the hosts have registered (step 3).
It is idempotent (PUT upserts).

---

## Admin "Hosts" management pane

The pane already exists in the SPA (`/settings/hosts`, `HostsPage.tsx`) and is
wired into Settings under the **Admin** nav group. It only renders for users the
server reports as **admin**. In the header/OIDC deploy the omnigent server uses,
admin is set by the **admin roster file** (default `<data_dir>/admins`, override
`OMNIGENT_ADMIN_LIST_PATH`): one identity per line, promoted on next login,
additive-only.

To "bring back" the pane for a facilitator, add their email to that file on the
`omnigent` app's data volume and have them re-login:

```
# <data_dir>/admins
david.okeeffe1@coles.com.au
```

Once admin, the facilitator can manage every host's shares from the UI
(the same `PUT/DELETE /v1/hosts/{id}/permissions/{user}` calls this script makes),
so `grant_team_hosts.sh` and the UI are interchangeable.

---

## Team roster (segregation map)

Round-robin, balanced (six teams of 3, two of 2). One `manage` per team so a
human can administer that team's share without a server admin; the rest `use`.
Edit `team_roster.json` to change assignments, then re-run the grant tool.

The roster's `facilitators` block (currently `david.okeeffe1@coles.com.au` →
`manage`) is granted on **every** host by `coda_self_grant.py`, so the
facilitator keeps UI control of each team's shares. Pass `--no-facilitators`
to skip. `grant_team_hosts.sh` (admin/API path) applies teams only.

| Team | Host | manage | use |
|---|---|---|---|
| T1 | coda-01 | asanga.wickramasinghe | hariharasudhan.j, shree.acharya |
| T2 | coda-02 | chris.hatton | john.pereyra (Bob), srinivasulu.reddiboina |
| T3 | coda-03 | david.hemming | mahesh.chahar, swethakachana.kachana |
| T4 | coda-04 | david.johnston3 | ning.kang, todd.schwarzbrott |
| T5 | coda-05 | david.sartori | peter.eldred, tom.commons |
| T6 | coda-06 | dom.maeorg | pinal.desai, uday.nagar |
| T7 | coda-07 | fareed.akhlaq | prabhanjan.sindgikar |
| T8 | coda-08 | feroz.basha1 | rakesh.gunna |

(All `@coles.com.au`. 22 confirmed guests, 22 grants.)

Because each user is granted `use` on **only their team's host_id**, a guest who
opens the Omnigent host picker sees exactly one CoDA — their team's — and cannot
launch a session on any other team's host. That is the segregation you asked for.
