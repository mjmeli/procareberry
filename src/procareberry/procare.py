"""Parent-facing Procare JSON client. No rendered-page scraping or media downloads."""

import asyncio
import calendar
from datetime import date, timedelta
from urllib.parse import urlsplit

import aiohttp

from .models import SyncError
from .storage import private_json, read_json

AUTH_URL = "https://online-auth.procareconnect.com/sessions/"


def api_host(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise SyncError("Procare returned an invalid school API URL")
    if not (host.endswith(".procareconnect.com") or host.endswith(".kinderlime.com")):
        raise SyncError("Procare returned an unexpected school API host")
    if parsed.path.rstrip("/") not in ("", "/api/web"):
        raise SyncError("Procare returned an unexpected school API path")
    return f"https://{host}/api/web/"


class AiohttpTransport:
    def __init__(self, session):
        self.session = session

    async def request(self, method, url, *, headers=None, payload=None, params=None):
        try:
            async with self.session.request(
                method,
                url,
                headers=headers,
                json=payload,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=False,
            ) as response:
                retry_after = response.headers.get("Retry-After", "")
                if response.status not in (200, 201):
                    return response.status, {}, retry_after
                return response.status, await response.json(), retry_after
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise SyncError("Procare request failed or returned invalid JSON") from exc


class ProcareClient:
    def __init__(self, transport, email, password, token_file, *, sleep=asyncio.sleep):
        self.transport, self.email, self.password = transport, email, password
        self.token_file, self.sleep = token_file, sleep
        saved = read_json(token_file)
        self.token = saved.get("token") if saved.get("email") == email else None
        self.base = api_host(saved["base"]) if self.token and saved.get("base") else None

    async def _send(self, method, url, **kwargs):
        for attempt in range(3):
            try:
                status, payload, retry_after = await self.transport.request(method, url, **kwargs)
            except SyncError:
                if attempt == 2:
                    raise
                await self.sleep(2**attempt)
                continue
            if status == 429 or status >= 500:
                if attempt == 2:
                    raise SyncError(
                        "Procare is rate-limiting requests or temporarily unavailable; retry later"
                    )
                try:
                    delay = min(max(float(retry_after), 1), 60)
                except (ValueError, TypeError):
                    delay = 2**attempt
                await self.sleep(delay)
                continue
            return status, payload
        raise SyncError("Procare request retry limit reached")

    async def login(self):
        status, raw = await self._send(
            "POST",
            AUTH_URL,
            payload={
                "email": self.email,
                "password": self.password,
                "role": "carer",
                "platform": "web",
                "preserve_sites": True,
            },
        )
        if status in (401, 403, 422):
            raise SyncError(
                "Procare rejected email/password authentication; check credentials and verification requirements"
            )
        if status not in (200, 201) or not isinstance(raw, dict):
            raise SyncError("Unexpected Procare authentication response")
        data = raw.get("data", raw)
        if not isinstance(data, dict):
            raise SyncError("Unexpected Procare authentication response")
        token = data.get("auth_token") or (data.get("user") or {}).get("auth_token")
        if not token or data.get("access_to", "regular_requests") != "regular_requests":
            raise SyncError(
                "Procare requires verification or SSO; this release supports email/password sessions only"
            )
        sites = data.get("sites")
        if not isinstance(sites, list) or not sites or not all(isinstance(s, dict) for s in sites):
            raise SyncError("Procare did not return a school API host")
        default = [s for s in sites if s.get("is_default")]
        if len(default) > 1 or (not default and len(sites) > 1):
            raise SyncError(
                "Procare has multiple sites without one default; select a default school in Procare first"
            )
        self.base = api_host((default or sites)[0].get("base_url", ""))
        self.token = token
        private_json(self.token_file, {"email": self.email, "token": token, "base": self.base})

    async def get(self, path, params=None):
        if not self.token or not self.base:
            await self.login()
        for attempt in range(2):
            status, data = await self._send(
                "GET",
                self.base + path,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                params=params,
            )
            if status in (401, 403) and not attempt:
                await self.login()
                continue
            if status != 200 or not isinstance(data, dict):
                raise SyncError(
                    f"Procare parent API returned HTTP {status}; no incomplete feed will be imported"
                )
            return data
        raise SyncError("Procare session could not be refreshed")

    async def children(self):
        data = await self.get("parent/kids/")
        kids = data.get("kids")
        if not isinstance(kids, list) or not all(isinstance(k, dict) and k.get("id") for k in kids):
            raise SyncError("Procare returned an invalid child list")
        return kids

    async def activities(self, kid_id: str, start: date, end: date):
        records, seen = [], set()
        cursor = start
        while cursor <= end:
            last = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
            boundary = min(last, end)
            for record in await self._pages(kid_id, cursor, boundary):
                identifier = record.get("id")
                if identifier is None or str(identifier) not in seen:
                    records.append(record)
                if identifier is not None:
                    seen.add(str(identifier))
            cursor = boundary + timedelta(days=1)
        return records

    async def _pages(self, kid_id: str, start: date, end: date):
        records, ids, previous = [], set(), None
        for page in range(1, 1001):
            data = await self.get(
                "parent/daily_activities/",
                {
                    "kid_id": kid_id,
                    "filters[daily_activity][date_from]": start.isoformat(),
                    "filters[daily_activity][date_to]": end.isoformat(),
                    "page": page,
                },
            )
            batch = data.get("daily_activities")
            if not isinstance(batch, list) or not all(isinstance(r, dict) for r in batch):
                raise SyncError("Procare returned an invalid activity page")
            if not batch:
                return records
            page_ids = tuple(str(r.get("id", "")) for r in batch)
            if page_ids == previous:
                raise SyncError("Procare repeated an activity page; refusing an incomplete feed")
            previous = page_ids
            for record in batch:
                # Preserve invalid/no-ID records so the mapper can report them.
                identifier = record.get("id")
                if identifier is None or str(identifier) not in ids:
                    records.append(record)
                if identifier is not None:
                    ids.add(str(identifier))
            await self.sleep(0.3)
        raise SyncError("Procare pagination exceeded its limit; narrow the requested date range")
