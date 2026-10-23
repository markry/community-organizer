"""First / last name splitting and surname sorting (core/names.py)."""
from __future__ import annotations

import pytest

from community_organizer.core import names
from community_organizer.core.models import User


@pytest.mark.parametrize("whole,first,last,sure", [
    ("Pat Example", "Pat", "Example", True),
    ("Pat Q. Example", "Pat Q.", "Example", True),        # middle initial
    ("Fr. Pat Example", "Fr. Pat", "Example", True),      # title stays first
    ("Father Example", "Father", "Example", True),
    ("Pat Example Jr.", "Pat", "Example Jr.", True),      # suffix stays last
    ("Pat Q. Example, Jr.", "Pat Q.", "Example Jr.", True),
    ("Pat de Example", "Pat", "de Example", True),        # two-part surname
    ("Pat Lee Example", "Pat Lee", "Example", False),     # middle or surname?
    ("pexample", "pexample", "", False),                  # a username
    ("Pat Example (work)", "Pat Example (work)", "", False),
    ("", "", "", False),
])
def test_split(whole, first, last, sure) -> None:
    assert names.split(whole) == (first, last, sure)


def test_compose() -> None:
    assert names.compose("Fr. Pat", "Example") == "Fr. Pat Example"
    assert names.compose(" Pat ", None) == "Pat"


def test_sort_key_files_by_surname_then_first_name() -> None:
    def u(first, last, whole=None):
        return User(community_id="c", email="x@example.com",
                    name=whole or names.compose(first, last),
                    first_name=first, last_name=last)
    people = [u("Zed", "Adams"), u("Amy", "Young"), u("Bob", "Adams"),
              u("Cal", "de Baker"), u(None, None, "unsplit")]
    ordered = [p.name for p in sorted(people, key=names.sort_key)]
    assert ordered == ["Bob Adams", "Zed Adams", "Cal de Baker", "unsplit",
                       "Amy Young"]
