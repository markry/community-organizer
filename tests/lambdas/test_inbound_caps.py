"""Tests for D15 — inbound MIME size and walk caps.

The _MAX_INBOUND_BYTES and _MAX_INBOUND_PARTS constants protect the
Lambda from DoS via huge / pathological inbound emails. SES caps
inbound at 40 MB but our legitimate traffic is tiny; anything above
the local cap is dropped before parsing.
"""
from __future__ import annotations

from community_organizer.lambdas import inbound


def test_max_inbound_bytes_default_is_1mb() -> None:
    assert inbound._MAX_INBOUND_BYTES == 1_000_000


def test_max_inbound_parts_default_is_50() -> None:
    assert inbound._MAX_INBOUND_PARTS == 50


def test_oversize_raw_message_is_rejected(monkeypatch) -> None:
    """A raw payload larger than _MAX_INBOUND_BYTES is dropped before
    ``email.message_from_bytes`` ever runs (security fix D15)."""
    huge = b"x" * (inbound._MAX_INBOUND_BYTES + 1)

    class _FakeS3:
        def get_object(self, **_):
            class _Body:
                def read(self):
                    return huge
            return {"Body": _Body()}

    monkeypatch.setattr(inbound, "INBOUND_BUCKET", "test-bucket")
    monkeypatch.setattr(inbound, "_get_s3", lambda: _FakeS3())
    parsed_attempts = []

    def _spy_parse(raw):
        parsed_attempts.append(len(raw))
        import email as _email
        return _email.message_from_bytes(raw)

    monkeypatch.setattr(inbound.email, "message_from_bytes", _spy_parse)

    result = inbound._process_message("abc123", verdicts_pass=True)
    assert result is False
    # The size cap fired BEFORE parsing — the parser was never called.
    assert parsed_attempts == []
