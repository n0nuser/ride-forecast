#!/usr/bin/env python3
"""Telegram bot for ride-forecast: answers commands from one chat and pushes the nightly forecast.
Long-polls the Bot API, stdlib only (charts need matplotlib; without it you get a text sparkline).
Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (the only chat it answers), RIDE_PLACE, RIDE_COUNTRY, RIDE_TRIPS,
     RIDE_PUSH_TIME (HH:MM local, default 21:00), STATE_DIRECTORY (systemd sets it).
Commands: /today /tomorrow /date <day> /week /chart [day|week] /place [name[, CC]] /trips [spec|reset|none] /help
"""
import json, os, re, socket, sys, time, urllib.parse, urllib.request, uuid
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import charts, ride
from ride import UserError, log

HELP = """Commands:
/today, /tomorrow: verdict per trip
/date <day>: any day up to 7 ahead. <day> is today, tomorrow, a weekday (sat, monday, lunes), 2026-11-14 or 14/11
/week: one line per day for the next 7 days
/chart [day]: hourly graph of a day (default: the next day that matters). /chart week: the 7 days in one chart
/place [name, CC]: show or change the place, e.g. /place Leon, ES
/trips [spec]: show or change your trips, e.g. /trips work 08:00-09:00 mon-fri; out 20:00-22:00
  /trips reset goes back to the server's RIDE_TRIPS, /trips none removes them
/help: this text"""
NO_TRIPS = "\n\nNo trips set. Add yours: /trips NAME HH:MM-HH:MM [days], e.g. /trips work 08:00-09:00 mon-fri"
CACHE_SECONDS = 600
POLL_SECONDS = 30
COMMAND_TIMEOUT = 90       # s, one command or one push, whole
PUSH_RETRY_SECONDS = 300
PUSH_FAILURES_BEFORE_NOTICE = 3
TEXT_LIMIT, CAPTION_LIMIT = 4000, 1000  # Telegram allows 4096 and 1024 UTF-16 units
WEEKDAYS = {**ride.DAYS, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6}


class Text(NamedTuple):
    text: str


class Photo(NamedTuple):
    png: bytes
    caption: str


def clip(text, limit):
    """Cut to `limit` UTF-16 units, which is how Telegram counts (an emoji is 2)."""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


# ---- state ---------------------------------------------------------------------------------

class State:
    """Place, trips and last push, persisted as JSON. Env values are the defaults."""

    def __init__(self, path=None):
        base = Path(os.environ.get("STATE_DIRECTORY") or Path.home() / ".local/state/ride-forecast")
        self.path = Path(path) if path else base / "state.json"
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self.data = data if isinstance(data, dict) else {}
        self._cache = {}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def update(self, **changes):
        """Save first, apply after: if the disk refuses, nothing changed. A value of None removes the key."""
        new = {k: v for k, v in {**self.data, **changes}.items() if v is not None}
        self._write(new)
        self.data = new

    def mark_pushed(self, day):
        self.data["last_push"] = day  # in memory first: a failed save must not cause a second push today
        try:
            self._write(self.data)
        except OSError as e:
            log(f"could not save state: {type(e).__name__}")

    def has_place(self):
        return "g" in self.data or bool(os.environ.get("RIDE_PLACE"))

    def place(self):
        if "g" not in self.data:  # first use: resolve the env place once and remember it
            place = os.environ.get("RIDE_PLACE")
            if not place:
                raise UserError("no place set: send /place <name>")
            self.update(g=ride.geocode(place, os.environ.get("RIDE_COUNTRY")))
        return self.data["g"]

    def trips_spec(self):
        return self.data["trips"] if "trips" in self.data else os.environ.get("RIDE_TRIPS", "")

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
    """today, tomorrow, a weekday name, YYYY-MM-DD, DD/MM or DD-MM -> ISO date within the forecast range."""
    a, today = arg.strip().lower(), now.date()
    if a in ("today", "hoy"):
        d = today
    elif a in ("tomorrow", "mañana", "manana"):
        d = today + timedelta(days=1)
    elif a in WEEKDAYS:  # the next such day, starting tomorrow
        d = today + timedelta(days=(WEEKDAYS[a] - today.weekday() - 1) % 7 + 1)
    elif m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", a):
        d = _date(*map(int, m.groups()))
    elif m := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", a):
        day, month = map(int, m.groups())
        d = _date(today.year, month, day)
        if d < today:
            d = _date(today.year + 1, month, day)
    else:
        raise UserError(f"I don't understand '{arg}'. Try today, tomorrow, sat, monday, 2026-11-14 or 14/11.")
    last = today + timedelta(days=ride.FORECAST_DAYS - 1)
    if not today <= d <= last:
        raise UserError(f"{d} is outside the forecast: today to {last}.")
    return d.isoformat()


