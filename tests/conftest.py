"""Shared pytest fixtures for Community Organizer tests.

This file is auto-discovered by pytest and provides fixtures available to
every test in the suite. The pattern is:

    1. Set AWS env vars *before* any `community_organizer.*` import (some of those
       modules read env at import time — e.g. ``db.TABLE_NAME``).
    2. Use moto's ``mock_aws`` context to intercept all boto3 calls so no
       real AWS account is touched.
    3. Provide a ``ddb_table`` fixture that creates a fresh test table
       table per test (matching the schema in ``template.yaml``) and
       tears it down after.

If you add a new test that needs DynamoDB, just add ``ddb_table`` as a
parameter — pytest wires it in for you.
"""
from __future__ import annotations

import os

# Set AWS env vars BEFORE any community_organizer.* import. db.TABLE_NAME and other
# module-level constants read env at import time, so anything that imports
# `community_organizer.core.db` first wins. By setting these here, tests get a
# consistent fake account / table name regardless of import order.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TABLE_NAME", "community-organizer")
os.environ.setdefault("COMMUNITY_ID", "test-community")
os.environ.setdefault("DOMAIN_NAME", "test.example.com")
# auth.py reads these at import time. Tests don't hit Cognito but the
# module-level constants need to resolve.
os.environ.setdefault("USER_POOL_ID", "us-east-1_test")
os.environ.setdefault("USER_POOL_CLIENT_ID", "test-client-id")
os.environ.setdefault("COGNITO_DOMAIN", "auth.test.example.com")

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def ddb_table():
    """Yield a freshly-created DynamoDB table mirroring template.yaml's schema.

    Each test gets its own moto-backed table — no state bleeds between tests.
    The table has the same PK/SK/GSI1 layout as production, so any code that
    works against the real table works against this one.

    Schema (matches ``template.yaml``):
        - PK (hash) + SK (range), both String
        - GSI1: GSI1PK (hash) + GSI1SK (range), ProjectionType=ALL

    Usage in a test::

        def test_something(ddb_table):
            ddb_table.put_item(Item={"PK": "X", "SK": "Y"})
            assert ddb_table.scan()["Count"] == 1
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="community-organizer",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        client.get_waiter("table_exists").wait(TableName="community-organizer")
        yield boto3.resource("dynamodb", region_name="us-east-1").Table("community-organizer")
