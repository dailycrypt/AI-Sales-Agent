"""Hot replies, stalled deals, forecast, loop health, alerts, closing a deal."""

from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from harvey.closer import (
    STALL_AFTER,
    assess_health,
    close_deal,
    forecast,
    is_hot,
    is_stalled,
    send_hot_alerts,
)
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager

NOW = datetime(2033, 1, 10)
WEBHOOK = "https://alerts.example.com/hook"


def _convo(sender="prospect", intent="interested", stage="engaged", age=timedelta(0), cid="c1", pid="p"):
    thread = [Message(sender=sender, content="hi", timestamp=NOW - age)]
    return Conversation(id=cid, prospect_id=pid, thread=thread, intent=intent, stage=stage,
                        status="replied")


@pytest_asyncio.fixture
async def state(tmp_path):
    manager = StateManager(str(tmp_path / "test.db"))
    await manager.init_db()
    return manager


@pytest_asyncio.fixture
async def stored(state):
    pid = await state.add_prospect(Prospect(
        first_name="Sam", last_name="Lee", title="Owner", company="Example Co",
        email="sam@example.com"))
    convo = _convo(pid=pid)
    await state.add_conversation(convo)
    await state.update_conversation(convo.id, intent="interested", status="replied")
    return convo


@pytest.mark.parametrize("kwargs,hot", [
    ({}, True),
    ({"intent": "escalate"}, True),
    ({"intent": "objection"}, False),
    ({"sender": "harvey"}, False),
    ({"stage": "closed_won"}, False),
])
def test_is_hot(kwargs, hot):
    assert is_hot(_convo(**kwargs)) is hot


@pytest.mark.parametrize("kwargs,stalled", [
    ({"sender": "harvey", "age": STALL_AFTER}, True),
    ({"sender": "harvey", "age": STALL_AFTER - timedelta(hours=1)}, False),
    ({"sender": "prospect", "age": STALL_AFTER}, False),
    ({"sender": "harvey", "age": STALL_AFTER, "stage": "initial_outreach"}, False),
    ({"sender": "harvey", "age": STALL_AFTER, "stage": "closed_lost"}, False),
])
def test_is_stalled(kwargs, stalled):
    assert is_stalled(_convo(**kwargs), now=NOW) is stalled


def test_forecast_separates_won_open_and_weighted():
    conversations = [
        _convo(cid="a", stage="closed_won"),
        _convo(cid="b", stage="closing"),
        _convo(cid="c", stage="closed_lost"),
        _convo(cid="d", stage="engaged"),
    ]
    report = forecast(conversations, {"a": 100, "b": 1000, "c": 500})
    assert report == {"won": 100, "open": 1000, "weighted": 700}


def test_health_needs_a_running_loop_and_a_recent_cycle():
    recent, old = NOW - timedelta(hours=1), NOW - timedelta(days=1)
    assert assess_health(True, recent, now=NOW).healthy
    assert not assess_health(False, recent, now=NOW).healthy
    assert not assess_health(True, old, now=NOW).healthy
    assert not assess_health(True, None, now=NOW).healthy


@pytest.mark.asyncio
async def test_last_action_reads_only_heartbeat_cycles(state):
    assert await state.last_action_at() is None
    await state.log_action("send_email", agent="sender")
    assert await state.last_action_at() is None
    await state.log_action("prospect", agent="main")
    assert await state.last_action_at() is not None


@pytest.mark.asyncio
async def test_alert_fires_once_and_carries_no_message_text(state, stored):
    posts = []

    def handler(request):
        posts.append(request.content.decode())
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    assert await send_hot_alerts(state, WEBHOOK, transport) == 1
    assert await send_hot_alerts(state, WEBHOOK, transport) == 0
    assert "Sam Lee" in posts[0] and "hi" not in posts[0].replace("hot", "")


@pytest.mark.asyncio
async def test_failed_alert_retries_next_cycle(state, stored):
    failing = httpx.MockTransport(lambda request: httpx.Response(500))
    working = httpx.MockTransport(lambda request: httpx.Response(200))
    assert await send_hot_alerts(state, WEBHOOK, failing) == 0
    assert await send_hot_alerts(state, WEBHOOK, working) == 1


@pytest.mark.asyncio
async def test_plain_http_webhook_is_refused(state, stored):
    def handler(request):
        raise AssertionError("must not post")

    assert await send_hot_alerts(state, "http://alerts.example.com", httpx.MockTransport(handler)) == 0


@pytest.mark.asyncio
async def test_closing_a_deal_updates_conversation_prospect_and_value(state, stored):
    await close_deal(state, stored, won=True, value=2500)
    convo = await state.get_conversation(stored.id)
    assert (convo.stage, convo.status) == ("closed_won", "closed")
    assert (await state.get_prospect(stored.prospect_id)).status == "closed"
    assert await state.get_setting(f"deal_value:{stored.id}") == "2500"


@pytest.mark.asyncio
async def test_negative_value_is_refused(state, stored):
    with pytest.raises(ValueError):
        await close_deal(state, stored, won=False, value=-1)
