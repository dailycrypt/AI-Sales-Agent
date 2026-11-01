"""Update check, Cal.com calendar, lead scores, reply analytics, signal changes."""

import json

import httpx
import pytest

from harvey.insights import reply_analytics, score_lead, signal_changes
from harvey.integrations.calendar import BOOKINGS_API_VERSION, CalendarClient
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from harvey.updates import UpdateError, apply_update, check_for_update, latest_tag

LS_REMOTE = (
    "aaa\trefs/tags/v0.9.0\n"
    "bbb\trefs/tags/v0.10.0\n"
    "ccc\trefs/tags/v0.10.0^{}\n"
    "ddd\trefs/tags/nightly\n"
)


class FakeGit:
    """Records commands; a command starting with a word in `fails` errors."""

    def __init__(self, fails=(), status=""):
        self.calls, self.fails, self.status = [], fails, status

    def __call__(self, *args):
        self.calls.append(args[0])
        if args[0] in self.fails:
            raise UpdateError(f"git {args[0]} failed")
        return self.status if args[0] == "status" else ""


# ── updates ──


def test_latest_tag_compares_numerically_and_ignores_other_tags():
    assert latest_tag(LS_REMOTE) == "v0.10.0"
    assert latest_tag("") is None


def test_check_reports_newer_and_current_versions():
    git = lambda *a: LS_REMOTE
    assert check_for_update(git, current="0.9.5").available
    assert not check_for_update(git, current="0.10.0").available
    assert not check_for_update(lambda *a: "", current="0.3.0").available


def test_apply_merges_a_verified_tag():
    git = FakeGit()
    apply_update("v1.0.0", git=git, loop_running=lambda: False)
    assert git.calls == ["status", "fetch", "verify-tag", "merge"]


def test_unsigned_tag_is_never_merged():
    git = FakeGit(fails=("verify-tag",))
    with pytest.raises(UpdateError, match="no signature"):
        apply_update("v1.0.0", git=git, loop_running=lambda: False)
    assert "merge" not in git.calls


@pytest.mark.parametrize("tag,running,status,message", [
    ("v1.0.0", True, "", "running"),
    ("v1.0.0", False, " M harvey/cli.py", "uncommitted"),
    ("--upload-pack=x", False, "", "Not a release tag"),
])
def test_apply_refuses_before_touching_anything(tag, running, status, message):
    git = FakeGit(status=status)
    with pytest.raises(UpdateError, match=message):
        apply_update(tag, git=git, loop_running=lambda: running)
    assert "fetch" not in git.calls and "merge" not in git.calls


# ── calendar ──


def _client(handler):
    return CalendarClient("key", "10", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_slots_are_flattened_across_days():
    def handler(request):
        assert request.url.params["eventTypeId"] == "10"
        return httpx.Response(200, json={"data": {
            "2033-09-05": [{"start": "a"}], "2033-09-06": [{"start": "b"}]}})

    slots = await _client(handler).get_available_slots("2033-09-05", "2033-09-06")
    assert [s["start"] for s in slots] == ["a", "b"]


@pytest.mark.asyncio
async def test_booking_sends_attendee_and_version_header():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["version"] = request.headers["cal-api-version"]
        return httpx.Response(201, json={"status": "success"})

    assert await _client(handler).book_meeting("sam@example.com", {"start": "2033-09-05T09:00:00Z"})
    assert seen["version"] == BOOKINGS_API_VERSION
    assert seen["body"]["eventTypeId"] == 10
    assert seen["body"]["attendee"]["email"] == "sam@example.com"


@pytest.mark.asyncio
async def test_calendar_failures_degrade_instead_of_raising():
    failing = _client(lambda request: httpx.Response(500))
    assert await failing.get_available_slots("2033-09-05", "2033-09-06") is None
    assert not await failing.book_meeting("sam@example.com", {"start": "x"})
    unconfigured = CalendarClient()
    assert await unconfigured.get_available_slots("a", "b") == []
    assert not await unconfigured.book_meeting("sam@example.com", {"start": "x"})
    assert not await failing.book_meeting("", {"start": "x"})


# ── scores ──


def test_score_explains_itself():
    prospect = Prospect(email_status="verified", seniority="c_suite", status="replied")
    score, reasons = score_lead(prospect, positive_signals=2)
    assert score == 25 + 25 + 10 + 20
    assert "+25 email verified" in reasons and "+10 2 positive signal(s)" in reasons


def test_score_stays_in_range_and_caps_signals():
    assert score_lead(Prospect(email_status="invalid"))[0] == 0
    full = Prospect(email_status="verified", seniority="c_suite", status="meeting",
                    personalization_notes="note")
    assert score_lead(full, positive_signals=50)[0] == 100
    assert score_lead(Prospect(), positive_signals=50)[0] == 20


def test_company_inbox_scores_below_a_person():
    inbox = Prospect(email_status="verified", source=COMPANY_INBOX_SOURCE)
    assert score_lead(inbox)[0] < score_lead(Prospect(email_status="verified"))[0]


# ── analytics ──


def _convo(cid, replied=False, intent="", **kwargs):
    thread = [Message(sender="prospect", content="hi")] if replied else []
    return Conversation(id=cid, prospect_id="p", thread=thread, intent=intent, **kwargs)


def test_reply_analytics_counts_rates_and_stages():
    report = reply_analytics([
        _convo("1"),
        _convo("2", replied=True, intent="interested", stage="closing", status="meeting_booked"),
        _convo("3", replied=True, intent="objection"),
        _convo("4", intent="ooo"),
    ])
    assert report["reply_rate"] == 0.5 and report["positive_rate"] == 0.5
    assert report["intents"] == {"interested": 1, "objection": 1}
    assert report["meetings"] == 1
    assert report["stages"]["closing"] == 1


def test_reply_analytics_handles_no_data():
    assert reply_analytics([])["reply_rate"] == 0.0


# ── signal changes ──


def _obs(company, code, num=None, text=""):
    return {"company_id": company, "signal_code": code, "value_num": num, "value_text": text}


def test_signal_flip_is_a_change_and_older_history_is_ignored():
    changes = signal_changes([
        _obs("c1", "HIRING", 1), _obs("c1", "HIRING", 0), _obs("c1", "HIRING", 1),
        _obs("c2", "ADS", 1), _obs("c2", "ADS", 1),
        _obs("c3", "NEW", 1),
    ])
    assert [(c["company_id"], c["became_positive"]) for c in changes] == [("c1", True)]


def test_lost_signal_is_not_positive():
    assert not signal_changes([_obs("c", "S", 0), _obs("c", "S", 1)])[0]["became_positive"]


def test_text_change_is_detected():
    assert signal_changes([_obs("c", "CMS", text="B"), _obs("c", "CMS", text="A")])[0]["after"] == "B"
