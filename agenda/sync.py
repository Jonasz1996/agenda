"""Ophaaldagen uit recycleapp.be in de agenda zetten, en dat dagelijks herhalen."""

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine

from . import db
from .recycle import Client, Pickup, RecycleError

log = logging.getLogger("agenda.sync")
DAYS_AHEAD = 180
EVERY = 24 * 3600
_lock = threading.Lock()


def address(engine: Engine) -> tuple[str, str, str] | None:
    parts = tuple((db.get_setting(engine, f"recycle_{k}") or "").strip() for k in ("postcode", "street", "number"))
    return parts if all(parts) else None


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def apply(engine: Engine, pickups: list[Pickup], today: date, tz: ZoneInfo) -> dict:
    """Zet de ophalingen in de agenda. Eigen afspraken blijven altijd staan; staat er op dezelfde dag al een eigen
    afspraak met dezelfde naam (bv. "PMD" uit de oude agenda), dan komt er geen dubbel bij."""
    start_today = datetime.combine(today, datetime.min.time(), tz)
    with engine.connect() as c:
        rows = c.execute(select(db.events).where(db.events.c.end >= start_today.astimezone(timezone.utc))).all()
    existing = {r.external_id: db.row_out(r) for r in rows if r.source == "recycle" and r.external_id}
    manual = {(db.aware(r.start).astimezone(tz).date(), _norm(r.title)) for r in rows if r.source != "recycle"}
    hidden = set((db.get_setting(engine, "recycle_hidden") or "").split("\n"))
    added = changed = skipped = 0
    seen = set()
    for p in pickups:
        if p.day < today:
            continue
        seen.add(p.external_id)
        if p.external_id in hidden:  # zelf weggehaald: niet terugzetten
            skipped += 1
            continue
        s = datetime.combine(p.day, datetime.min.time(), tz)
        e = s + timedelta(days=1)
        cur = existing.get(p.external_id)
        if cur:
            if cur["title"] != p.fraction or cur["start"] != s:
                db.update_event(engine, cur["id"], title=p.fraction, start=s, end=e)
                changed += 1
            continue
        if (p.day, _norm(p.fraction)) in manual:
            skipped += 1
            continue
        db.add_event(engine, title=p.fraction, start=s, end=e, all_day=True, category="afval", source="recycle",
                     external_id=p.external_id)
        added += 1
    removed = 0
    for ext, ev in existing.items():
        if ext not in seen and ev["start"].astimezone(tz).date() >= today:
            db.delete_event(engine, ev["id"])
            removed += 1
    return {"added": added, "changed": changed, "removed": removed, "skipped": skipped, "total": len(pickups)}


def run(engine: Engine, client: Client | None = None, tz: str = "Europe/Brussels", today: date | None = None) -> dict:
    """Eén sync. Gooit RecycleError als het niet lukt; de agenda blijft dan zoals hij was."""
    addr = address(engine)
    if not addr:
        raise RecycleError("Nog geen adres ingesteld (postcode, straat en huisnummer)")
    zone = ZoneInfo(tz)
    today = today or datetime.now(zone).date()
    with _lock:
        try:
            pickups = (client or Client(secret=db.get_setting(engine, "recycle_secret") or None)).pickups(
                *addr, today, today + timedelta(days=DAYS_AHEAD))
            if not pickups:
                raise RecycleError("recycleapp.be gaf geen enkele ophaling voor dit adres: klopt het huisnummer?")
            result = apply(engine, pickups, today, zone)
        except RecycleError as e:
            db.set_setting(engine, "recycle_error", str(e))
            db.set_setting(engine, "recycle_tried", db.now().isoformat())
            raise
        db.set_setting(engine, "recycle_error", None)
        db.set_setting(engine, "recycle_tried", db.now().isoformat())
        db.set_setting(engine, "recycle_synced", db.now().isoformat())
        db.set_setting(engine, "recycle_result", ", ".join(f"{k} {v}" for k, v in result.items()))
        return result


def status(engine: Engine) -> dict:
    addr = address(engine)
    return {"configured": bool(addr), "postcode": addr[0] if addr else db.get_setting(engine, "recycle_postcode"),
            "street": addr[1] if addr else db.get_setting(engine, "recycle_street"),
            "number": addr[2] if addr else db.get_setting(engine, "recycle_number"),
            "synced": db.get_setting(engine, "recycle_synced"), "tried": db.get_setting(engine, "recycle_tried"),
            "error": db.get_setting(engine, "recycle_error"), "result": db.get_setting(engine, "recycle_result")}


def due(engine: Engine, every: int = EVERY) -> bool:
    if not address(engine):
        return False
    tried = db.get_setting(engine, "recycle_tried")
    if not tried:
        return True
    age = (db.now() - datetime.fromisoformat(tried)).total_seconds()
    # Na een fout sneller opnieuw (na een uur), anders één keer per dag.
    return age >= (3600 if db.get_setting(engine, "recycle_error") else every)


def start_background(engine: Engine, tz: str, interval: int = 600) -> threading.Thread:
    def loop():
        time.sleep(30)
        while True:
            try:
                if due(engine):
                    log.info("recycleapp.be: %s", run(engine, tz=tz))
            except RecycleError as e:
                log.warning("recycleapp.be: %s", e)
            except Exception:  # de achtergrond mag nooit stoppen
                log.exception("recycleapp.be: onverwachte fout")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="recycle-sync", daemon=True)
    t.start()
    return t


__all__ = ["run", "apply", "status", "due", "start_background"]
