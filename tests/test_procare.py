from datetime import date

import pytest

from procareberry.models import SyncError
from procareberry.procare import AUTH_URL, ProcareClient, api_host
from procareberry.storage import private_json


class Transport:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return (*reply, "")


async def no_sleep(_):
    pass


def login(host="https://api-school.test.procareconnect.com"):
    return 200, {"auth_token": "synthetic-token", "sites": [{"base_url": host, "is_default": True}]}


async def test_school_discovery_pagination_and_saved_session(tmp_path):
    transport = Transport(
        [
            login(),
            (200, {"daily_activities": [{"id": "a"}]}),
            (200, {"daily_activities": [{"id": "b"}]}),
            (200, {"daily_activities": []}),
        ]
    )
    client = ProcareClient(transport, "fake", "fake", tmp_path / "token.json", sleep=no_sleep)
    rows = await client.activities("kid", date(2026, 10, 1), date(2026, 10, 2))
    assert [r["id"] for r in rows] == ["a", "b"]
    assert transport.calls[0][1] == AUTH_URL
    assert transport.calls[1][1].startswith("https://api-school.test.procareconnect.com/")
    restored = ProcareClient(Transport([]), "fake", "fake", tmp_path / "token.json", sleep=no_sleep)
    assert restored.token == "synthetic-token"


async def test_changed_login_authenticates_new_parent_before_listing_children(tmp_path):
    token_file = tmp_path / "token.json"
    private_json(
        token_file,
        {
            "email": "old@example.com",
            "token": "old-synthetic-token",
            "base": "https://api-school.test.procareconnect.com/api/web/",
        },
    )
    transport = Transport([login(), (200, {"kids": [{"id": "kid"}]})])
    client = ProcareClient(transport, "new@example.com", "new-fake-password", token_file)
    assert await client.children() == [{"id": "kid"}]
    assert transport.calls[0][0:2] == ("POST", AUTH_URL)
    assert transport.calls[0][2]["payload"]["email"] == "new@example.com"
    assert transport.calls[1][2]["headers"]["Authorization"] == "Bearer synthetic-token"


async def test_session_expiry_and_bounded_backoff(tmp_path):
    transport = Transport(
        [login(), (401, {}), login(), (429, {}), (503, {}), (200, {"kids": [{"id": "kid"}]})]
    )
    client = ProcareClient(transport, "fake", "fake", tmp_path / "token.json", sleep=no_sleep)
    assert await client.children() == [{"id": "kid"}]
    assert len(transport.calls) == 6


async def test_mfa_and_repeated_page_are_clear_errors(tmp_path):
    client = ProcareClient(
        Transport([(200, {"access_to": "verification"})]),
        "fake",
        "fake",
        tmp_path / "token.json",
        sleep=no_sleep,
    )
    with pytest.raises(SyncError, match="verification"):
        await client.login()
    client = ProcareClient(
        Transport(
            [login(), (200, {"daily_activities": [{"id": "a"}]}), (200, {"daily_activities": [{"id": "a"}]})]
        ),
        "fake",
        "fake",
        tmp_path / "token.json",
        sleep=no_sleep,
    )
    with pytest.raises(SyncError, match="repeated"):
        await client.activities("kid", date(2026, 10, 1), date(2026, 10, 2))


async def test_late_page_failure_does_not_return_partial_feed(tmp_path):
    client = ProcareClient(
        Transport([login(), (200, {"daily_activities": [{"id": "a"}]}), (400, {})]),
        "fake",
        "fake",
        tmp_path / "token.json",
        sleep=no_sleep,
    )
    with pytest.raises(SyncError, match="incomplete"):
        await client.activities("kid", date(2026, 10, 1), date(2026, 10, 2))


async def test_history_is_fetched_in_monthly_windows(tmp_path):
    transport = Transport(
        [
            login(),
            (200, {"daily_activities": [{"id": "a"}]}),
            (200, {"daily_activities": []}),
            (200, {"daily_activities": [{"id": "b"}]}),
            (200, {"daily_activities": []}),
        ]
    )
    client = ProcareClient(transport, "fake", "fake", tmp_path / "token.json", sleep=no_sleep)
    assert len(await client.activities("kid", date(2026, 9, 30), date(2026, 10, 1))) == 2
    assert transport.calls[1][2]["params"]["filters[daily_activity][date_to]"] == "2026-09-30"
    assert transport.calls[3][2]["params"]["filters[daily_activity][date_from]"] == "2026-10-01"


@pytest.mark.parametrize(
    "host",
    [
        "https://procareconnect.com.evil.invalid",
        "http://api-school.procareconnect.com",
        "https://user:pass@api-school.procareconnect.com",
        "https://api-school.procareconnect.com:444",
        "https://api-school.procareconnect.com/other",
    ],
)
def test_bearer_token_never_sent_to_unexpected_host(host):
    with pytest.raises(SyncError):
        api_host(host)
