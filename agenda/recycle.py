"""Ophaaldagen uit recycleapp.be (Fost Plus), zoals de app en de Home Assistant-integraties ze ophalen.

Stappen: het geheim uit de JavaScript van www.recycleapp.be halen, daarmee een toegangstoken vragen, dan postcode
en straat opzoeken en de ophalingen voor een periode vragen. De API is niet officieel gedocumenteerd: bij een fout
blijft de agenda zoals hij was en staat de reden bij de status.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx

SITE = "https://www.recycleapp.be"
API = "https://api.fostplus.be/recycle-public/app/v1"
CONSUMER = "recycleapp.be"
TIMEOUT = 20.0
UA = "agenda.jbogaert.be/2.0"

_SCRIPT = re.compile(r'src="(/?(?:static/js/)?main[^"]*\.js)"')
_SECRET = re.compile(r'''secret\s*:\s*["']([A-Za-z0-9_\-]{20,})["']''')


class RecycleError(Exception):
    """Wat er misging, in gewone taal."""


@dataclass(frozen=True)
class Pickup:
    day: date
    fraction: str
    color: str | None

    @property
    def external_id(self) -> str:
        return f"recycle:{self.day.isoformat()}:{self.fraction.lower()}"


class Client:
    def __init__(self, http: httpx.Client | None = None, secret: str | None = None):
        self.http = http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True)
        self.secret = secret
        self.token: str | None = None

    def _get(self, url: str, **kw) -> httpx.Response:
        try:
            r = self.http.get(url, **kw)
        except httpx.HTTPError as e:
            raise RecycleError(f"recycleapp.be niet bereikbaar ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise RecycleError(f"recycleapp.be gaf HTTP {r.status_code} op {url.split('?')[0].replace(API, '')}")
        return r

    def find_secret(self) -> str:
        if self.secret:
            return self.secret
        page = self._get(SITE + "/").text
        m = _SCRIPT.search(page)
        if not m:
            raise RecycleError("Kon het script van recycleapp.be niet vinden (site veranderd?)")
        src = m.group(1)
        js = self._get(SITE + (src if src.startswith("/") else "/" + src)).text
        s = _SECRET.search(js)
        if not s:
            raise RecycleError("Geen geheim in het script van recycleapp.be (site veranderd?): zet RECYCLE_SECRET")
        self.secret = s.group(1)
        return self.secret

    def login(self) -> str:
        r = self._get(API + "/access-token", headers={"x-secret": self.find_secret(), "x-consumer": CONSUMER})
        token = (r.json() or {}).get("accessToken")
        if not token:
            raise RecycleError("Geen toegangstoken van recycleapp.be")
        self.token = token
        return token

    def api(self, path: str, params: dict) -> dict:
        if not self.token:
            self.login()
        headers = {"Authorization": self.token, "x-consumer": CONSUMER}
        try:
            r = self.http.get(API + path, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise RecycleError(f"recycleapp.be niet bereikbaar ({type(e).__name__})") from e
        if r.status_code == 401:  # token verlopen: één keer opnieuw
            headers["Authorization"] = self.login()
            r = self._get(API + path, params=params, headers=headers)
        elif r.status_code >= 400:
            raise RecycleError(f"recycleapp.be gaf HTTP {r.status_code} op {path}")
        return r.json() or {}

    def zipcode_id(self, postcode: str) -> str:
        items = self.api("/zipcodes", {"q": postcode}).get("items") or []
        for it in items:
            if str(it.get("code")) == str(postcode):
                return it["id"]
        if items:
            return items[0]["id"]
        raise RecycleError(f"Postcode {postcode} niet gevonden bij recycleapp.be")

    def street_id(self, zipcode_id: str, street: str) -> str:
        items = self.api("/streets", {"q": street, "zipcodes": zipcode_id}).get("items") or []
        want = _norm(street)
        for it in items:
            names = [it.get("name")] + list((it.get("names") or {}).values())
            if any(_norm(n or "") == want for n in names):
                return it["id"]
        if items:
            return items[0]["id"]
        raise RecycleError(f"Straat '{street}' niet gevonden bij recycleapp.be")

    def pickups(self, postcode: str, street: str, number: str, start: date, end: date) -> list[Pickup]:
        zid = self.zipcode_id(postcode)
        sid = self.street_id(zid, street)
        data = self.api("/collections", {"zipcodeId": zid, "streetId": sid, "houseNumber": number,
                                         "fromDate": start.isoformat(), "untilDate": end.isoformat(), "size": 200})
        return parse_collections(data)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _name(fraction: dict) -> str:
    n = fraction.get("name") or {}
    if isinstance(n, dict):
        return n.get("nl") or n.get("fr") or n.get("en") or next(iter(n.values()), "afval")
    return str(n or "afval")


def parse_collections(data: dict) -> list[Pickup]:
    """Ophalingen uit het antwoord van /collections. Verplaatste ophalingen (exception.replacedBy) vallen weg: de
    vervanging staat er zelf ook in. Geannuleerde (exception zonder vervanging, of 'cancelled') ook."""
    out: set[Pickup] = set()
    for it in data.get("items") or []:
        if it.get("type") not in (None, "collection"):
            continue  # bv. "event": een activiteit van de gemeente, geen ophaling
        exc = it.get("exception") or {}
        if exc.get("replacedBy") or exc.get("cancelled") or it.get("cancelled"):
            continue
        ts = str(it.get("timestamp") or "")
        try:
            day = datetime.fromisoformat(ts.replace("Z", "+00:00")).date() if "T" in ts else date.fromisoformat(ts[:10])
        except ValueError:
            continue
        # De API geeft middernacht in UTC of in Brussel; de datum zelf telt.
        if "T" in ts and ts[11:13] in ("22", "23"):
            day = date.fromordinal(day.toordinal() + 1)
        fr = it.get("fraction") or {}
        out.add(Pickup(day, _name(fr).strip(), fr.get("color")))
    return sorted(out, key=lambda p: (p.day, p.fraction.lower()))
