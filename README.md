# Agenda 2.0

Kleine agenda (vooral de afvalkalender) met een ICS-feed voor homepage en je gsm. Opvolger van de oude Flask-app op
`agenda.jbogaert.be`, met dezelfde routes zodat homepage en scripts blijven werken.

## Wat is nieuw tegenover de oude app

- **Wijzigen vraagt een login.** Lezen (`/`, `/api/events`, `/ics`) blijft open op het LAN, zoals nu (homepage
  haalt de feed op `http://192.168.105.172:5269/ics`). Toevoegen, bewerken en wissen kan alleen:
  - in de webinterface na aanmelden met `AGENDA_PASSWORD` (of `AGENDA_PASSWORD_HASH`), met een CSRF-token;
  - via Authentik: zet `AGENDA_PROXY_USER_HEADER=X-authentik-username` en het IP van NPM in
    `AGENDA_TRUSTED_PROXIES`, dan ben je via `agenda.jbogaert.be` meteen aangemeld (geen tweede login);
  - voor scripts met `Authorization: Bearer <AGENDA_API_TOKEN>`.
  Staat er niets ingesteld, dan kan niemand wijzigen (veilige standaard). Wil je ook lezen afschermen, zet dan
  `AGENDA_READ_TOKEN` en gebruik `/ics?token=...`.
- **Afspraken bewerken en verslepen** (de oude app kon alleen toevoegen en wissen), met hele dag, soort en notitie.
- **Afvalkalender automatisch uit recycleapp.be**: postcode, straat en huisnummer in de webinterface
  (*Afvalkalender*) of in `RECYCLE_*`. Elke dag haalt de app de ophaaldagen voor de komende 180 dagen op; verplaatste
  en geschrapte ophalingen worden bijgewerkt. Je eigen afspraken blijven altijd staan; staat dezelfde ophaling er al
  (bv. uit de oude agenda), dan komt er geen dubbel. Wis je een ophaling, dan zet de sync ze niet terug; bewerk je
  er een, dan wordt ze een eigen afspraak. Lukt het ophalen niet, dan blijft alles zoals het was en staat de reden
  in het venster en op `/healthz`.
- **Betere ICS-feed**: `DTSTAMP`, `LAST-MODIFIED`, vaste UID's, hele dagen als `VALUE=DATE`, `CATEGORIES`,
  gevouwen regels, naam van de agenda, en voor afval een **herinnering de avond ervoor om 18:00**
  (`AGENDA_REMINDER=-PT6H`, leeg = geen). De feed geeft afspraken vanaf 90 dagen geleden.
- **gunicorn** in plaats van de ontwikkelserver van Flask, Python 3.13, draait niet als root, healthcheck.
- **Geen wachtwoorden in de code**: alles komt uit `.env`.
- FullCalendar 6.1.15 zit in `static/` (geen CDN nodig), met een gsm-weergave (lijst).

## Routes

| Route | Wat |
| --- | --- |
| `GET /` | de agenda |
| `GET /api/events?start=&end=` | `[{id, title, start, end, allDay, category, notes, source}]` (oude velden blijven) |
| `POST /api/add_event` | `{title, start, end?, allDay?, category?, notes?}` |
| `POST /api/update_event` | `{id, ...}` (nieuw) |
| `POST /api/delete_event` | `{id}` |
| `GET /ics`, `GET /agenda.ics` | ICS-feed |
| `GET/POST /api/recycle`, `POST /api/recycle/sync` | adres en status van de afvalkalender, nu ophalen |
| `GET /healthz` | voor Docker en ClusterForge |

Datums: `2026-11-25` is een hele dag, `2026-11-25T10:00` is lokale tijd (Europe/Brussels), met `Z` of `+01:00`
zoals opgegeven.

## Uitrollen (ClusterForge-stack "agenda" op het wger-cluster)

De database-container en de poort (5269) blijven dezelfde. De nieuwe app gebruikt eigen tabellen
(`agenda_events`, `agenda_settings`), dus de oude tabel blijft onaangeroerd staan: terugdraaien is de oude image
weer starten.

1. Back-up van de database (`pg_dump`) via ClusterForge of op wger-01.
2. Code naar `/docker/agenda` (beide nodes, of via de stack-sync), `.env` invullen (zie `.env.example`; gebruik
   dezelfde database-gebruiker en hetzelfde wachtwoord als nu).
3. Oude afspraken overnemen terwijl de oude app nog draait:
   `docker compose run --rm agenda python -m tools.import_old http://192.168.105.172:5269/api/events`
   (twee keer draaien maakt geen dubbels).
4. `docker compose up -d --build`, controleer `/healthz` en `/ics` (46 afspraken + afval).
5. In de webinterface: *Afvalkalender* → adres → *Bewaren en ophalen*.

## Ontwikkelen

```sh
pip install -r requirements-dev.txt
pytest                                   # SQLite
AGENDA_TEST_DATABASE_URL=postgresql://... pytest   # tegen PostgreSQL
DATABASE_URL=sqlite:///dev.db AGENDA_PASSWORD=pw RECYCLE_AUTO=0 flask --app agenda.wsgi run
```
