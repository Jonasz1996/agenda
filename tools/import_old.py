"""Afspraken van de oude agenda overnemen: uit zijn /api/events (url of JSON-bestand) in de nieuwe database.

    python -m tools.import_old http://192.168.105.172:5269/api/events
    python -m tools.import_old oud.json

Gebruikt DATABASE_URL zoals de app. Twee keer draaien maakt geen dubbels (external_id = old:<id>). Een afspraak die
van middernacht tot middernacht loopt (of tot 23:59) wordt een "hele dag"-afspraak.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from agenda import db


def load(src: str) -> list[dict]:
    if src.startswith(("http://", "https://")):
        r = httpx.get(src, timeout=20)
        r.raise_for_status()
        return r.json()
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def parse(v: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def convert(item: dict, tz: ZoneInfo) -> dict:
    s = parse(item["start"], tz)
    e = parse(item.get("end") or item["start"], tz)
    ls, le = s.astimezone(tz), e.astimezone(tz)
    midnight = ls.hour == 0 and ls.minute == 0
    ends_day = (le.hour == 0 and le.minute == 0) or (le.hour == 23 and le.minute >= 59)
    all_day = len(str(item["start"])) == 10 or (midnight and (ends_day or e <= s))
    if all_day:
        d0 = ls.date()
        d1 = le.date() + (timedelta(days=1) if le.hour == 23 else timedelta())
        if d1 <= d0:
            d1 = d0 + timedelta(days=1)
        s = datetime.combine(d0, datetime.min.time(), tz)
        e = datetime.combine(d1, datetime.min.time(), tz)
    title = str(item.get("title") or "(zonder titel)").strip()[:200]
    from agenda.ics import WASTE_WORDS
    cat = "afval" if any(w in title.lower() for w in WASTE_WORDS) else None
    return {"title": title, "start": s, "end": max(e, s), "all_day": all_day, "category": cat,
            "external_id": f"old:{item['id']}"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    tz = ZoneInfo(os.environ.get("AGENDA_TZ", "Europe/Brussels"))
    engine = db.make_engine(os.environ.get("DATABASE_URL", "sqlite:///agenda.db"))
    with engine.connect() as c:
        have = set(c.execute(select(db.events.c.external_id)).scalars())
    new = skipped = 0
    for item in load(argv[1]):
        ev = convert(item, tz)
        if ev["external_id"] in have:
            skipped += 1
            continue
        db.add_event(engine, **ev)
        new += 1
    print(f"{new} overgenomen, {skipped} stonden er al")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
