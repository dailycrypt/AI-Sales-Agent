"""Read-only sales insights: explainable lead scores, reply analytics, signal changes.

Nothing here writes to the database or calls a model. `Prospect.score` stays the
scout's fit score; `score_lead` is a separate priority with its reasons shown.
"""

from collections import Counter

from harvey.models.conversation import STAGES, Conversation
from harvey.models.prospect import Prospect

EMAIL_POINTS = {"verified": 25, "risky": 10, "guess": -10, "invalid": -30}
SENIORITY_POINTS = {"c_suite": 25, "vp": 20, "director": 15, "manager": 8}
STATUS_POINTS = {"replied": 20, "meeting": 30}
SIGNAL_POINTS = 5
MAX_SIGNAL_POINTS = 20
PERSONALIZATION_POINTS = 10
INBOX_PENALTY = -10
OPEN_PROSPECT_STATUSES = ("new", "contacted", "replied", "meeting")
CONVERSATION_STATUSES = ("open", "replied", "meeting_booked", "closed")
# ponytail: newest rows only, raise if a database outgrows it and changes go missing
OBSERVATION_SCAN_LIMIT = 20000


def _is_positive(observation: dict) -> bool:
    """A boolean signal is stored either way; 0 means "checked, not there"."""
    if observation.get("value_num") is not None:
        return bool(observation["value_num"])
    return bool(observation.get("value_text"))


def score_lead(prospect: Prospect, positive_signals: int = 0) -> tuple[int, list[str]]:
    """Priority from 0 to 100, plus each reason that moved it."""
    parts = []
    email_points = EMAIL_POINTS.get(prospect.email_status, 0)
    if email_points:
        parts.append((email_points, f"email {prospect.email_status}"))
    seniority_points = SENIORITY_POINTS.get(prospect.seniority, 0)
    if seniority_points:
        parts.append((seniority_points, f"seniority {prospect.seniority}"))
    if positive_signals:
        signal_points = min(positive_signals * SIGNAL_POINTS, MAX_SIGNAL_POINTS)
        parts.append((signal_points, f"{positive_signals} positive signal(s)"))
    if prospect.personalization_notes:
        parts.append((PERSONALIZATION_POINTS, "personalization notes"))
    status_points = STATUS_POINTS.get(prospect.status, 0)
    if status_points:
        parts.append((status_points, f"status {prospect.status}"))
    if prospect.is_company_inbox:
        parts.append((INBOX_PENALTY, "general inbox, no named person"))
    score = max(0, min(100, sum(points for points, _ in parts)))
    return score, [f"{points:+d} {reason}" for points, reason in parts]


def reply_analytics(conversations: list[Conversation]) -> dict:
    """Funnel and reply outcomes. Reply rate counts threads the prospect wrote in."""
    replied = [
        c for c in conversations if any(m.sender == "prospect" for m in c.thread)
    ]
    intents = Counter(c.intent for c in replied if c.intent)
    positive = intents["interested"]
    stages = Counter(c.stage for c in conversations)
    return {
        "conversations": len(conversations),
        "replied": len(replied),
        "reply_rate": round(len(replied) / len(conversations), 3) if conversations else 0.0,
        "positive_rate": round(positive / len(replied), 3) if replied else 0.0,
        "intents": dict(intents.most_common()),
        "stages": {stage: stages[stage] for stage in STAGES if stages[stage]},
        "meetings": sum(1 for c in conversations if c.status == "meeting_booked"),
    }


def signal_changes(observations: list[dict]) -> list[dict]:
    """Signals whose two newest readings differ. Input is newest first."""
    newest, changes = {}, []
    seen_twice = set()
    for row in observations:
        key = (row.get("company_id", ""), row.get("signal_code", ""))
        if key in seen_twice:
            continue
        if key not in newest:
            newest[key] = row
            continue
        seen_twice.add(key)
        latest = newest[key]
        before = (row.get("value_num"), row.get("value_text") or "")
        after = (latest.get("value_num"), latest.get("value_text") or "")
        if before != after:
            changes.append({
                "company_id": key[0],
                "signal_code": key[1],
                "before": row.get("value_text") or row.get("value_num"),
                "after": latest.get("value_text") or latest.get("value_num"),
                "became_positive": _is_positive(latest) and not _is_positive(row),
                "observed_at": latest.get("observed_at"),
                "evidence_url": latest.get("evidence_url", ""),
            })
    return changes


async def ranked_leads(state, limit: int = 25) -> list[dict]:
    """Open prospects, highest priority first."""
    observations = await state.get_observations(latest_only=True, limit=OBSERVATION_SCAN_LIMIT)
    positives = Counter(o["company_id"] for o in observations if _is_positive(o))
    rows = []
    for status in OPEN_PROSPECT_STATUSES:
        for prospect in await state.get_prospects_by_status(status):
            score, reasons = score_lead(prospect, positives[prospect.company_id])
            rows.append({"prospect": prospect, "score": score, "reasons": reasons})
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:limit]


async def load_analytics(state) -> dict:
    conversations = []
    for status in CONVERSATION_STATUSES:
        conversations += await state.get_conversations_by_status(status)
    return reply_analytics(conversations)


async def load_signal_changes(state) -> list[dict]:
    return signal_changes(await state.get_observations(limit=OBSERVATION_SCAN_LIMIT))
