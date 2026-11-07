#!/usr/bin/env python3
"""Telegram bot for ride-forecast: answers commands from one chat and pushes the nightly forecast.
Long-polls the Bot API, stdlib only (charts need matplotlib; without it you get a text sparkline).
Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (the only chat it answers), RIDE_PLACE, RIDE_COUNTRY, RIDE_TRIPS,
     RIDE_PUSH_TIME (HH:MM local, default 21:00), TELEGRAM_API_BASE (tests), STATE_DIRECTORY (systemd sets it).
Commands: /today /tomorrow /date <day> /chart [day] /place [name[, CC]] /trips [spec|default|none] /help
"""
import json, os, re, sys, time, urllib.parse, urllib.request, uuid
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import charts, ride

HELP = """Commands:
/today, /tomorrow: verdict per trip
/date <day>: a day, e.g. 2026-11-14, 14/11, sat, monday (up to 7 days ahead)
/chart [day]: hourly graph (default: the next day that matters)
/place [name, CC]: show or change the place, e.g. /place Leon, ES
/trips [spec]: show or change your trips, e.g. /trips work 07:30-09:00 mon-fri; out 20:00-23:00
  /trips default restores the defaults, /trips none removes them
/help: this text"""
CACHE_SECONDS = 600
POLL_SECONDS = 30
PUSH_RETRY_SECONDS = 300
WEEKDAYS = {**ride.DAYS, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6}


# ---- state ---------------------------------------------------------------------------------

class State:
    """Place, trips and last push, persisted as JSON. Env values are the defaults."""

    def __init__(self, path=None):
        base = Path(os.environ.get("STATE_DIRECTORY") or Path.home() / ".local/state/ride-forecast")
        self.path = Path(path) if path else base / "state.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self._cache = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)

    def place(self):
        if "g" not in self.data:  # first use: resolve the env place once and remember it
            place = os.environ.get("RIDE_PLACE")
            if not place:
                raise LookupError("no place set: send /place <name>")
            self.data["g"] = ride.geocode(place, os.environ.get("RIDE_COUNTRY"))
            self.save()
        return self.data["g"]

    def trips_spec(self):
        return self.data.get("trips", os.environ.get("RIDE_TRIPS", ride.DEFAULT_TRIPS))

    def sources(self, g):
        key = (g["latitude"], g["longitude"])
        hit = self._cache.get(key)
        if not hit or time.time() - hit[0] > CACHE_SECONDS:
            hit = self._cache[key] = (time.time(), ride.fetch_sources(g))
        return hit[1]

    def report(self, day):
        g = self.place()
        return ride.build_report(g, day, self.sources(g), ride.parse_trips(self.trips_spec()))

    def now(self):
        return datetime.now(ZoneInfo(self.place()["timezone"]))


# ---- commands ------------------------------------------------------------------------------

def parse_day(arg, now):
    """'', today, tomorrow, YYYY-MM-DD, DD/MM, DD-MM or a weekday name -> ISO date within the forecast range."""
    a, today = arg.strip().lower(), now.date()
    if a in ("today", "hoy"):
        d = today
    elif a in ("tomorrow", "mañana", "manana"):
        d = today + timedelta(days=1)
    elif a in WEEKDAYS:  # the next such day, starting tomorrow
        d = today + timedelta(days=(WEEKDAYS[a] - today.weekday() - 1) % 7 + 1)
    elif m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", a):
        d = date(*map(int, m.groups()))
    elif m := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", a):
        day, month = map(int, m.groups())
        d = date(today.year, month, day)
        if d < today:
            d = date(today.year + 1, month, day)
    else:
        raise ValueError(f"I don't understand '{arg}'. Try 2026-11-14, 14/11, sat, monday, tomorrow.")
    last = today + timedelta(days=ride.FORECAST_DAYS - 1)
    if not today <= d <= last:
        raise ValueError(f"{d} is outside the forecast: today to {last}.")
    return d.isoformat()


def chart_reply(rep):
    caption = (rep["summary"] or {}).get("verdict", rep["day"])
    try:
        return ("photo", charts.render(rep), f"{rep['label']} · {rep['day']}: {caption}")
    except Exception as e:  # matplotlib missing or broken: say so, still give something
        print(f"chart failed: {type(e).__name__}: {e}", file=sys.stderr)
        return ("text", f"No chart available ({type(e).__name__}). Score per hour 00-23:\n{charts.sparkline(rep['rows'])}")


