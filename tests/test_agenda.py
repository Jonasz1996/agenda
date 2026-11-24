import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from agenda import create_app, db, ics, sync
from agenda.recycle import Client, Pickup, RecycleError, parse_collections
from tools.import_old import convert

BXL = ZoneInfo("Europe/Brussels")


PG = os.environ.get("AGENDA_TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _schoon():
    if PG:  # elke test een lege PostgreSQL-database
        e = db.make_engine(PG)
        db.metadata.drop_all(e)
        e.dispose()
    yield


def url(tmp_path, name="t"):
    return PG or f"sqlite:///{tmp_path}/{name}.db"


@pytest.fixture
def app(tmp_path):
    a = create_app({"DATABASE_URL": url(tmp_path), "PASSWORD": "pw", "API_TOKEN": "tok",
                    "RECYCLE_AUTO": False, "TESTING": True})
    a.testing = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def login(client) -> str:
    r = client.post("/login", data={"password": "pw"})
    assert r.status_code == 302
    with client.session_transaction() as s:
        return s["csrf"]


# --- oude routes blijven werken, maar wijzigen vraagt een login -----------------------------------------------

def test_lezen_open_wijzigen_niet(client):
    assert client.get("/api/events").json == []
    assert client.post("/api/add_event", json={"title": "x", "start": "2026-11-25"}).status_code == 401
    assert client.post("/api/delete_event", json={"id": 1}).status_code == 401
    assert client.get("/").status_code == 200


def test_met_cookie_alleen_met_csrf(client):
    csrf = login(client)
    assert client.post("/api/add_event", json={"title": "x", "start": "2026-11-25"}).status_code == 401
    r = client.post("/api/add_event", json={"title": "PMD", "start": "2026-11-25"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201
    assert r.json["allDay"] is True and r.json["start"] == "2026-11-25" and r.json["end"] == "2026-11-26"


def test_fout_wachtwoord(client):
    assert client.post("/login", data={"password": "nee"}).status_code == 200
    with client.session_transaction() as s:
        assert "user" not in s


def test_token_toevoegen_bewerken_verwijderen(client):
    h = {"Authorization": "Bearer tok"}
    r = client.post("/api/add_event", json={"title": "Tandarts", "start": "2026-11-25T10:00", "end": "2026-11-25T11:00"},
                    headers=h)
    assert r.status_code == 201
    eid = r.json["id"]
    assert r.json["start"].startswith("2026-11-25T10:00:00+01:00")
    r = client.post("/api/update_event", json={"id": eid, "title": "Tandarts (controle)"}, headers=h)
    assert r.json["title"] == "Tandarts (controle)" and r.json["start"].startswith("2026-11-25T10:00")
    assert client.post("/api/update_event", json={"id": eid, "start": "2026-11-25T12:00", "end": "2026-11-25T11:00"},
                       headers=h).status_code == 400
    assert client.post("/api/add_event", json={"title": "", "start": "2026-11-25"}, headers=h).status_code == 400
    assert client.post("/api/add_event", json={"title": "x", "start": "morgen"}, headers=h).status_code == 400
    assert client.post("/api/delete_event", json={"id": eid}, headers=h).json == {"ok": True}
    assert client.post("/api/delete_event", json={"id": eid}, headers=h).status_code == 404
    assert client.post("/api/add_event", json={"title": "x", "start": "2026-11-25"},
                       headers={"Authorization": "Bearer fout"}).status_code == 401


def test_venster_op_events(client):
    h = {"Authorization": "Bearer tok"}
    for d in ("2026-11-20", "2026-11-25", "2026-12-10"):
        client.post("/api/add_event", json={"title": d, "start": d}, headers=h)
    got = client.get("/api/events?start=2026-11-23T00:00:00%2B01:00&end=2026-12-01").json
    assert [e["title"] for e in got] == ["2026-11-25"]


def test_lees_token(tmp_path):
    a = create_app({"DATABASE_URL": url(tmp_path, "r"), "READ_TOKEN": "lees", "RECYCLE_AUTO": False,
                    "TESTING": True})
    c = a.test_client()
    assert c.get("/ics").status_code == 401
    assert c.get("/ics?token=lees").status_code == 200
    assert c.get("/").status_code == 302


def test_zonder_wachtwoord_staat_wijzigen_uit(tmp_path):
    a = create_app({"DATABASE_URL": url(tmp_path, "z"), "RECYCLE_AUTO": False, "TESTING": True})
    r = a.test_client().post("/api/add_event", json={"title": "x", "start": "2026-11-25"})
    assert r.status_code == 403 and "AGENDA_PASSWORD" in r.json["error"]


def test_authentik_header_alleen_van_vertrouwde_proxy(tmp_path):
    a = create_app({"DATABASE_URL": url(tmp_path, "p"), "PROXY_USER_HEADER": "X-authentik-username",
                    "TRUSTED_PROXIES": ["10.0.0.5"], "RECYCLE_AUTO": False, "TESTING": True})
    c = a.test_client()
    page = c.get("/", headers={"X-authentik-username": "jonas"}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert b'data-user=""' in page.data
    page = c.get("/", headers={"X-authentik-username": "jonas"}, environ_base={"REMOTE_ADDR": "10.0.0.5"})
    assert b'data-user="jonas"' in page.data
    with c.session_transaction() as s:
        csrf = s["csrf"]
    r = c.post("/api/add_event", json={"title": "x", "start": "2026-11-25"},
               headers={"X-authentik-username": "jonas", "X-CSRF-Token": csrf}, environ_base={"REMOTE_ADDR": "10.0.0.5"})
    assert r.status_code == 201


# --- ICS ------------------------------------------------------------------------------------------------------

def test_ics_feed(client):
    h = {"Authorization": "Bearer tok"}
    today = datetime.now(BXL).date()
    client.post("/api/add_event", json={"title": "PMD", "start": (today + timedelta(days=1)).isoformat()}, headers=h)
    client.post("/api/add_event", json={"title": "Overleg; budget, Q1", "start": f"{today}T14:00", "notes": "a\nb"},
                headers=h)
    r = client.get("/ics")
    assert r.mimetype == "text/calendar"
    t = r.get_data(as_text=True)
    assert t.startswith("BEGIN:VCALENDAR\r\n") and t.endswith("END:VCALENDAR\r\n")
    assert t.count("BEGIN:VEVENT") == 2 and t.count("DTSTAMP:") == 2
    assert f"DTSTART;VALUE=DATE:{today + timedelta(days=1):%Y%m%d}" in t
    assert "SUMMARY:Overleg\\; budget\\, Q1" in t and "DESCRIPTION:a\\nb" in t
    assert "TRIGGER;RELATED=START:-PT6H" in t and t.count("BEGIN:VALARM") == 1  # alleen afval
    assert all(len(line.encode()) <= 75 for line in t.split("\r\n"))
    assert client.get("/agenda.ics").status_code == 200


def test_ics_vouwt_lange_regels():
    e = {"uid": "u@x", "title": "é" * 100, "start": datetime(2026, 11, 25, 9, tzinfo=BXL),
         "end": datetime(2026, 11, 25, 10, tzinfo=BXL), "all_day": False, "category": None, "notes": None,
         "source": "manual", "updated": datetime(2026, 11, 24, tzinfo=BXL)}
    t = ics.build([e], reminder=None)
    lines = t.split("\r\n")
    assert all(len(x.encode()) <= 75 for x in lines)
    unfolded = t.replace("\r\n ", "")
    assert "SUMMARY:" + "é" * 100 in unfolded


# --- recycleapp.be -----------------------------------------------------------------------------------------

API = {
    "items": [
        {"timestamp": "2026-11-25T00:00:00.000Z", "type": "collection", "fraction": {"name": {"nl": "PMD"}, "color": "#1e88e5"}},
        {"timestamp": "2026-11-26T00:00:00.000Z", "type": "collection", "fraction": {"name": {"nl": "Restafval"}},
         "exception": {"replacedBy": {"timestamp": "2026-11-27T00:00:00.000Z"}}},
        {"timestamp": "2026-11-27T00:00:00.000Z", "type": "collection", "fraction": {"name": {"nl": "Restafval"}},
         "exception": {"replaces": {"timestamp": "2026-11-26T00:00:00.000Z"}}},
        {"timestamp": "2026-11-28T00:00:00.000Z", "type": "event", "event": {"title": {"nl": "Kringloopdag"}}},
        {"timestamp": "2026-12-01T23:00:00.000Z", "type": "collection", "fraction": {"name": {"nl": "GFT"}}},
    ]
}


def test_parse_collections():
    got = [(p.day.isoformat(), p.fraction) for p in parse_collections(API)]
    assert got == [("2026-11-25", "PMD"), ("2026-11-27", "Restafval"), ("2026-12-02", "GFT")]


def fake_recycle(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        u = req.url
        if u.host == "www.recycleapp.be" and u.path == "/":
            return httpx.Response(200, text='<html><script src="/static/js/main.abc123.js"></script></html>')
        if u.path == "/static/js/main.abc123.js":
            return httpx.Response(200, text='var x={api:"...",secret:"FAKEsecretVoorDeTest_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",y=1}')
        if u.path.endswith("/access-token"):
            assert req.headers["x-consumer"] == "recycleapp.be" and req.headers["x-secret"].startswith("FAKEsecret")
            return httpx.Response(200, json={"accessToken": "acc", "expiresAt": "2026-11-25T00:00:00Z"})
        assert req.headers["authorization"] == "acc"
        if u.path.endswith("/zipcodes"):
            return httpx.Response(200, json={"items": [{"id": "9000-44021", "code": "9000"}]})
        if u.path.endswith("/streets"):
            return httpx.Response(200, json={"items": [{"id": "s1", "name": "Andere Straat"},
                                                       {"id": "s2", "name": "Kerkstraat"}]})
        if u.path.endswith("/collections"):
            assert u.params["streetId"] == "s2" and u.params["houseNumber"] == "12"
            return httpx.Response(200, json=API)
        return httpx.Response(404)
    return handler


def test_recycle_client_volledige_flow():
    calls = []
    c = Client(httpx.Client(transport=httpx.MockTransport(fake_recycle(calls))))
    got = c.pickups("9000", "Kerkstraat", "12", date(2026, 11, 24), date(2027, 5, 1))
    assert [p.fraction for p in got] == ["PMD", "Restafval", "GFT"]
    assert sum(1 for r in calls if r.url.path.endswith("/access-token")) == 1


def test_recycle_fout_in_gewone_taal():
    c = Client(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>"))))
    with pytest.raises(RecycleError, match="script"):
        c.find_secret()


def test_sync_voegt_toe_werkt_bij_en_ruimt_op(app):
    engine = app.config["ENGINE"]
    for k, v in (("postcode", "9000"), ("street", "Kerkstraat"), ("number", "12")):
        db.set_setting(engine, f"recycle_{k}", v)
    # De oude agenda had PMD al op 25/11: geen dubbel.
    db.add_event(engine, title="pmd", start=datetime(2026, 11, 25, tzinfo=BXL),
                 end=datetime(2026, 11, 26, tzinfo=BXL), all_day=True)
    calls = []
    client = Client(httpx.Client(transport=httpx.MockTransport(fake_recycle(calls))))
    r = sync.run(engine, client, today=date(2026, 11, 24))
    assert r == {"added": 2, "changed": 0, "removed": 0, "skipped": 1, "total": 3}
    titles = sorted((e["title"], e["source"]) for e in db.list_events(engine))
    assert titles == [("GFT", "recycle"), ("Restafval", "recycle"), ("pmd", "manual")]
    # Tweede keer: niets nieuws.
    assert sync.run(engine, client, today=date(2026, 11, 24))["added"] == 0
    # Een ophaling verdwijnt uit recycleapp.be: weg uit de agenda.
    gone = [p for p in parse_collections(API) if p.fraction != "GFT"]
    assert sync.apply(engine, gone, date(2026, 11, 24), BXL)["removed"] == 1
    st = sync.status(engine)
    assert st["configured"] and st["error"] is None and st["synced"]


def test_sync_onthoudt_verwijderd_en_bewerkt(app, client):
    engine = app.config["ENGINE"]
    sync.apply(engine, [Pickup(date(2026, 11, 30), "Glas", None), Pickup(date(2026, 12, 3), "PMD", None)],
               date(2026, 11, 24), BXL)
    evs = {e["title"]: e for e in db.list_events(engine)}
    h = {"Authorization": "Bearer tok"}
    client.post("/api/delete_event", json={"id": evs["Glas"]["id"]}, headers=h)
    client.post("/api/update_event", json={"id": evs["PMD"]["id"], "title": "PMD (later)"}, headers=h)
    r = sync.apply(engine, [Pickup(date(2026, 11, 30), "Glas", None), Pickup(date(2026, 12, 3), "PMD", None)],
                   date(2026, 11, 24), BXL)
    assert r["added"] == 1  # PMD komt terug als ophaling; de bewerkte blijft als eigen afspraak
    titles = sorted(e["title"] for e in db.list_events(engine))
    assert titles == ["PMD", "PMD (later)"]


def test_sync_zonder_adres_en_fout(app):
    engine = app.config["ENGINE"]
    with pytest.raises(RecycleError, match="adres"):
        sync.run(engine)
    for k, v in (("postcode", "9000"), ("street", "Kerkstraat"), ("number", "12")):
        db.set_setting(engine, f"recycle_{k}", v)
    boom = Client(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(RecycleError, match="503"):
        sync.run(engine, boom)
    assert "503" in sync.status(engine)["error"]
    assert not sync.due(engine)  # na een fout pas over een uur opnieuw


def test_recycle_routes(client):
    csrf = login(client)
    h = {"X-CSRF-Token": csrf}
    assert client.post("/api/recycle", json={"postcode": "9000", "street": ""}, headers=h).status_code == 400
    r = client.post("/api/recycle", json={"postcode": "9000", "street": "Kerkstraat", "number": "12"}, headers=h)
    assert r.json["configured"] is True
    assert client.get("/api/recycle").json["street"] == "Kerkstraat"


# --- oude gegevens overnemen ---------------------------------------------------------------------------------

def test_import_oud():
    e = convert({"id": 7, "title": "zwarte zak", "start": "2026-11-24T23:00:00Z", "end": "2026-11-25T23:00:00Z"}, BXL)
    assert e["all_day"] and e["start"] == datetime(2026, 11, 25, tzinfo=BXL) and e["category"] == "afval"
    e = convert({"id": 8, "title": "Afspraak", "start": "2026-11-25T09:00:00Z", "end": "2026-11-25T10:00:00Z"}, BXL)
    assert not e["all_day"] and e["external_id"] == "old:8"
    e = convert({"id": 9, "title": "glas", "start": "2026-11-25", "end": "2026-11-25"}, BXL)
    assert e["all_day"] and e["end"] - e["start"] == timedelta(days=1)


def test_healthz(client):
    assert client.get("/healthz").json["ok"] is True
