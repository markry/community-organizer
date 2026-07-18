# Contributing

Community Organizer is published so the code can be inspected, reused, and
adapted. External contributions are welcome as issues or pull requests, but
review capacity may be limited while the project is in beta.

**First, enable the commit guard:**

```sh
git config core.hooksPath .githooks
```

It rejects commit messages containing real people's names, live ids, emails, or
deployment identifiers. This repo previously carried a real user's first name in
its history despite the bullet below already saying not to — which is why there
is now a hook and not just a bullet. Write about **roles** ("the AA", "a
member"), never people. See [CLAUDE.md](CLAUDE.md).

Before opening a pull request:

- Keep changes focused and easy to review.
- Add or update tests for behavior changes.
- Never commit deployment-specific values, credentials, live domains, account
  IDs, or personal data — in code, tests, fixtures, comments, or commit
  messages. Fixtures use invented people at `example.com`.
- Run the local test suite with `pytest`.

Security reports should not be filed as public issues. See
[docs/SECURITY.md](docs/SECURITY.md) for reporting guidance.
