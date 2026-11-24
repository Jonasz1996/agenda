"""Database: PostgreSQL in productie (DATABASE_URL), SQLite voor tests en ontwikkeling."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, delete,
                        insert, select, update)
from sqlalchemy.engine import Engine

metadata = MetaData()

# Eigen tabelnamen (agenda_*): botst niet met de tabel van de oude app in dezelfde database.
events = Table(
    "agenda_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(80), nullable=False, unique=True),
    Column("title", String(200), nullable=False),
    Column("start", DateTime(timezone=True), nullable=False, index=True),
    Column("end", DateTime(timezone=True), nullable=False),
    Column("all_day", Boolean, nullable=False, default=False),
    Column("category", String(40), nullable=True),
    Column("notes", Text, nullable=True),
    # manual: zelf ingevoerd, recycle: uit recycleapp.be (wordt bij elke sync bijgewerkt)
    Column("source", String(20), nullable=False, default="manual"),
    Column("external_id", String(120), nullable=True, unique=True),
    Column("created", DateTime(timezone=True), nullable=False),
    Column("updated", DateTime(timezone=True), nullable=False),
)

settings = Table(
    "agenda_settings", metadata,
    Column("key", String(60), primary_key=True),
    Column("value", Text, nullable=True),
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite geeft tijden zonder tijdzone terug: die zijn altijd UTC opgeslagen."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _utc(fields: dict) -> dict:
    """Altijd in UTC opslaan: SQLite bewaart geen tijdzone (PostgreSQL wel, daar maakt het niets uit)."""
    return {k: (v.astimezone(timezone.utc) if isinstance(v, datetime) and v.tzinfo else v) for k, v in fields.items()}


def make_engine(url: str) -> Engine:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    kw = {"pool_pre_ping": True} if url.startswith("postgresql") else {}
    engine = create_engine(url, future=True, **kw)
    metadata.create_all(engine)
    return engine


def row_out(r) -> dict:
    return {"id": r.id, "uid": r.uid, "title": r.title, "start": aware(r.start), "end": aware(r.end),
            "all_day": bool(r.all_day), "category": r.category, "notes": r.notes, "source": r.source,
            "external_id": r.external_id, "updated": aware(r.updated)}


def list_events(engine: Engine, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    q = select(events).order_by(events.c.start, events.c.id)
    if start is not None:
        q = q.where(events.c.end >= start.astimezone(timezone.utc))
    if end is not None:
        q = q.where(events.c.start < end.astimezone(timezone.utc))
    with engine.connect() as c:
        return [row_out(r) for r in c.execute(q)]


def get_event(engine: Engine, event_id: int) -> dict | None:
    with engine.connect() as c:
        r = c.execute(select(events).where(events.c.id == event_id)).first()
    return row_out(r) if r else None


def add_event(engine: Engine, *, title: str, start: datetime, end: datetime, all_day: bool = False,
              category: str | None = None, notes: str | None = None, source: str = "manual",
              external_id: str | None = None) -> int:
    t = now()
    with engine.begin() as c:
        res = c.execute(insert(events).values(**_utc(dict(
            uid=f"{uuid.uuid4()}@agenda.jbogaert.be", title=title, start=start, end=end, all_day=all_day,
            category=category, notes=notes, source=source, external_id=external_id, created=t, updated=t))))
        return int(res.inserted_primary_key[0])


def update_event(engine: Engine, event_id: int, **fields) -> bool:
    fields["updated"] = now()
    with engine.begin() as c:
        return c.execute(update(events).where(events.c.id == event_id).values(**_utc(fields))).rowcount > 0


def delete_event(engine: Engine, event_id: int) -> bool:
    with engine.begin() as c:
        return c.execute(delete(events).where(events.c.id == event_id)).rowcount > 0


def get_setting(engine: Engine, key: str, default: str | None = None) -> str | None:
    with engine.connect() as c:
        v = c.execute(select(settings.c.value).where(settings.c.key == key)).scalar()
    return default if v is None else v


def set_setting(engine: Engine, key: str, value: str | None) -> None:
    with engine.begin() as c:
        if c.execute(select(settings.c.key).where(settings.c.key == key)).first():
            c.execute(update(settings).where(settings.c.key == key).values(value=value))
        else:
            c.execute(insert(settings).values(key=key, value=value))
