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
API_PUBLIC = "https://api.fostplus.be/recycle-public/public/v1"
CONSUMER = "recycleapp.be"
TIMEOUT = 20.0
UA = "agenda.jbogaert.be/2.0"

_SCRIPTS = re.compile(r'''<script[^>]+src=["']([^"']+\.js)["']''', re.I)
_API_URL = re.compile(r'''["'`](https://[a-z0-9.\-]*(?:fostplus|recycleapp)[a-z0-9.\-]*\.be/[A-Za-z0-9_\-/]*?)["'`]''')
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
    """Ontdekt zelf hoe de site de API aanspreekt: het adres uit window.globalConfig (inline in de pagina) plus
    /public/v1 of /app/v1, met of zonder toegangstoken (het geheim uit de scripts). Wat werkt, wordt onthouden."""

    def __init__(self, http: httpx.Client | None = None, secret: str | None = None):
        self.http = http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=True)
        self.secret = secret
        self.candidates: list[str] = [secret] if secret else []
        self.bases: list[str] = []
        self.base: str | None = None
        self.token: str | None = None
        self.discovered = False
        self.last = ""

    def _get(self, url: str, **kw) -> httpx.Response:
        try:
            r = self.http.get(url, **kw)
        except httpx.HTTPError as e:
            raise RecycleError(f"recycleapp.be niet bereikbaar ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise RecycleError(f"recycleapp.be gaf HTTP {r.status_code} op {_short(url)}")
        return r

    def discover(self) -> None:
        """Leest de pagina en de scripts: API-adres(sen) en kandidaat-geheimen."""
        if self.discovered:
            return
        self.discovered = True
        try:
            page = self._get(SITE + "/").text
        except RecycleError:
            page = ""  # site even weg: dan de bekende adressen proberen
        texts = [page]
        srcs = _SCRIPTS.findall(page)
        srcs.sort(key=lambda x: (0 if "main" in x.rsplit("/", 1)[-1] else 1))
        for src in srcs[:6]:
            url = src if src.startswith("http") else SITE + (src if src.startswith("/") else "/" + src)
            if url.startswith(SITE + "/"):
                try:
                    texts.append(self._get(url).text)
                except RecycleError:
                    pass
        roots: list[str] = []
        for t in texts:
            for m in _API_URL.finditer(t):
                u = m.group(1).rstrip("/")
                if u not in roots:
                    roots.append(u)
            for pat in _SECRETS:
                for m in pat.finditer(t):
                    if m.group(1) not in self.candidates:
                        self.candidates.append(m.group(1))
        for r in roots:
            if re.search(r"/v\d+$", r):
                self.bases.append(r)
            else:
                self.bases += [r + "/public/v1", r + "/app/v1"]
        for b in (API_PUBLIC, API):
            if b not in self.bases:
                self.bases.append(b)

    def find_secret(self) -> str:
        self.discover()
        if not self.candidates:
            raise RecycleError("Geen geheim in de scripts van recycleapp.be (site veranderd?): zet RECYCLE_SECRET")
        return self.candidates[0]

    def _token(self, base: str) -> str | None:
        for secret in self.candidates[:6]:
            try:
                r = self._get(base + "/access-token", headers={"x-secret": secret, "x-consumer": CONSUMER})
                token = (r.json() or {}).get("accessToken")
            except (RecycleError, ValueError):
                continue
            if token:
                self.secret = secret
                return token
        return None

    def _try(self, base: str, path: str, params: dict, token: str | None) -> httpx.Response | None:
        headers = {"x-consumer": CONSUMER, "Accept": "application/json"}
        if token:
            headers["Authorization"] = token
        try:
            r = self.http.get(base + path, params=params, headers=headers)
        except httpx.HTTPError as e:
            self.last = type(e).__name__
            return None
        self.last = f"HTTP {r.status_code}"
        if r.status_code == 200 and "json" in r.headers.get("content-type", "json"):
            return r
        return None

    def api(self, path: str, params: dict) -> dict:
        if self.base:
            r = self._try(self.base, path, params, self.token)
            if r is None and self.token:  # token verlopen
                self.token = self._token(self.base)
                r = self._try(self.base, path, params, self.token)
            if r is None:
                raise RecycleError(f"recycleapp.be antwoordt niet meer op {path}")
            return r.json() or {}
        self.discover()
        for base in self.bases:
            # Eerst zonder token (een publieke API), dan met een token uit het geheim.
            r = self._try(base, path, params, None)
            token = None
            if r is None and self.candidates:
                token = self._token(base)
                r = self._try(base, path, params, token) if token else None
            if r is not None:
                self.base, self.token = base, token
                return r.json() or {}
        raise RecycleError("Kon de API van recycleapp.be niet aanspreken (site veranderd?). Geprobeerd: "
                           + ", ".join(_short(b) for b in self.bases)
                           + (f" (laatste antwoord: {self.last})" if self.last else "")
                           + ("" if self.candidates else "; geen geheim gevonden, zet eventueel RECYCLE_SECRET"))

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


def _short(url: str) -> str:
    return url.split("?")[0].replace("https://", "")


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
