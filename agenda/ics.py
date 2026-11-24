"""ICS-feed (RFC 5545): zelf geschreven, met DTSTAMP, vaste UID's, CATEGORIES en een herinnering voor afval."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PRODID = "-//jbogaert.be//agenda 2.0//NL"


def esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def fold(line: str) -> list[str]:
    """Regels van hoogstens 75 bytes; vervolgregels beginnen met een spatie (UTF-8 niet middenin knippen)."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return [out[0]] + [" " + x for x in out[1:]]


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build(events: list[dict], *, name: str = "Agenda", tz: str = "Europe/Brussels", reminder: str | None = "-PT6H",
          stamp: datetime | None = None) -> str:
    """events: zoals db.list_events. reminder: ICS-duur ten opzichte van het begin voor afval (leeg = geen)."""
    zone = ZoneInfo(tz)
    stamp = stamp or datetime.now(timezone.utc)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{esc(name)}", f"X-WR-TIMEZONE:{tz}", "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
             "X-PUBLISHED-TTL:PT1H"]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{utc(stamp)}", f"LAST-MODIFIED:{utc(e['updated'])}"]
        if e["all_day"]:
            d0 = e["start"].astimezone(zone).date()
            d1 = e["end"].astimezone(zone).date()
            if d1 <= d0:
                d1 = d0 + timedelta(days=1)
            lines += [f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{d1:%Y%m%d}", "TRANSP:TRANSPARENT"]
        else:
            lines += [f"DTSTART:{utc(e['start'])}", f"DTEND:{utc(max(e['end'], e['start']))}"]
        lines.append(f"SUMMARY:{esc(e['title'])}")
        if e.get("category"):
            lines.append(f"CATEGORIES:{esc(e['category'])}")
        if e.get("notes"):
            lines.append(f"DESCRIPTION:{esc(e['notes'])}")
        if reminder and is_waste(e):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(e['title'])} buiten zetten",
                      f"TRIGGER;RELATED=START:{reminder}", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "".join(part + "\r\n" for line in lines for part in fold(line))


WASTE_WORDS = ("afval", "pmd", "gft", "papier", "karton", "glas", "rest", "zak", "snoeiafval", "grofvuil", "textiel")


def is_waste(e: dict) -> bool:
    if e.get("source") == "recycle" or (e.get("category") or "").lower() == "afval":
        return True
    t = (e.get("title") or "").lower()
    return e.get("all_day", False) and any(w in t for w in WASTE_WORDS)