def _date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        raise UserError(f"{d:02d}/{m:02d}/{y} is not a date.")


def chart_reply(rep):
    caption = (rep["summary"] or {}).get("verdict", rep["day"])
    try:
        return Photo(charts.render(rep), f"{rep['label']} · {rep['day']}: {caption}")
    except Exception as e:  # matplotlib missing or broken: say so, still give something
        log(f"chart failed: {type(e).__name__}: {e}")
        return Text(f"No chart available ({type(e).__name__}). Score per hour 00-23:\n{charts.sparkline(rep['rows'])}")


def week_reports(state):
    g = state.place()
    return ride.week_reports(g, state.now().date(), state.sources(g), ride.parse_trips(state.trips_spec()))


def week_chart_reply(reps):
    try:
        return Photo(charts.render_week(reps), f"{reps[0]['label']} · next {len(reps)} days")
    except Exception as e:
        log(f"week chart failed: {type(e).__name__}: {e}")
        return Text(f"No chart available ({type(e).__name__}).\n" + "\n".join(ride.week_line(r) for r in reps))


def report_reply(state, day):
    return Text(state.report(day)["text"] + ("" if state.trips_spec().strip().lower() not in ("", "none") else NO_TRIPS))


def handle(state, text):
    """One incoming message -> list of replies (Text or Photo)."""
    parts = text.strip().split(None, 1)
    cmd, arg = parts[0].split("@")[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    try:
        if cmd in ("/start", "/help"):
            return [Text(HELP)]
        if cmd in ("/today", "/tomorrow", "/date"):
            if cmd == "/date" and not arg:
                return [Text("Which day? e.g. /date tomorrow, /date sat, /date 2026-11-14")]
            return [report_reply(state, parse_day(arg if cmd == "/date" else cmd[1:], state.now()))]
        if cmd in ("/week", "/semana"):
            reps = week_reports(state)
            return [Text(f"Next {len(reps)} days · {reps[0]['label']}\n" + "\n".join(ride.week_line(r) for r in reps))]
        if cmd == "/chart" and arg.lower() in ("week", "semana"):
            return [week_chart_reply(week_reports(state))]
        if cmd == "/chart":
            now = state.now()
            return [chart_reply(state.report(parse_day(arg, now) if arg else ride.target_day(now)))]
        if cmd == "/place":
            if not arg:
                return [Text(f"Place: {ride.place_label(state.place())}")]
            name, _, cc = (p.strip() for p in arg.partition(","))
            g = ride.geocode(name, cc or None)
            state.update(g=g, last_push=None)  # a new place may be in another time zone: let tonight's push happen
            return [Text(f"Place set to {ride.place_label(g)} ({g['timezone']}).")]
        if cmd == "/trips":
            if not arg:
                return [Text("Trips: " + (state.trips_spec().strip() or "none") + ("" if state.trips_spec().strip() else NO_TRIPS))]
            if arg.lower() == "reset":
                state.update(trips=None)
                return [Text("Trips reset to the server's setting: " + (state.trips_spec().strip() or "none"))]
            ride.parse_trips(arg)  # raises UserError on bad input before anything is saved
            state.update(trips=arg)
            return [Text("Trips set: " + (arg if arg.lower() != "none" else "none"))]
        return [Text("I don't know that one.\n" + HELP)]
    except UserError as e:  # the user's mistake: tell them plainly
        return [Text(str(e))]
    except Exception as e:  # ours or the network's: log it with a traceback, keep running
        log(f"command {cmd} failed: {type(e).__name__}: {e}", exc=True)
        return [Text(f"Something broke ({type(e).__name__}). Try again in a minute.")]


# ---- Telegram ------------------------------------------------------------------------------

class Telegram:
    def __init__(self, token, chat_id, base="https://api.telegram.org"):
        self.url = f"{base}/bot{token}/"
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
        if isinstance(reply, Photo):
            self.call("sendPhoto", {"chat_id": self.chat_id, "caption": clip(reply.caption, CAPTION_LIMIT)}, photo=reply.png)
        else:
            self.call("sendMessage", {"chat_id": self.chat_id, "text": clip(reply.text, TEXT_LIMIT)})


def notify(msg):
    """systemd readiness and watchdog pings (sd_notify). No-op outside systemd."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect("\0" + path[1:] if path[0] == "@" else path)
            s.send(msg.encode())
    except OSError as e:
        log(f"sd_notify failed: {type(e).__name__}")


def push_due(now, last_push, push_time):
    """Nightly push: once per local day, as soon as the clock passes push_time (also late, after a restart)."""
    return now.time() >= push_time and last_push != now.date().isoformat()


def push(state, tg):
    now = state.now()
    rep = state.report((now.date() + timedelta(days=1)).isoformat())
    tg.send(report_reply_from(state, rep))
    state.mark_pushed(now.date().isoformat())  # the text is out: a chart failure below must not repeat it
    try:
        tg.send(chart_reply(rep))
    except Exception as e:
        log(f"chart push failed: {type(e).__name__}")


def report_reply_from(state, rep):
    return Text(rep["text"] + ("" if state.trips_spec().strip().lower() not in ("", "none") else NO_TRIPS))


def serve(state, tg, push_time, loops=None):
    offset, retry_at, failures, strangers, backoff = None, 0, 0, set(), 5
    notify("READY=1")
    while True:
        notify("WATCHDOG=1")
        try:
            if state.has_place() and time.time() >= retry_at:  # its own try: a bad place must never stop the polling below
                try:
                    with ride.deadline(COMMAND_TIMEOUT):
                        if push_due(state.now(), state.data.get("last_push"), push_time):
                            push(state, tg)
                            failures = 0
                except Exception as e:
                    failures, retry_at = failures + 1, time.time() + PUSH_RETRY_SECONDS
                    log(f"push failed ({failures}): {type(e).__name__}: {e}")
                    if failures == PUSH_FAILURES_BEFORE_NOTICE:  # say so once, instead of a silent night
                        try:
                            tg.send(Text(f"The nightly forecast failed {failures} times ({type(e).__name__}). I keep trying."))
                        except Exception:
                            pass
            with ride.deadline(POLL_SECONDS + 60):
                updates = tg.call("getUpdates", {"offset": offset, "timeout": POLL_SECONDS}, timeout=POLL_SECONDS + 15)
            for u in updates:
                offset = u["update_id"] + 1
                m = u.get("message")
                if m is None:  # edits, channel posts, callbacks: not commands
                    continue
                cid = m.get("chat", {}).get("id")
                if cid != tg.chat_id:
                    if cid not in strangers:
                        strangers.add(cid)
                        log(f"ignored a message from chat {cid}")
                    continue
                if m.get("text", "").startswith("/"):
                    try:
                        with ride.deadline(COMMAND_TIMEOUT):
                            for reply in handle(state, m["text"]):
                                tg.send(reply)
                    except ride.Deadline:
                        tg.send(Text("That took too long. Try again in a minute."))
            backoff = 5
        except ride.Deadline as e:
            log(f"poll timed out: {e}")
        except Exception as e:  # network or Telegram down: back off and retry
            log(f"poll failed: {type(e).__name__}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        if loops is not None:
            loops -= 1
            if loops <= 0:
                return


def die(msg):
    print(f"config error: {msg}", file=sys.stderr)
    sys.exit(2)  # the unit's RestartPreventExitStatus=2 keeps systemd from restarting a config error forever


def main():
    token, chat = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat):
        die("set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
    if not re.fullmatch(r"\d+:[\w-]+", token):
        die("TELEGRAM_TOKEN looks malformed (no spaces or inline comments after the value in the env file)")
    if not re.fullmatch(r"-?\d+", chat):
        die("TELEGRAM_CHAT_ID must be a number, e.g. 123456789")
    try:
        h, _, m = os.environ.get("RIDE_PUSH_TIME", "21:00").partition(":")
        push_time = dtime(int(h), int(m or 0))
        ride.parse_trips(os.environ.get("RIDE_TRIPS", ""))
    except ValueError as e:
        die(f"RIDE_PUSH_TIME or RIDE_TRIPS: {e}")
    serve(State(), Telegram(token, chat), push_time)


if __name__ == "__main__":
    main()
