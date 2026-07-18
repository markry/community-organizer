# Dependencies and supply-chain hygiene

## Layout

```
src/
  requirements.in       # source-of-truth, human-edited
  requirements.txt      # generated lock file with hashes — SAM reads this
pyproject.toml          # dev deps (pytest, moto, etc.) — never deployed
```

The Lambda artifact only contains what's resolved from
`src/requirements.txt`. The dev deps in `pyproject.toml`
`[project.optional-dependencies].dev` are installed locally and on CI
for tests, but they don't reach production.

## Runtime dependencies

As of 2026-05-30:

| Library | Version | Purpose | Notes |
|---|---|---|---|
| `boto3` (+ `botocore`, `jmespath`, `s3transfer`) | 1.43.x | AWS SDK | Maintained by AWS; not replaceable |
| `python-jose` (+ `ecdsa`, `pyasn1`, `rsa`) | 3.5.x | JWT verification on the Cognito ID-token path | Critical for `auth.py:verify_id_token` |
| `tzdata` | 2026.x | IANA timezone data backing Python `zoneinfo` | Pure data — candidate to vendor |
| `urllib3`, `python-dateutil`, `six` | transitive (boto + jose) | — | |

## Updating

```bash
# 1. Edit src/requirements.in to bump a constraint or add a dep.
# 2. Regenerate the lock from the active venv:
cd src && pip-compile --generate-hashes --output-file requirements.txt requirements.in

# 3. Inspect the diff — every transitive bump should be deliberate.
git diff src/requirements.txt

# 4. Run the test suite.
pytest

# 5. Run the audit (see below).
scripts/audit-deps.sh

# 6. Build + deploy.
sam build && sam deploy --no-confirm-changeset
```

## Vulnerability auditing

```bash
scripts/audit-deps.sh
```

Wraps `pip-audit --strict --requirement src/requirements.txt`. Exit
code 0 = clean. Non-zero = a published CVE matches one of our deps
(or a transitive); read the output and either bump or apply the
recommended workaround.

Cadence:

- **Every PR** — make audit part of the pre-merge checklist.
- **Nightly / weekly** — schedule a cron or GitHub Action so a CVE
  published after the last PR doesn't go unnoticed.
- **Within 24-48h of advisory** — for any GitHub Security Advisory or
  PyPI advisory referencing our deps, audit and patch promptly.

## GitHub Security Advisories

Subscribe (one-time, in your GitHub account settings):

- Watch → Custom → Security Alerts on the `boto3`, `python-jose`,
  `tzdata`, `urllib3`, and `cryptography` repositories.
- Or enable Dependabot security alerts on this repo
  (`Settings → Security & analysis → Dependabot alerts`). Dependabot
  will open a PR when a CVE matches `src/requirements.txt`.

## Pinning policy

The lock file pins every dep — including transitive ones — to an exact
version with a SHA256 hash. `pip install --require-hashes -r
src/requirements.txt` refuses to install anything that doesn't match.
SAM's PythonPipBuilder runs `pip install -r requirements.txt`; it
doesn't pass `--require-hashes` by default, but the hashes are still a
useful integrity-check artifact for human review and for CI.

If you want hash verification at SAM-build time (defense in depth),
override the Lambda build behavior in `template.yaml` (currently uses
the SAM-managed PythonPipBuilder; switching to `BuildMethod: makefile`
gives you full control of the install command). Not a priority today.

## Replaceable / vendor candidates

These are options if we want to shrink the production attack surface
further. Not done now; tracked here so they don't get lost.

- **Replace `click` with stdlib `argparse`.** The CLI is admin-only,
  has maybe 8 commands. Argparse can express all of them. Removes
  `click` + `colorama`. ~100-line diff in `cli.py`.
- **Vendor `tzdata`.** It's just data files — check in a snapshot
  from <https://www.iana.org/time-zones>, refresh on a known cadence
  (~6 months or after any DST rule change). Zero code execution risk.
- **Keep `python-jose`.** Cannot reasonably roll our own JWT verifier;
  alternatives (PyJWT, jwcrypto) have the same risk profile.
- **Keep `boto3`.** AWS is the trust anchor anyway.
