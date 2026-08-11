# CoDA repository-clone protocol verification

Date: 2026-08-11
Bead: `omnigent-0hd.9.13`

## Source and scope

- Dedicated branch: `fix/coda-existing-claim-repo-clone`
- Chosen published base: `fork/dev` at `55e5be7b8491424b1fa7537f2e16effdabd7a4ee`
- Base verification: exact head and merge-base match; ahead/behind `0/0` before implementation.
- Scope: Omnigent server/provider protocol forwarding, App fencing, create/adopt,
  Release/Resume/relaunch reconstruction, cleanup, validation, and operator docs.
- No deployment/workshop checkout was modified.

## Deterministic verification

- Focused managed-host/provider/lifecycle/integration suite: `378 passed`.
- Final focused protocol/security suite: `60 passed`.
- Ruff checks: pass.
- Ruff formatting: pass.
- Mypy on changed production modules: pass.
- `git diff --check`: pass.
- A repository-wide collection contains more than 20,000 tests; two monolithic
  local attempts exceeded the bounded command window. The changed server-rest
  and server-integration surfaces were instead exercised directly, with the
  normal fork PR CI retained as the repository-wide gate.

Covered behavior includes new-claim repository threading, existing-claim
adoption, retained repository Resume, managed relaunch, granting-App-only
routing, cloned-directory persistence, shared-session cleanup, sibling
preservation, malformed/credential-bearing URL redaction, mixed-version
capability failure, and non-repository compatibility.

## Adversarial review

Fresh-context requirements, security/data, and follow-up reviews were run
read-only. Reproduced findings corrected before final validation:

- reject a materialized marker when the absolute cloned directory is absent;
- keep non-repository shared Release on its previous protocol shape;
- omit raw validation input and credential-bearing URLs from 422 responses;
- avoid echoing forged/removed provider identifiers.

Follow-up review reported no remaining findings in the changed scope.

## Deployment and live evidence

CoDA protocol support must be merged and deployed before this Omnigent change.
Merged SHA, deployment receipt, bounded live adoption/Resume evidence, temporary
resource cleanup, and post-deploy health are recorded after those gates run;
no production identifiers or private repository contents belong in this pack.
