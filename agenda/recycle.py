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

_SCRIPTS = re.compile(r'''<script[^>]+src=["']([^"']+\.js)["']''', re.I)
_TOKEN = r"([A-Za-z0-9_\-+/=]{20,})"
_SECRETS = [re.compile(p, re.I) for p in (
    r'''\bsecret["']?\s*[:=]\s*["'`]''' + _TOKEN + r"[\"'`]",
    r'''["']x-secret["']\s*[:,]\s*["'`]''' + _TOKEN + r"[\"'`]",
    r'''\b[A-Z_]*SECRET[A-Z_]*["']?\s*[:=]\s*["'`]''' + _TOKEN + r"[\"'`]",
)]


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
        self.candidates: list[str] = []
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
        """Het geheim staat in de JavaScript van de site. Eerst main*.js, dan de andere scripts (hoogstens 8)."""
        if self.secret:
            return self.secret
        page = self._get(SITE + "/").text
        srcs = _SCRIPTS.findall(page)
        if not srcs:
            raise RecycleError("Kon geen script van recycleapp.be vinden (site veranderd?): zet RECYCLE_SECRET")
        srcs.sort(key=lambda x: (0 if "main" in x.rsplit("/", 1)[-1] else 1))
        for src in srcs[:8]:
            url = src if src.startswith("http") else SITE + (src if src.startswith("/") else "/" + src)
            if not url.startswith(SITE + "/"):
                continue
            js = self._get(url).text
            for pat in _SECRETS:
                for m in pat.finditer(js):
                    if m.group(1) not in self.candidates:
                        self.candidates.append(m.group(1))
            if self.candidates:
                return self.candidates[0]
        raise RecycleError("Geen geheim in de scripts van recycleapp.be (site veranderd?): zet RECYCLE_SECRET")

    def login(self) -> str:
        first = self.find_secret()
        tries = [first] + [c for c in self.candidates if c != first][:5]
        last = None
        for secret in tries:
            try:
                r = self._get(API + "/access-token", headers={"x-secret": secret, "x-consumer": CONSUMER})
            except RecycleError as e:  # verkeerde kandidaat: de volgende proberen
                last = e
                continue
            token = (r.json() or {}).get("accessToken")
            if token:
                self.secret, self.token = secret, token
                return token
        raise last or RecycleError("Geen toegangstoken van recycleapp.be")

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
        if not items:
            # "Burg. van Liefferingestraat": afkortingen vindt de zoekfunctie niet; dan op het langste woord.
            word = max(re.findall(r"[A-Za-zÀ-ÿ]{4,}", street) or [street], key=len)
            if word != street:
                items = self.api("/streets", {"q": word, "zipcodes": zipcode_id}).get("items") or []
        want = _norm(street)
        for it in items:
            names = [it.get("name")] + list((it.get("names") or {}).values())
            if any(_norm(n or "") == want for n in names):
                return it["id"]
        tail = _norm(max(re.findall(r"[A-Za-zÀ-ÿ]{4,}", street) or [street], key=len))
        for it in items:
            if tail and tail in _norm(it.get("name") or ""):
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
