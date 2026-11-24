"""Agenda 2.0: kleine agenda met ICS-feed, login op wijzigen en de afvalkalender uit recycleapp.be.

Compatibel met de oude app: GET /api/events, POST /api/add_event, POST /api/delete_event en GET /ics bestaan nog,
met dezelfde velden (id, title, start, end).
"""

import hmac
import logging
import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from . import db, ics, sync
from .recycle import RecycleError

MAX_SPAN = timedelta(days=31)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    cfg = {
        "DATABASE_URL": env("DATABASE_URL", "sqlite:///agenda.db"),
        "TZ": env("AGENDA_TZ", "Europe/Brussels"),
        "NAME": env("AGENDA_NAME", "Agenda"),
        "PASSWORD": env("AGENDA_PASSWORD"),
        "PASSWORD_HASH": env("AGENDA_PASSWORD_HASH"),
        "API_TOKEN": env("AGENDA_API_TOKEN"),
        "READ_TOKEN": env("AGENDA_READ_TOKEN"),
        "PROXY_USER_HEADER": env("AGENDA_PROXY_USER_HEADER"),  # bv. X-authentik-username
        "TRUSTED_PROXIES": [x.strip() for x in (env("AGENDA_TRUSTED_PROXIES", "") or "").split(",") if x.strip()],
        "REMINDER": env("AGENDA_REMINDER", "-PT6H"),  # afval: de avond ervoor om 18:00
        "RECYCLE_AUTO": env("RECYCLE_AUTO", "1") != "0",
        "COOKIE_SECURE": env("AGENDA_COOKIE_SECURE", "0") == "1",
    }
    cfg.update(config or {})
    engine = db.make_engine(cfg["DATABASE_URL"])
    tz = ZoneInfo(cfg["TZ"])
    app.config.update(AGENDA=cfg, ENGINE=engine)

    # Sessiesleutel: uit de omgeving, anders één keer gemaakt en in de database bewaard (gedeeld door beide nodes).
    key = env("AGENDA_SECRET_KEY") or db.get_setting(engine, "secret_key")
    if not key:
        key = secrets.token_hex(32)
        db.set_setting(engine, "secret_key", key)
    app.secret_key = key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=cfg["COOKIE_SECURE"], PERMANENT_SESSION_LIFETIME=timedelta(days=30))

    # Adres voor recycleapp.be uit de omgeving overnemen (de webinterface kan het daarna aanpassen).
    for k in ("postcode", "street", "number"):
        v = env(f"RECYCLE_{k.upper()}")
        if v and not db.get_setting(engine, f"recycle_{k}"):
            db.set_setting(engine, f"recycle_{k}", v)
    if env("RECYCLE_SECRET"):
        db.set_setting(engine, "recycle_secret", env("RECYCLE_SECRET"))

    # --- aanmelding -------------------------------------------------------------------------------------------

    def can_login() -> bool:
        return bool(cfg["PASSWORD"] or cfg["PASSWORD_HASH"])

    def proxy_user() -> str | None:
        h = cfg["PROXY_USER_HEADER"]
        if h and request.remote_addr in cfg["TRUSTED_PROXIES"]:
            return request.headers.get(h) or None
        return None

    def bearer() -> str | None:
        a = request.headers.get("Authorization", "")
        return a[7:].strip() if a.lower().startswith("bearer ") else None

    def user() -> str | None:
        return session.get("user") or proxy_user()

    def token_ok(expected: str | None, given: str | None) -> bool:
        return bool(expected and given and hmac.compare_digest(expected, given))

    def csrf() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def may_write() -> bool:
        if token_ok(cfg["API_TOKEN"], bearer()):
            return True  # scripts en homepage: Bearer-token, geen cookie dus geen CSRF
        if not user():
            return False
        # Met een cookie (of via Authentik): alleen vanuit de eigen pagina.
        return token_ok(session.get("csrf"), request.headers.get("X-CSRF-Token"))

    def may_read() -> bool:
        if not cfg["READ_TOKEN"]:
            return True
        return bool(user() or token_ok(cfg["READ_TOKEN"], request.args.get("token") or bearer())
                    or token_ok(cfg["API_TOKEN"], bearer()))

    def writer(fn):
        @wraps(fn)
        def inner(*a, **kw):
            if not may_write():
                if not (can_login() or cfg["API_TOKEN"] or cfg["PROXY_USER_HEADER"]):
                    return jsonify(error="Wijzigen staat uit: zet AGENDA_PASSWORD of AGENDA_API_TOKEN"), 403
                return jsonify(error="Niet aangemeld"), 401
            return fn(*a, **kw)
        return inner

    def reader(fn):
        @wraps(fn)
        def inner(*a, **kw):
            if not may_read():
                return jsonify(error="Sleutel nodig (?token=...)"), 401
            return fn(*a, **kw)
        return inner

    @app.after_request
    def headers(r: Response):
        r.headers.setdefault("X-Content-Type-Options", "nosniff")
        r.headers.setdefault("Referrer-Policy", "same-origin")
        r.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return r

    # --- pagina's ---------------------------------------------------------------------------------------------

    @app.get("/")
    def index():
        if cfg["READ_TOKEN"] and not user():
            return redirect(url_for("login"))
        return render_template("index.html", user=user(), csrf=csrf() if user() else "", can_login=can_login(),
                               name=cfg["NAME"], tz=cfg["TZ"], via_proxy=bool(proxy_user() and not session.get("user")))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            pw = request.form.get("password", "")
            ok = (cfg["PASSWORD_HASH"] and check_password_hash(cfg["PASSWORD_HASH"], pw)) or \
                 (cfg["PASSWORD"] and hmac.compare_digest(cfg["PASSWORD"].encode(), pw.encode()))
            if ok:
                session.clear()
                session.permanent = True
                session["user"] = "jonas"
                csrf()
                return redirect(url_for("index"))
            error = "Fout wachtwoord" if can_login() else "Aanmelden staat uit: zet AGENDA_PASSWORD"
        return render_template("login.html", error=error, name=cfg["NAME"], can_login=can_login())

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # --- API --------------------------------------------------------------------------------------------------

    def parse_when(v, field: str) -> tuple[datetime, bool]:
        if not isinstance(v, str) or not v.strip():
            abort(_bad(f"{field} ontbreekt"))
        v = v.strip()
        try:
            if len(v) == 10:
                return datetime.combine(date.fromisoformat(v), datetime.min.time(), tz), True
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            abort(_bad(f"{field} is geen datum: {v[:40]}"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=tz)), False

    def event_in(data: dict, current: dict | None = None) -> dict:
        title = (data.get("title") if "title" in data else (current or {}).get("title")) or ""
        title = str(title).strip()
        if not 1 <= len(title) <= 200:
            abort(_bad("Titel moet 1 tot 200 tekens zijn"))
        if "start" in data or not current:
            start, day = parse_when(data.get("start"), "start")
        else:
            start, day = current["start"], current["all_day"]
        all_day = bool(data["allDay"]) if "allDay" in data else day if ("start" in data or not current) else current["all_day"]
        if data.get("end"):
            end, _ = parse_when(data.get("end"), "end")
        elif current and "start" not in data:
            end = current["end"]
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        if all_day:
            start = datetime.combine(start.astimezone(tz).date(), datetime.min.time(), tz)
            end = datetime.combine(end.astimezone(tz).date(), datetime.min.time(), tz)
            if end <= start:
                end = start + timedelta(days=1)
        if end < start:
            abort(_bad("Einde ligt voor het begin"))
        if end - start > MAX_SPAN:
            abort(_bad("Een afspraak duurt hoogstens 31 dagen"))
        out = {"title": title, "start": start, "end": end, "all_day": all_day}
        for k in ("category", "notes"):
            if k in data:
                v = str(data.get(k) or "").strip()
                out[k] = v[: (40 if k == "category" else 2000)] or None
        return out

    def event_out(e: dict) -> dict:
        if e["all_day"]:
            s, en = e["start"].astimezone(tz).date().isoformat(), e["end"].astimezone(tz).date().isoformat()
        else:
            s, en = e["start"].astimezone(tz).isoformat(), e["end"].astimezone(tz).isoformat()
        return {"id": e["id"], "title": e["title"], "start": s, "end": en, "allDay": e["all_day"],
                "category": e["category"], "notes": e["notes"], "source": e["source"]}

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(_bad("JSON verwacht"))
        return data

    @app.get("/api/events")
    @reader
    def api_events():
        start = end = None
        if request.args.get("start"):
            start, _ = parse_when(request.args["start"][:25], "start")
        if request.args.get("end"):
            end, _ = parse_when(request.args["end"][:25], "end")
        return jsonify([event_out(e) for e in db.list_events(engine, start, end)])

    @app.post("/api/add_event")
    @writer
    def api_add():
        fields = event_in(body())
        eid = db.add_event(engine, **fields)
        return jsonify(event_out(db.get_event(engine, eid))), 201

    @app.post("/api/update_event")
    @writer
    def api_update():
        data = body()
        cur = db.get_event(engine, _id(data))
        if not cur:
            return jsonify(error="Afspraak niet gevonden"), 404
        fields = event_in(data, cur)
        if cur["source"] == "recycle":
            # Zelf aangepast: niet meer door de sync overschrijven of weghalen.
            fields.update(source="manual", external_id=None)
        db.update_event(engine, cur["id"], **fields)
        return jsonify(event_out(db.get_event(engine, cur["id"])))

    @app.post("/api/delete_event")
    @writer
    def api_delete():
        data = body()
        cur = db.get_event(engine, _id(data))
        if not cur:
            return jsonify(error="Afspraak niet gevonden"), 404
        db.delete_event(engine, cur["id"])
        if cur["source"] == "recycle" and cur["external_id"]:
            # Onthouden zodat de volgende sync hem niet terugzet.
            gone = set(filter(None, (db.get_setting(engine, "recycle_hidden") or "").split("\n")))
            gone.add(cur["external_id"])
            db.set_setting(engine, "recycle_hidden", "\n".join(sorted(gone)[-500:]))
        return jsonify(ok=True)

    @app.get("/ics")
    @app.get("/agenda.ics")
    @reader
    def feed():
        start = datetime.now(tz) - timedelta(days=int(request.args.get("past", 90) or 90))
        text = ics.build(db.list_events(engine, start=start), name=cfg["NAME"], tz=cfg["TZ"],
                         reminder=cfg["REMINDER"])
        return Response(text, mimetype="text/calendar",
                        headers={"Content-Disposition": "inline; filename=calendar.ics", "Cache-Control": "no-cache"})

    @app.get("/api/recycle")
    @reader
    def recycle_status():
        return jsonify(sync.status(engine))

    @app.post("/api/recycle")
    @writer
    def recycle_set():
        data = body()
        for k in ("postcode", "street", "number"):
            v = str(data.get(k) or "").strip()
            if not v or len(v) > 80:
                return jsonify(error=f"{k} ontbreekt"), 400
            db.set_setting(engine, f"recycle_{k}", v)
        db.set_setting(engine, "recycle_tried", None)
        return jsonify(sync.status(engine))

    @app.post("/api/recycle/sync")
    @writer
    def recycle_sync():
        try:
            return jsonify(ok=True, result=sync.run(engine, tz=cfg["TZ"]), status=sync.status(engine))
        except RecycleError as e:
            return jsonify(ok=False, error=str(e), status=sync.status(engine)), 502

    @app.get("/healthz")
    def health():
        n = len(db.list_events(engine))
        st = sync.status(engine)
        return jsonify(ok=True, events=n, recycle_error=st["error"], recycle_synced=st["synced"])

    if cfg["RECYCLE_AUTO"] and not app.testing and not (config or {}).get("TESTING"):
        sync.start_background(engine, cfg["TZ"])
    logging.basicConfig(level=logging.INFO)
    return app


def _bad(msg: str) -> Response:
    r = jsonify(error=msg)
    r.status_code = 400
    return r


def _id(data: dict) -> int:
    try:
        return int(data.get("id"))
    except (TypeError, ValueError):
        abort(_bad("id ontbreekt"))
