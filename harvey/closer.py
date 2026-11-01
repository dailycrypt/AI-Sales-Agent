"""After the first reply: hot-lead alerts, stalled deals, forecast, loop health.

Sending stays with the Outbox. This module only tells a person where a deal
needs them and records how it ended. Alerts carry a name, company and intent,
never message text: replies are untrusted and needn't leave this machine.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from harvey.models.conversation import Conversation
from harvey.paths import PROJECT_ROOT
from harvey.pidfile import loop_lock_held

logger = logging.getLogger("harvey.closer")

HOT_INTENTS = ("interested", "escalate")
STALL_AFTER = timedelta(days=5)
# Quiet hours log no actions, so this must outlast a night.
HEALTH_STALE_AFTER = timedelta(hours=12)
ALERT_TIMEOUT_SECONDS = 10
OPEN_STATUSES = ("open", "replied", "meeting_booked")
CLOSED_STAGES = ("closed_won", "closed_lost")
# Starting guesses, not measurements. Tune them against your own closed deals.
STAGE_WIN_ODDS = {
    "initial_outreach": 0.0,
    "engaged": 0.1,
    "qualifying": 0.2,
    "presenting": 0.35,
    "negotiating": 0.5,
    "closing": 0.7,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_hot(convo: Conversation) -> bool:
    """The prospect spoke last and wants something from a person."""
    return (
        bool(convo.thread)
        and convo.thread[-1].sender == "prospect"
        and convo.intent in HOT_INTENTS
        and convo.stage not in CLOSED_STAGES
    )


def is_stalled(convo: Conversation, now: datetime | None = None) -> bool:
    """A prospect engaged, we spoke last, and they've gone quiet."""
    if not convo.thread or convo.stage in CLOSED_STAGES or convo.stage == "initial_outreach":
        return False
    last = convo.thread[-1]
    return last.sender != "prospect" and (now or _utcnow()) - last.timestamp >= STALL_AFTER


def forecast(conversations: list[Conversation], values: dict[str, float]) -> dict:
    """Won revenue, open pipeline, and pipeline weighted by stage odds."""
    won = open_value = weighted = 0.0
    for convo in conversations:
        value = values.get(convo.id, 0.0)
        if convo.stage == "closed_won":
            won += value
        elif convo.stage != "closed_lost":
            open_value += value
            weighted += value * STAGE_WIN_ODDS.get(convo.stage, 0.0)
    return {"won": won, "open": open_value, "weighted": round(weighted, 2)}


@dataclass(frozen=True)
class Health:
    running: bool
    last_cycle: datetime | None
    healthy: bool


def assess_health(running: bool, last_cycle: datetime | None, now: datetime | None = None) -> Health:
    fresh = last_cycle is not None and (now or _utcnow()) - last_cycle < HEALTH_STALE_AFTER
    return Health(running, last_cycle, running and fresh)


async def loop_health(state) -> Health:
    return assess_health(loop_lock_held(), await state.last_action_at())


async def active_conversations(state) -> list[Conversation]:
    conversations = []
    for status in OPEN_STATUSES:
        conversations += await state.get_conversations_by_status(status)
    return conversations


async def deal_values(state, conversations: list[Conversation]) -> dict[str, float]:
    values = {}
    for convo in conversations:
        raw = await state.get_setting(f"deal_value:{convo.id}", "")
        if raw:
            values[convo.id] = float(raw)
    return values


async def close_deal(state, convo: Conversation, won: bool, value: float | None = None) -> None:
    if value is not None:
        if value < 0:
            raise ValueError("Deal value can't be negative.")
        await state.set_setting(f"deal_value:{convo.id}", str(value))
    await state.update_conversation(
        convo.id, stage="closed_won" if won else "closed_lost", status="closed")
    await state.update_prospect_status(convo.prospect_id, "closed" if won else "lost")


async def send_hot_alerts(state, webhook_url: str = "", transport=None) -> int:
    """Post each new hot reply once. Unsent alerts retry next cycle."""
    if not webhook_url:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
        webhook_url = os.environ.get("HARVEY_ALERT_WEBHOOK", "")
    if not webhook_url:
        return 0
    if not webhook_url.startswith("https://"):
        logger.warning("HARVEY_ALERT_WEBHOOK must use https; no alert sent.")
        return 0

    sent = 0
    async with httpx.AsyncClient(timeout=ALERT_TIMEOUT_SECONDS, transport=transport) as client:
        for convo in await active_conversations(state):
            key = f"alerted:{convo.id}"
            mark = str(len(convo.thread))
            if not is_hot(convo) or await state.get_setting(key, "") == mark:
                continue
            prospect = await state.get_prospect(convo.prospect_id)
            who = f"{prospect.full_name()}, {prospect.company}" if prospect else convo.prospect_id
            text = f"Hot reply ({convo.intent}): {who}. Conversation {convo.id}."
            try:
                # `text` is read by Slack, `content` by Discord.
                response = await client.post(webhook_url, json={"text": text, "content": text})
                response.raise_for_status()
            except httpx.HTTPError as e:
                # The exception text can contain the URL, which is a secret.
                logger.warning(f"Hot-lead alert failed: {type(e).__name__}")
                continue
            await state.set_setting(key, mark)
            sent += 1
    return sent
