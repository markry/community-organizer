"""First / last names: composing the display name, guessing a split for a
legacy single-field name, and the sort order every member list uses.

A person's display ``name`` stays the one string the rest of the app reads
(emails, invites, rosters). ``first_name`` / ``last_name`` exist so lists
can sort by surname. A clergy title stays with the first name ("Fr. Paul"),
since that is how such a person is addressed; a suffix stays with the last
name ("Smith Jr.") so it never becomes the sort key.
"""
from __future__ import annotations

import re

_TITLES = {"fr.", "fr", "father", "rev.", "rev", "msgr.", "msgr", "deacon",
           "dcn.", "br.", "brother", "sister", "bishop", "mother",
           "dr.", "dr"}
_SUFFIXES = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "esq.", "esq",
             "phd", "ph.d.", "md", "m.d."}
# Lower-case words that open a two-part surname: "Francis de Rosa".
_PARTICLES = {"de", "del", "della", "di", "da", "van", "von", "der", "den",
              "la", "le", "du", "dos", "das"}


def compose(first: str | None, last: str | None) -> str:
    return " ".join(p for p in ((first or "").strip(), (last or "").strip())
                    if p)


def split(name: str | None) -> tuple[str, str, bool]:
    """Best-guess (first, last, sure) for a one-field name. ``sure`` is
    False when a person should look: a single word (often an email
    username), three or more names that aren't a middle initial, or
    anything bracketed."""
    raw = " ".join((name or "").split())
    if not raw or "(" in raw or "[" in raw or "@" in raw:
        return raw, "", False
    toks = raw.replace(",", " ").split()
    pre: list[str] = []
    while len(toks) > 1 and toks[0].lower() in _TITLES:
        pre.append(toks.pop(0))
    suf: list[str] = []
    while len(toks) > 1 and toks[-1].lower() in _SUFFIXES:
        suf.insert(0, toks.pop())
    if len(toks) == 1 and pre:             # "Father Example": title + surname
        return " ".join(pre), " ".join(toks + suf), True
    if len(toks) < 2:
        return raw, "", False
    sure = True
    for i in range(1, len(toks)):
        if toks[i] in _PARTICLES:          # lower-case only: "de", not "De"
            first, last = toks[:i], toks[i:]
            break
    else:
        first, last = toks[:-1], toks[-1:]
        middles = toks[1:-1]
        if middles and not all(re.fullmatch(r"[A-Za-z]\.?", m)
                               for m in middles):
            sure = False                   # middle name or two-part surname?
    return " ".join(pre + first), " ".join(last + suf), sure


def sort_key(user) -> tuple[str, str]:
    """Surname, then first name, case-insensitive. Someone without a stored
    last name sorts by their whole name."""
    last = (getattr(user, "last_name", None) or "").strip()
    first = (getattr(user, "first_name", None) or "").strip()
    if not last:
        return ((user.name or user.email or "").lower(), "")
    # "de Rosa" files under D, "Smith Jr." just after "Smith".
    return (last.lower(), first.lower())
