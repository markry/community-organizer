# Community Organizer — working rules

## 1. Never write about real people. This is the rule that has been broken.

This app serves a real parish. Its users are volunteers who never agreed to be
case studies. **Everything in this repo — code, tests, fixtures, comments, and
above all commit messages — describes ROLES, never PEOPLE.**

- Write "the AA", "an organizer", "a member", "a household".
- Never a real first name, surname, email, phone, dish, or quote.
- Never a real `user_id` / `event_id` / `app_id` (32-hex) from the live table.
- Never a live deployment identifier (account id, hosted zone, real domain).

**This has already gone wrong.** On 2026-07-15, hours into a session spent
scrubbing PII out of this project, a real member's first name went into two
commit messages of the then-public repo — narrating her behaviour,
unflatteringly, in permanent history. Nobody was being careless on purpose. The
name was simply the most natural way to explain *why* a change existed, and no
step in the process ever asked the question. That is why the history was
rewritten and this file exists.

Enable the guard once per clone. It fails the commit rather than advising you:

```sh
git config core.hooksPath .githooks
```

The name denylist lives in the **private** `community-organizer-ops` repo
(`pii-denylist.txt`) — a roster of real names is exactly what must not be here.
Clone it as a sibling directory or the name check silently skips. **When a real
person enters a conversation about this project, add them to that denylist
before writing any commit.**

If you catch yourself typing a name to explain motivation, the motivation is
the point, not the person: "an AA wanted a date added to a live poll" carries
the same information and costs nobody their privacy.

## 2. The public/private split

| Belongs HERE (was public, now private pending review) | Belongs in `community-organizer-ops` (private) |
|---|---|
| Parameterised templates, `src/`, tests | `samconfig.toml` — real overrides |
| Generic defaults (`000000000000` ARNs) | `deploy.sh`, `DEPLOY.md` runbook |
| `co@amdg.io` (the project's published contact) | `pii-denylist.txt` |
| Fixtures using `example.com` | One-off data scripts touching real rows |

`samconfig.toml` is gitignored here. Keep it that way.

## 3. Deploys

**Never `sam sync --code` against prod.** It skips the dependency layer and the
site 502s — it did exactly that on 2026-07-15 (3m05s of downtime). Deploy with
`community-organizer-ops/deploy.sh`, which encodes the only safe path and
smoke-tests afterwards. `sam build` needs `--config-env` too, not just
`sam deploy`.

## 4. Tests

`pytest` against a moto-backed table (`tests/conftest.py`). The full suite is
the bar for any change to `src/`. Fixtures are invented people at
`example.com` — never a real roster.
