"""Smoke tests for the English pluralizer used in schedule emails.

The pluralizer covers the common cases the app cares about: nouns the admin
sets as ``event_noun`` or ``terminology`` (singular form) get auto-converted
to a plural form for table column headers, button labels, etc., *unless*
the admin explicitly overrides via ``event_noun_plural`` /
``terminology_plural`` (for irregular cases like "child"/"children").

Rules implemented (see ``schedule_email._pluralize``):
    - Empty / falsy → return as-is
    - Ends in s, x, z, ch, sh → append "es"  (mass → masses; church → churches)
    - Ends in consonant + y → strip y, append "ies"  (party → parties)
    - Ends in vowel + y → just append "s"  (key → keys)
    - Otherwise → append "s"  (event → events)

These tests are intentionally small and exhaustive of the public rule set
rather than the implementation: if we ever swap to a fancier pluralizer
(e.g. an inflection library), the same assertions should still hold.
"""
from __future__ import annotations

import pytest

from community_organizer.core.schedule_email import _pluralize


@pytest.mark.parametrize(
    "singular, expected",
    [
        # Regular -s
        ("event", "events"),
        ("volunteer", "volunteers"),
        ("usher", "ushers"),
        # -s / -x / -z  →  -es
        ("Mass", "Masses"),
        ("box", "boxes"),
        ("buzz", "buzzes"),
        # -ch / -sh  →  -es
        ("church", "churches"),
        ("dish", "dishes"),
        # consonant + y  →  -ies
        ("party", "parties"),
        ("city", "cities"),
        # vowel + y  →  -s  (must not become "key" → "kies")
        ("key", "keys"),
        ("day", "days"),
        # Empty input round-trips
        ("", ""),
    ],
)
def test_pluralize_rules(singular: str, expected: str) -> None:
    """Each (singular, expected) pair exercises one branch of _pluralize.

    The parametrize decorator turns this into 13 separate test cases that
    pytest reports individually — so if "party → parties" breaks, the
    failure points at that exact row, not "test_pluralize_rules".
    """
    assert _pluralize(singular) == expected


def test_pluralize_preserves_case() -> None:
    """First-letter case must round-trip — schedule email headers rely on it.

    The pluralizer lower-cases internally only to check the suffix; it must
    leave the caller's casing intact so ``"Mass" → "Masses"`` (capitalized)
    flows straight into the email column header.
    """
    assert _pluralize("Mass") == "Masses"
    assert _pluralize("mass") == "masses"
