"""Per-member email choices (beyond reminders, which live in
``lead_times_minutes``). A record written before a field existed reads as
the default, which is on."""
from __future__ import annotations


def wants_calendar(user) -> bool:
    """Whether to send this member calendar files (.ics) of any kind:
    invites, updates and cancellations. An email that exists only to carry
    one is skipped when this is off; any other email goes without it."""
    return getattr(user, "calendar_invites", True) is not False


def wants_peer_emails(user) -> bool:
    """Whether to email this member about other members' changes: requests
    to cover an opening or take part in a trade, and who joined or left a
    shift they're on."""
    return getattr(user, "peer_emails", True) is not False


def ics_for(user, ics: str | None) -> str | None:
    """The calendar attachment to send this member: ``ics``, or None."""
    return ics if wants_calendar(user) else None
