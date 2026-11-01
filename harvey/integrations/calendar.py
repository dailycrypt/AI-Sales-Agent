"""Calendar integration: Cal.com availability and booking (API v2).

Set CAL_API_KEY and CAL_EVENT_TYPE_ID in .env. Without them every method
degrades gracefully (empty results / False) so callers can invoke them
unconditionally. Booking runs only when something calls book_meeting; the
agents don't, so a person still decides who gets a meeting.
"""

import logging

import httpx

logger = logging.getLogger("harvey.calendar")

API_BASE = "https://api.cal.com/v2"
SLOTS_API_VERSION = "2024-09-04"
BOOKINGS_API_VERSION = "2026-02-25"
TIMEOUT_SECONDS = 20


class CalendarClient:
    def __init__(self, api_key: str = "", event_type_id: str = "", transport=None):
        self.api_key = api_key
        self.event_type_id = event_type_id
        self._transport = transport  # tests inject httpx.MockTransport

    @property
    def configured(self) -> bool:
        return bool(self.api_key and str(self.event_type_id).isdigit())

    def _client(self, api_version: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            timeout=TIMEOUT_SECONDS,
            transport=self._transport,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "cal-api-version": api_version,
            },
        )

    async def get_available_slots(
        self, start: str, end: str, time_zone: str = "UTC"
    ) -> list[dict]:
        """Open slots between two dates (YYYY-MM-DD), flattened and in API order."""
        if not self.configured:
            logger.info("Calendar not configured (CAL_API_KEY, CAL_EVENT_TYPE_ID).")
            return []
        params = {
            "eventTypeId": self.event_type_id,
            "start": start,
            "end": end,
            "timeZone": time_zone,
        }
        try:
            async with self._client(SLOTS_API_VERSION) as client:
                response = await client.get("/slots", params=params)
                response.raise_for_status()
                days = response.json().get("data") or {}
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"Could not read calendar slots: {e}")
            return []
        return [slot for day in days.values() for slot in day]

    async def book_meeting(
        self, prospect_email: str, slot: dict, name: str = "", time_zone: str = "UTC"
    ) -> bool:
        """Book a slot. `slot["start"]` is an ISO timestamp from get_available_slots."""
        start = slot.get("start") if isinstance(slot, dict) else None
        if not prospect_email or not start:
            logger.warning("book_meeting called with invalid arguments.")
            return False
        if not self.configured:
            logger.info("Calendar not configured (CAL_API_KEY, CAL_EVENT_TYPE_ID).")
            return False
        body = {
            "start": start,
            "eventTypeId": int(self.event_type_id),
            "attendee": {
                "name": name or prospect_email,
                "email": prospect_email,
                "timeZone": time_zone,
            },
        }
        try:
            async with self._client(BOOKINGS_API_VERSION) as client:
                response = await client.post("/bookings", json=body)
                response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning(f"Could not book the meeting: {e}")
            return False
        return True