def handle(state, text):
    """One incoming message -> list of replies: ("text", str) or ("photo", png bytes, caption)."""
    cmd, _, arg = text.strip().partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    try:
        if cmd in ("/start", "/help"):
            return [("text", HELP)]
        if cmd in ("/today", "/tomorrow", "/date"):
            if cmd == "/date" and not arg:
                return [("text", "Which day? e.g. /date 2026-11-14, /date sat")]
            day = parse_day(arg if cmd == "/date" else cmd[1:], state.now())
            return [("text", state.report(day)["text"])]
        if cmd == "/chart":
            now = state.now()
            return [chart_reply(state.report(parse_day(arg, now) if arg else ride.target_day(now)))]
        if cmd == "/place":
            if not arg:
                return [("text", f"Place: {ride.place_label(state.place())}")]
            name, _, cc = (p.strip() for p in arg.partition(","))
            g = ride.geocode(name, cc or None)
            state.data["g"] = g
            state.save()
            return [("text", f"Place set to {ride.place_label(g)} ({g['timezone']}).")]
        if cmd == "/trips":
            if not arg:
                return [("text", "Trips: " + (state.trips_spec() or "none"))]
            spec = ride.DEFAULT_TRIPS if arg.lower() == "default" else arg
            ride.parse_trips(spec)  # raises on bad input before anything is saved
            state.data["trips"] = spec
            state.save()
            return [("text", "Trips set: " + (spec if spec.lower() != "none" else "none"))]
        return [("text", "I don't know that one.\n" + HELP)]
    except (ValueError, LookupError) as e:  # the user's mistake: tell them plainly
        return [("text", str(e))]
    except Exception as e:  # ours or the network's: say it failed, keep running
        print(f"command {cmd} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return [("text", f"Something broke ({type(e).__name__}). Try again in a minute.")]


# ---- Telegram ------------------------------------------------------------------------------

class Telegram:
    def __init__(self, token, chat_id, base=None):
        self.url = f"{base or os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org')}/bot{token}/"
        self.chat_id = int(chat_id)

    def call(self, method, params=None, photo=None, timeout=45):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if photo:  # multipart by hand: stdlib has no encoder
            b = uuid.uuid4().hex
            body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in params.items())
            body += (f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
                     f'Content-Type: image/png\r\n\r\n').encode() + photo + f"\r\n--{b}--\r\n".encode()
            req = urllib.request.Request(self.url + method, body, {"Content-Type": f"multipart/form-data; boundary={b}", **ride.UA})
        else:
            req = urllib.request.Request(self.url + method, urllib.parse.urlencode(params).encode(), ride.UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
        if not out.get("ok"):
            raise RuntimeError(f"telegram {method}: {out.get('description')}")
        return out["result"]

    def send(self, reply):
        if reply[0] == "photo":
            self.call("sendPhoto", {"chat_id": self.chat_id, "caption": reply[2][:1000]}, photo=reply[1])
        else:
            self.call("sendMessage", {"chat_id": self.chat_id, "text": reply[1][:4000]})


def push_due(now, last_push, push_time):
    """Nightly push: once per local day, as soon as the clock passes push_time (also late, after a restart)."""
    return now.time() >= push_time and last_push != now.date().isoformat()


def push(state, tg):
    now = state.now()
    day = (now.date() + timedelta(days=1)).isoformat()
    rep = state.report(day)
    tg.send(("text", rep["text"]))
    try:
        tg.send(chart_reply(rep))
    except Exception as e:
        print(f"chart push failed: {type(e).__name__}", file=sys.stderr)
    state.data["last_push"] = now.date().isoformat()
    state.save()


def serve(state, tg, push_time, once=False):
    offset, retry_at, backoff = None, 0, 5
    while True:
        try:
            if state.data.get("g") or os.environ.get("RIDE_PLACE"):
                now = state.now()
                if push_due(now, state.data.get("last_push"), push_time) and time.time() >= retry_at:
                    try:
                        push(state, tg)
                    except Exception as e:  # retry in a few minutes, keep answering commands meanwhile
                        retry_at = time.time() + PUSH_RETRY_SECONDS
                        print(f"push failed: {type(e).__name__}: {e}", file=sys.stderr)
            for u in tg.call("getUpdates", {"offset": offset, "timeout": POLL_SECONDS}, timeout=POLL_SECONDS + 15):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if m.get("chat", {}).get("id") != tg.chat_id:
                    print("ignored a message from another chat", file=sys.stderr)
                    continue
                if m.get("text", "").startswith("/"):
                    for reply in handle(state, m["text"]):
                        tg.send(reply)
            backoff = 5
        except Exception as e:  # network or Telegram down: log the type only (the URL carries the token), back off
            print(f"poll failed: {type(e).__name__}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        if once:
            return


def main():
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        sys.exit("set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
    h, _, m = os.environ.get("RIDE_PUSH_TIME", "21:00").partition(":")
    serve(State(), Telegram(token, chat), dtime(int(h), int(m or 0)))


if __name__ == "__main__":
    main()
