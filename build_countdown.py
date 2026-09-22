#!/usr/bin/env python3
"""
SD74 break countdowns -- builder
=================================

Reads school_year.json (the district's official school-year dates), checks the
district's public calendar feed for the 8th grade (Class of 2027) event, and
writes:

  countdown.json  -- the resolved countdown data
  embed.html      -- the paste-ready page for Google Sites, built from the
                     index.html template with the data and a live-update link
                     filled in

Paste embed.html into Google Sites, never index.html. Only this script writes
embed.html, so the pasted copy is always complete.

Where the dates come from
--------------------------
Break dates come from the district's 2026-2027 calendar PDF, transcribed into
school_year.json. They are official and don't change during the year.

The 8th grade date is NOT on that PDF. It's looked up in the district's live
calendar feed by title ("8th grade graduation", "8th grade promotion", "8th
grade last day", ...). Until the district posts it, the 8th grade countdown
targets the last day of school and says so plainly -- it never guesses a date.

Run locally:
    pip install requests
    python3 build_countdown.py
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

try:
    import requests
except ImportError:          # the page still builds from school_year.json alone
    requests = None

CONFIG_FILE = "school_year.json"
DATA_FILE = "countdown.json"
TEMPLATE_FILE = "index.html"
EMBED_FILE = "embed.html"

DISTRICT_ICS_URL = (
    "https://calendar.google.com/calendar/ical/"
    "c_434d10cea58b170a51434a2f6e2b051def420ade92c062309647605353d7c139"
    "%40group.calendar.google.com/public/basic.ics"
)
HEADERS = {"User-Agent": "SD74CountdownBot/1.0 (parent-run countdown page)"}

URL_REGION_RE = re.compile(r'(/\*URL_START\*/).*?(/\*URL_END\*/)', re.DOTALL)
DATA_REGION_RE = re.compile(r'(/\*DATA_START\*/).*?(/\*DATA_END\*/)', re.DOTALL)

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:
    LOCAL_TZ = None


# --- Reading the district feed ----------------------------------------------

def unfold(text):
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


DT_RE = re.compile(r'^DTSTART([^:]*):(\d{8})(?:T(\d{2})(\d{2})(\d{2}))?(Z?)', re.IGNORECASE)


def parse_feed(text):
    """VEVENTs -> [{'title', 'date', 'time' ('HH:MM' local or '')}]"""
    events, cur = [], None
    for line in unfold(text):
        up = line.upper()
        if up.startswith("BEGIN:VEVENT"):
            cur = {}
        elif up.startswith("END:VEVENT"):
            if cur and cur.get("date") and cur.get("title"):
                events.append(cur)
            cur = None
        elif cur is not None:
            m = DT_RE.match(line)
            if m:
                params, ymd, hh, mm, ss, z = m.groups()
                y, mo, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
                if hh is None or "VALUE=DATE" in params.upper():
                    cur["date"], cur["time"] = date(y, mo, d), ""
                else:
                    dt = datetime(y, mo, d, int(hh), int(mm), int(ss or 0))
                    if z and LOCAL_TZ:   # feeds often publish UTC; convert to school time
                        dt = dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
                    cur["date"], cur["time"] = dt.date(), f"{dt.hour:02d}:{dt.minute:02d}"
            elif up.startswith("SUMMARY"):
                cur["title"] = line.partition(":")[2].replace("\\,", ",").replace("\\;", ";").strip()
    return events


EIGHTH_RE = re.compile(r'\b(8th|eighth)[\s-]*grade|\bclass of 2027\b', re.IGNORECASE)
# Which kind of 8th grade event best represents "the class's last day",
# strongest first.
EIGHTH_PRIORITY = [
    (re.compile(r'graduat|promotion|commencement|continuation', re.I), "8th Grade Graduation"),
    (re.compile(r'last day', re.I), "8th Grade Last Day"),
    (re.compile(r'clap[\s-]*out|farewell|celebration|send[\s-]*off', re.I), "8th Grade Celebration"),
]


def find_eighth_grade(events, end_year):
    """The best 8th grade end-of-year event in the feed, or None."""
    window = (date(end_year, 4, 1), date(end_year, 7, 1))
    best = None
    for ev in events:
        if not EIGHTH_RE.search(ev["title"]) or not (window[0] <= ev["date"] <= window[1]):
            continue
        for rank, (rx, label) in enumerate(EIGHTH_PRIORITY):
            if rx.search(ev["title"]):
                cand = (rank, ev["date"], ev, label)
                if best is None or cand[:2] < best[:2]:
                    best = cand
                break
    if best is None:
        return None
    _, _, ev, label = best
    return {"date": ev["date"].isoformat(), "time": ev.get("time", ""),
            "feed_title": ev["title"], "label": label}


def fetch_feed():
    if requests is None:
        return None, "requests library not installed"
    try:
        r = requests.get(DISTRICT_ICS_URL, headers=HEADERS, timeout=25)
        r.raise_for_status()
        if "BEGIN:VCALENDAR" not in r.text.upper():
            return None, "response was not a calendar feed"
        return parse_feed(r.text), None
    except Exception as e:
        return None, str(e)


# --- Resolving countdowns ---------------------------------------------------

def resolve(cfg, feed_events, feed_error, previous_eighth=None):
    first_day = cfg["first_day"]
    end_year = int(cfg["school_year"].split("-")[1])
    by_id = {c["id"]: c for c in cfg["countdowns"]}
    out, notes = [], []

    for c in cfg["countdowns"]:
        item = {k: v for k, v in c.items() if k not in ("find_in_feed", "fallback_to")}
        item.setdefault("kind", "break")

        if c.get("find_in_feed"):
            found = find_eighth_grade(feed_events or [], end_year)
            if not found and feed_error and previous_eighth:
                # Feed is down this run: keep the date found last time
                # instead of reverting to the fallback.
                item.update({k: previous_eighth[k] for k in
                             ("start", "time", "title", "note", "source") if k in previous_eighth})
                notes.append("8th grade: feed unavailable, kept the date found on a previous run")
                out.append(item)
                continue
            if found:
                item["start"] = found["date"]
                if found["time"]:
                    item["time"] = found["time"]
                item["title"] = found["label"]
                item["note"] = f'From the district calendar: "{found["feed_title"]}".'
                item["source"] = "district feed"
                notes.append(f"8th grade: found \"{found['feed_title']}\" on {found['date']}")
            else:
                fb = by_id.get(c.get("fallback_to", ""), {})
                item["start"] = fb.get("start")
                item["title"] = "8th Grade - Class of 2027"
                item["provisional"] = True
                item["note"] = ("The district hasn't posted the 8th grade end-of-year date yet, so "
                                "this counts down to the last day of school for now. It will switch "
                                "to the 8th grade date automatically once it's on the district calendar.")
                item["source"] = "fallback: last day of school"
                why = f" (feed unavailable: {feed_error})" if feed_error else ""
                notes.append(f"8th grade: not on the district feed yet{why} -- using the last day of school")
        else:
            item["source"] = "district school-year calendar"
        if item.get("start"):
            out.append(item)

    # Progress bars run from the end of the previous break (or the first day
    # of school) to this countdown's target.
    out.sort(key=lambda x: (x["start"], x["id"] == "eighth"))
    prev_back = first_day
    for item in out:
        item["from"] = prev_back
        if item["kind"] == "break" and item.get("back"):
            prev_back = item["back"]
    return out, notes


# --- Output ----------------------------------------------------------------

def build_embed(payload):
    if not os.path.exists(TEMPLATE_FILE):
        print(f"(no {TEMPLATE_FILE} -- can't build {EMBED_FILE})")
        return
    html = open(TEMPLATE_FILE, encoding="utf-8").read()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo and URL_REGION_RE.search(html):
        url = f"https://cdn.jsdelivr.net/gh/{repo}@main/{DATA_FILE}"
        html = URL_REGION_RE.sub(lambda m: f"{m.group(1)}'{url}'{m.group(2)}", html, count=1)
    body = json.dumps(payload, ensure_ascii=False, indent=1).replace("</", "<\\/")
    if not DATA_REGION_RE.search(html):
        print(f"WARNING: no data markers found in {TEMPLATE_FILE}", file=sys.stderr)
    html = DATA_REGION_RE.sub(lambda m: f"{m.group(1)}{body}{m.group(2)}", html, count=1)
    before = open(EMBED_FILE, encoding="utf-8").read() if os.path.exists(EMBED_FILE) else None
    if html != before:
        open(EMBED_FILE, "w", encoding="utf-8").write(html)
        print(f"Wrote {EMBED_FILE}")
    else:
        print(f"{EMBED_FILE} unchanged")


def main():
    cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
    feed, err = fetch_feed()
    prev8 = None
    if err:
        print(f"District feed unavailable: {err}", file=sys.stderr)
        try:
            prev = json.load(open(DATA_FILE, encoding="utf-8"))
            prev8 = next((c for c in prev.get("countdowns", [])
                          if c["id"] == "eighth" and c.get("source") == "district feed"), None)
        except Exception:
            prev8 = None
    else:
        print(f"District feed: {len(feed)} events")

    countdowns, notes = resolve(cfg, feed, err, prev8)
    for n in notes:
        print("  " + n)
    for c in countdowns:
        t = f" {c['time']}" if c.get("time") else ""
        print(f"  {c['start']}{t}  {c['title']}  [{c['source']}]")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "school_year": cfg["school_year"],
        "timezone": cfg.get("timezone", "America/Chicago"),
        "first_day": cfg["first_day"],
        "countdowns": countdowns,
        "no_school": cfg.get("no_school", []),
        "half_days": cfg.get("half_days", []),
    }
    json.dump(payload, open(DATA_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Wrote {DATA_FILE}: {len(countdowns)} countdowns")
    build_embed(payload)


if __name__ == "__main__":
    main()
