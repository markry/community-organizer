"""Tests for the bounce Lambda (D16 SNS-topic validation, D17 complaint counter)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from community_organizer.core import db
from community_organizer.core.models import Community, User
from community_organizer.lambdas import bounce


@pytest.fixture
def seeded_user(ddb_table, monkeypatch):
    """Seed a community + one user; wire the bounce module to that
    community."""
    cid = "test-community"
    db.put_community(Community(community_id=cid, name="Test Parish"))
    user = User(community_id=cid, email="member@example.com",
                name="Member")
    db.put_user(user)
    monkeypatch.setattr(bounce, "COMMUNITY_ID", cid)
    return user


def _bounce_event(*, topic_arn: str, body: dict,
                  event_source: str = "aws:sns") -> dict:
    return {"Records": [{
        "EventSource": event_source,
        "Sns": {"TopicArn": topic_arn, "Message": json.dumps(body)},
    }]}


def _complaint_payload(email: str) -> dict:
    return {
        "notificationType": "Complaint",
        "mail": {"source": "noreply@community.example.org", "messageId": "m1",
                 "commonHeaders": {"subject": "Test"}},
        "complaint": {
            "complainedRecipients": [{"emailAddress": email}],
            "complaintFeedbackType": "abuse",
        },
    }


# ---- D16: topic ARN gate ---------------------------------------------------

def test_rejects_record_from_unexpected_topic(seeded_user, monkeypatch) -> None:
    """The Lambda rejects SNS records whose TopicArn doesn't match the
    one it was configured to subscribe to — defense in depth in case a
    second topic is ever subscribed (security fix D16)."""
    monkeypatch.setattr(bounce, "EXPECTED_SNS_TOPIC_ARN",
                        "arn:aws:sns:us-east-1:111:expected-topic")
    event = _bounce_event(
        topic_arn="arn:aws:sns:us-east-1:222:foreign-topic",
        body=_complaint_payload("member@example.com"),
    )
    result = bounce.lambda_handler(event, None)
    assert result["handled"] == 0
    # User's complaint_count must NOT have moved.
    fresh = db.get_user_by_email(seeded_user.community_id,
                                  "member@example.com")
    assert (fresh.complaint_count or 0) == 0


def test_rejects_non_sns_event_source(seeded_user, monkeypatch) -> None:
    """An EventSource that isn't aws:sns is dropped."""
    monkeypatch.setattr(bounce, "EXPECTED_SNS_TOPIC_ARN",
                        "arn:aws:sns:us-east-1:111:expected-topic")
    event = _bounce_event(
        topic_arn="arn:aws:sns:us-east-1:111:expected-topic",
        body=_complaint_payload("member@example.com"),
        event_source="aws:ses",   # wrong source
    )
    result = bounce.lambda_handler(event, None)
    assert result["handled"] == 0


def test_accepts_record_from_expected_topic(seeded_user, monkeypatch) -> None:
    """The expected ARN passes the gate and the complaint is recorded."""
    expected = "arn:aws:sns:us-east-1:111:expected-topic"
    monkeypatch.setattr(bounce, "EXPECTED_SNS_TOPIC_ARN", expected)
    event = _bounce_event(topic_arn=expected,
                          body=_complaint_payload("member@example.com"))
    result = bounce.lambda_handler(event, None)
    assert result["handled"] == 1


# ---- D17: complaint counter ------------------------------------------------

def test_first_complaint_does_not_silence_user(seeded_user, monkeypatch) -> None:
    """A single complaint must NOT flip email_undeliverable —
    complaint mis-clicks are common and one shouldn't kill delivery
    (security fix D17). Counter moves to 1."""
    monkeypatch.setattr(bounce, "EXPECTED_SNS_TOPIC_ARN", "")  # disable gate
    monkeypatch.setattr(bounce, "COMPLAINT_THRESHOLD", 2)
    event = _bounce_event(
        topic_arn="any",
        body=_complaint_payload("member@example.com"),
    )
    bounce.lambda_handler(event, None)
    fresh = db.get_user_by_email("test-community", "member@example.com")
    assert fresh.complaint_count == 1
    assert fresh.email_undeliverable is False


def test_threshold_complaint_silences_user(seeded_user, monkeypatch) -> None:
    """Reaching the threshold (default 2) silences the user."""
    monkeypatch.setattr(bounce, "EXPECTED_SNS_TOPIC_ARN", "")
    monkeypatch.setattr(bounce, "COMPLAINT_THRESHOLD", 2)
    event = _bounce_event(
        topic_arn="any",
        body=_complaint_payload("member@example.com"),
    )
    # First complaint: counter 1, not silenced.
    bounce.lambda_handler(event, None)
    # Second complaint: counter 2, threshold reached → silenced.
    bounce.lambda_handler(event, None)
    fresh = db.get_user_by_email("test-community", "member@example.com")
    assert fresh.complaint_count == 2
    assert fresh.email_undeliverable is True


def test_complaint_after_silenced_does_not_increment(seeded_user, monkeypatch) -> None:
    """Once a user is silenced, further complaints are recorded as
    email-log entries but the user record isn't repeatedly updated."""
    monkeypatch.setattr(bounce, "EXPECTED_SNS_TOPIC_ARN", "")
    monkeypatch.setattr(bounce, "COMPLAINT_THRESHOLD", 1)
    event = _bounce_event(
        topic_arn="any",
        body=_complaint_payload("member@example.com"),
    )
    bounce.lambda_handler(event, None)
    # The user is now silenced (threshold 1, counter 1).
    fresh = db.get_user_by_email("test-community", "member@example.com")
    assert fresh.email_undeliverable is True
    assert fresh.complaint_count == 1
    # A second complaint should be ignored at the user-record level
    # (already silenced — the code guards on
    # ``not related_user.email_undeliverable``).
    bounce.lambda_handler(event, None)
    fresh2 = db.get_user_by_email("test-community", "member@example.com")
    assert fresh2.complaint_count == 1  # unchanged
