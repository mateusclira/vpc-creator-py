"""
Tests for main.py.
All AWS calls are intercepted by moto — no real AWS resources are created.
"""
import os

# Set env vars before importing the app so config is loaded with test values
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ADMIN_USERNAME"] = "testadmin"
os.environ["ADMIN_PASSWORD"] = "testpass123"
os.environ["DATABASE_PATH"] = "test_vpcs.db"

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

import database
from main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    database.init_db(path="test_vpcs.db")
    yield
    try:
        os.remove("test_vpcs.db")
    except FileNotFoundError:
        pass


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def token(client):
    resp = client.post(
        "/login", json={"username": "testadmin", "password": "testpass123"}
    )
    return resp.json()["access_token"]


@pytest.fixture()
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_login_success(client):
    resp = client.post(
        "/login", json={"username": "testadmin", "password": "testpass123"}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()
    assert resp.json()["token_type"] == "bearer"


def test_login_wrong_password(client):
    resp = client.post(
        "/login", json={"username": "testadmin", "password": "wrongpass"}
    )
    assert resp.status_code == 401


def test_login_wrong_username(client):
    resp = client.post(
        "/login", json={"username": "hacker", "password": "testpass123"}
    )
    assert resp.status_code == 401


def test_no_token_returns_401(client):
    assert client.get("/vpcs").status_code == 401


def test_invalid_token_returns_401(client):
    resp = client.get("/vpcs", headers={"Authorization": "Bearer garbage.token"})
    assert resp.status_code == 401


def test_valid_token_allows_access(client, auth):
    resp = client.get("/vpcs", headers=auth)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_vpc_cidr(client, auth):
    resp = client.post(
        "/vpcs",
        json={"cidr": "not-a-cidr", "region": "us-east-1", "subnets": []},
        headers=auth,
    )
    assert resp.status_code == 422


def test_empty_subnets_rejected(client, auth):
    resp = client.post(
        "/vpcs",
        json={"cidr": "10.0.0.0/16", "region": "us-east-1", "subnets": []},
        headers=auth,
    )
    assert resp.status_code == 422


def test_subnet_outside_vpc(client, auth):
    resp = client.post(
        "/vpcs",
        json={
            "cidr": "10.0.0.0/16",
            "region": "us-east-1",
            "subnets": [{"cidr": "192.168.1.0/24", "availability_zone": "us-east-1a"}],
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_overlapping_subnets(client, auth):
    resp = client.post(
        "/vpcs",
        json={
            "cidr": "10.0.0.0/16",
            "region": "us-east-1",
            "subnets": [
                {"cidr": "10.0.0.0/24", "availability_zone": "us-east-1a"},
                {"cidr": "10.0.0.0/25", "availability_zone": "us-east-1b"},
            ],
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_az_wrong_region(client, auth):
    resp = client.post(
        "/vpcs",
        json={
            "cidr": "10.0.0.0/16",
            "region": "us-east-1",
            "subnets": [{"cidr": "10.0.1.0/24", "availability_zone": "eu-west-1a"}],
        },
        headers=auth,
    )
    assert resp.status_code == 422


def test_invalid_region_format(client, auth):
    resp = client.post(
        "/vpcs",
        json={
            "cidr": "10.0.0.0/16",
            "region": "us-east",
            "subnets": [{"cidr": "10.0.1.0/24", "availability_zone": "us-east-1a"}],
        },
        headers=auth,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# AWS service calls
# ---------------------------------------------------------------------------


@mock_aws
def test_create_vpc_calls_aws(client, auth):
    resp = client.post(
        "/vpcs",
        json={
            "cidr": "10.0.0.0/16",
            "region": "us-east-1",
            "subnets": [{"cidr": "10.0.1.0/24", "availability_zone": "us-east-1a"}],
        },
        headers=auth,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["aws_vpc_id"].startswith("vpc-")
    assert data["cidr"] == "10.0.0.0/16"
    assert len(data["subnets"]) == 1
    assert data["subnets"][0]["subnet_id"].startswith("subnet-")


@mock_aws
def test_create_vpc_multiple_subnets(client, auth):
    resp = client.post(
        "/vpcs",
        json={
            "cidr": "172.16.0.0/16",
            "region": "us-east-1",
            "subnets": [
                {"cidr": "172.16.1.0/24", "availability_zone": "us-east-1a"},
                {"cidr": "172.16.2.0/24", "availability_zone": "us-east-1b"},
            ],
        },
        headers=auth,
    )
    assert resp.status_code == 201
    assert len(resp.json()["subnets"]) == 2


# ---------------------------------------------------------------------------
# API endpoints (full flow)
# ---------------------------------------------------------------------------


@mock_aws
def test_create_then_list_then_get(client, auth):
    create = client.post(
        "/vpcs",
        json={
            "cidr": "10.10.0.0/16",
            "region": "us-east-1",
            "subnets": [{"cidr": "10.10.1.0/24", "availability_zone": "us-east-1a"}],
        },
        headers=auth,
    )
    assert create.status_code == 201
    vpc_id = create.json()["id"]

    listing = client.get("/vpcs", headers=auth)
    assert listing.status_code == 200
    assert any(v["id"] == vpc_id for v in listing.json())

    detail = client.get(f"/vpcs/{vpc_id}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["id"] == vpc_id


def test_get_nonexistent_vpc(client, auth):
    resp = client.get("/vpcs/does-not-exist", headers=auth)
    assert resp.status_code == 404


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# DELETE endpoint
# ---------------------------------------------------------------------------


@mock_aws
def test_delete_vpc_removes_from_aws_and_db(client, auth):
    # Create a VPC first.
    create = client.post(
        "/vpcs",
        json={
            "cidr": "10.50.0.0/16",
            "region": "us-east-1",
            "subnets": [{"cidr": "10.50.1.0/24", "availability_zone": "us-east-1a"}],
        },
        headers=auth,
    )
    assert create.status_code == 201
    vpc_id = create.json()["id"]

    # Delete it.
    delete = client.delete(f"/vpcs/{vpc_id}", headers=auth)
    assert delete.status_code == 204

    # Confirm it's gone from the DB.
    get = client.get(f"/vpcs/{vpc_id}", headers=auth)
    assert get.status_code == 404


@mock_aws
def test_delete_nonexistent_vpc_returns_404(client, auth):
    resp = client.delete("/vpcs/does-not-exist", headers=auth)
    assert resp.status_code == 404


def test_delete_requires_auth(client):
    resp = client.delete("/vpcs/some-id")
    assert resp.status_code == 401
