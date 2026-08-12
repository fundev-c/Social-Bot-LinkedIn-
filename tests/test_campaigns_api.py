"""
Campaign API tests.

These run against the real app, real auth and a real (in-memory) database
rather than mocking the service layer, because the properties worth testing
here — that a route refuses an anonymous caller, and that one tenant cannot
reach another's campaigns — live in exactly the wiring a mock replaces.

Covers: auth, tenant isolation, creation, idempotency, execution, validation
and progress.
"""

import uuid
from datetime import datetime

import pytest

pytestmark = pytest.mark.asyncio


def auth(token: str, idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    return headers


@pytest.fixture
def sample_campaign_data():
    """Sample campaign creation data."""
    return {
        "name": "Test Campaign",
        "description": "Test description",
        "target_urls": ["https://linkedin.com/posts/test-123"],
        "account_ids": [str(uuid.uuid4())],
        "actions": {"like": True, "comment": False},
        "priority": 1,
    }


async def create_campaign(client, token, data, key=None):
    return await client.post(
        "/api/v1/campaigns",
        json=data,
        headers=auth(token, key or str(uuid.uuid4())),
    )


class TestCampaignAuth:
    """Every campaign route requires a valid session."""

    async def test_anonymous_requests_are_refused(self, api_client, sample_campaign_data):
        """
        No Authorization header -> 401, on reads and writes alike.

        This is the regression guard for the original defect: these routes were
        entirely unauthenticated while every other route required a session.
        """
        campaign_id = uuid.uuid4()
        unauthenticated = [
            ("get", "/api/v1/campaigns", {}),
            ("get", f"/api/v1/campaigns/{campaign_id}", {}),
            ("get", f"/api/v1/campaigns/{campaign_id}/status", {}),
            ("get", f"/api/v1/campaigns/{campaign_id}/tasks", {}),
            ("delete", f"/api/v1/campaigns/{campaign_id}", {}),
            ("post", f"/api/v1/campaigns/{campaign_id}/pause", {}),
        ]
        for method, url, kwargs in unauthenticated:
            resp = await getattr(api_client, method)(url, **kwargs)
            assert resp.status_code == 401, f"{method.upper()} {url} was not authenticated"

        # Writes that also require an idempotency key must still fail on auth,
        # not slip through on a header technicality.
        resp = await api_client.post(
            "/api/v1/campaigns",
            json=sample_campaign_data,
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert resp.status_code == 401

        resp = await api_client.post(
            f"/api/v1/campaigns/{campaign_id}/start",
            headers={"X-Idempotency-Key": str(uuid.uuid4())},
        )
        assert resp.status_code == 401

    async def test_garbage_token_is_refused(self, api_client):
        resp = await api_client.get(
            "/api/v1/campaigns", headers={"Authorization": "Bearer not.a.jwt"}
        )
        assert resp.status_code == 401


class TestTenantIsolation:
    """One organization must not be able to observe or touch another's work."""

    async def test_campaign_is_invisible_to_another_org(
        self, api_client, issue_token, sample_campaign_data
    ):
        """
        Org B gets 404 — not 403 — for org A's campaign, on every route.

        404 is deliberate: a 403 would confirm the id exists, which is itself a
        disclosure to a caller with no right to know.
        """
        alice = issue_token("user_alice", org="org_alpha")
        bob = issue_token("user_bob", org="org_beta")

        created = await create_campaign(api_client, alice, sample_campaign_data)
        assert created.status_code == 201
        campaign_id = created.json()["id"]

        # Alice sees her campaign.
        mine = await api_client.get(f"/api/v1/campaigns/{campaign_id}", headers=auth(alice))
        assert mine.status_code == 200

        # Bob sees nothing, by any route.
        for method, url in [
            ("get", f"/api/v1/campaigns/{campaign_id}"),
            ("get", f"/api/v1/campaigns/{campaign_id}/status"),
            ("get", f"/api/v1/campaigns/{campaign_id}/tasks"),
            ("delete", f"/api/v1/campaigns/{campaign_id}"),
        ]:
            resp = await getattr(api_client, method)(url, headers=auth(bob))
            assert resp.status_code == 404, f"{method.upper()} {url} leaked across orgs"

        patched = await api_client.patch(
            f"/api/v1/campaigns/{campaign_id}",
            json={"name": "Hijacked"},
            headers=auth(bob),
        )
        assert patched.status_code == 404

        started = await api_client.post(
            f"/api/v1/campaigns/{campaign_id}/start",
            headers=auth(bob, str(uuid.uuid4())),
        )
        assert started.status_code == 404

        paused = await api_client.post(
            f"/api/v1/campaigns/{campaign_id}/pause", headers=auth(bob)
        )
        assert paused.status_code == 404

        # And Alice's campaign survived all of that untouched.
        after = await api_client.get(f"/api/v1/campaigns/{campaign_id}", headers=auth(alice))
        assert after.status_code == 200
        assert after.json()["name"] == sample_campaign_data["name"]
        assert after.json()["status"] == "draft"

    async def test_list_returns_only_the_callers_org(
        self, api_client, issue_token, sample_campaign_data
    ):
        alice = issue_token("user_alice2", org="org_alpha2")
        bob = issue_token("user_bob2", org="org_beta2")

        await create_campaign(api_client, alice, {**sample_campaign_data, "name": "Alice A"})
        await create_campaign(api_client, alice, {**sample_campaign_data, "name": "Alice B"})
        await create_campaign(api_client, bob, {**sample_campaign_data, "name": "Bob only"})

        alice_list = await api_client.get("/api/v1/campaigns", headers=auth(alice))
        assert alice_list.status_code == 200
        alice_body = alice_list.json()
        assert alice_body["total"] == 2
        assert {c["name"] for c in alice_body["campaigns"]} == {"Alice A", "Alice B"}

        bob_list = await api_client.get("/api/v1/campaigns", headers=auth(bob))
        assert bob_list.json()["total"] == 1
        assert bob_list.json()["campaigns"][0]["name"] == "Bob only"

    async def test_teammates_share_their_org_campaigns(
        self, api_client, issue_token, sample_campaign_data
    ):
        """Isolation is per-organization, not per-user: a colleague still sees it."""
        founder = issue_token("user_founder", org="org_shared")
        teammate = issue_token("user_teammate", org="org_shared")

        created = await create_campaign(api_client, founder, sample_campaign_data)
        campaign_id = created.json()["id"]

        seen = await api_client.get(
            f"/api/v1/campaigns/{campaign_id}", headers=auth(teammate)
        )
        assert seen.status_code == 200
        assert seen.json()["id"] == campaign_id

    async def test_idempotency_keys_do_not_cross_orgs(
        self, api_client, issue_token, sample_campaign_data
    ):
        """
        The same client-chosen key in two orgs creates two separate campaigns.

        Keys are picked by the caller, so collisions between tenants are
        expected. If they shared a namespace, replaying another org's key would
        return that org's campaign as the "cached" response.
        """
        alice = issue_token("user_alice3", org="org_alpha3")
        bob = issue_token("user_bob3", org="org_beta3")
        shared_key = str(uuid.uuid4())

        a = await create_campaign(
            api_client, alice, {**sample_campaign_data, "name": "Alice's"}, key=shared_key
        )
        b = await create_campaign(
            api_client, bob, {**sample_campaign_data, "name": "Bob's"}, key=shared_key
        )

        assert a.status_code == b.status_code == 201
        assert a.json()["id"] != b.json()["id"]
        assert a.json()["name"] == "Alice's"
        assert b.json()["name"] == "Bob's"


class TestCampaignCreation:
    """Tests for campaign creation endpoint."""

    async def test_create_campaign_success(self, api_client, issue_token, sample_campaign_data):
        """POST with a valid payload returns 201 and a draft campaign."""
        token = issue_token("user_create", org="org_create")
        resp = await create_campaign(api_client, token, sample_campaign_data)

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == sample_campaign_data["name"]
        assert data["status"] == "draft"
        assert data["target_urls"] == sample_campaign_data["target_urls"]
        assert data["progress"]["total_tasks"] == 1

    async def test_create_campaign_missing_idempotency_key(
        self, api_client, issue_token, sample_campaign_data
    ):
        """POST without X-Idempotency-Key returns 422."""
        token = issue_token("user_nokey", org="org_nokey")
        resp = await api_client.post(
            "/api/v1/campaigns", json=sample_campaign_data, headers=auth(token)
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert any("x-idempotency-key" in str(err).lower() for err in detail)


class TestIdempotency:
    """Tests for idempotency behavior."""

    async def test_idempotency_replay(self, api_client, issue_token, sample_campaign_data):
        """POSTing twice with one key returns the same campaign, created once."""
        token = issue_token("user_idem", org="org_idem")
        key = str(uuid.uuid4())

        first = await create_campaign(api_client, token, sample_campaign_data, key=key)
        second = await create_campaign(api_client, token, sample_campaign_data, key=key)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

        listed = await api_client.get("/api/v1/campaigns", headers=auth(token))
        assert listed.json()["total"] == 1


class TestCampaignExecution:
    """Tests for campaign execution."""

    async def test_start_campaign(self, api_client, issue_token, sample_campaign_data):
        """
        Starting moves the campaign to running.

        No orchestrator is attached under the test transport, so zero tasks are
        submitted — the service's documented degraded path.
        """
        token = issue_token("user_start", org="org_start")
        created = await create_campaign(api_client, token, sample_campaign_data)
        campaign_id = created.json()["id"]

        resp = await api_client.post(
            f"/api/v1/campaigns/{campaign_id}/start",
            headers=auth(token, str(uuid.uuid4())),
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["campaign_id"] == campaign_id

        after = await api_client.get(f"/api/v1/campaigns/{campaign_id}", headers=auth(token))
        assert after.json()["status"] == "running"

    async def test_start_missing_campaign_is_404(self, api_client, issue_token):
        token = issue_token("user_missing", org="org_missing")
        resp = await api_client.post(
            f"/api/v1/campaigns/{uuid.uuid4()}/start",
            headers=auth(token, str(uuid.uuid4())),
        )
        assert resp.status_code == 404


class TestValidation:
    """Tests for input validation."""

    async def test_invalid_url_rejected(self, api_client, issue_token):
        """A non-LinkedIn target URL returns 422."""
        token = issue_token("user_badurl", org="org_badurl")
        invalid_data = {
            "name": "Bad Campaign",
            "target_urls": ["https://twitter.com/someuser/status/123"],
            "account_ids": [str(uuid.uuid4())],
            "actions": {"like": True},
        }

        resp = await create_campaign(api_client, token, invalid_data)

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert any("linkedin" in str(err).lower() for err in detail)


class TestCampaignStatus:
    """Tests for campaign status endpoint."""

    async def test_get_campaign_status(self, api_client, issue_token, sample_campaign_data):
        """GET /{id}/status returns progress counters."""
        token = issue_token("user_status", org="org_status")
        data = {**sample_campaign_data, "target_urls": [
            "https://linkedin.com/posts/a-1",
            "https://linkedin.com/posts/b-2",
        ]}
        created = await create_campaign(api_client, token, data)
        campaign_id = created.json()["id"]

        resp = await api_client.get(
            f"/api/v1/campaigns/{campaign_id}/status", headers=auth(token)
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_tasks"] == 2
        assert body["completed_tasks"] == 0
        assert body["pending_tasks"] == 2
        assert body["completion_percent"] == 0.0
