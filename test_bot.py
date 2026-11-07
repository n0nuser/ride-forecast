import contextlib, io, json, os, tempfile, threading
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo
import bot, charts, ride
from ride import Hour

OK = Hour(22, 50, 0, 0.0, 10, 0)
G = {"name": "Salamanca", "admin1": "Castille and León", "country_code": "ES", "timezone": "Europe/Madrid", "latitude": 1, "longitude": 1}
tz = ZoneInfo("Europe/Madrid")
TMP = tempfile.mkdtemp()
now_real = datetime.now(tz)
sources = {n: {f"{(now_real.date() + timedelta(days=d)).isoformat()}T{h:02d}:00": OK for d in range(8) for h in range(24)} for n in ("a", "b", "c")}
ride.fetch_sources = lambda g: sources
ride.geocode = lambda p, c=None: {**G, "name": p, "country_code": (c or "ES").upper()}
os.environ.pop("RIDE_PLACE", None)
quiet = contextlib.redirect_stderr(io.StringIO())


def new_state(**data):
    s = bot.State(os.path.join(TMP, f"s{len(os.listdir(TMP))}.json"))
    s.data.update(data)
    return s


# --- parse_day
now = datetime(2026, 11, 9, 10, 0, tzinfo=tz)   # a Monday
pd = lambda a: bot.parse_day(a, now)
assert pd("today") == "2026-11-09" and pd("tomorrow") == "2026-11-10" and pd("mañana") == "2026-11-10"
assert pd("sat") == "2026-11-14" and pd("monday") == "2026-11-16" and pd("viernes") == "2026-11-13"   # weekday: next one, from tomorrow
assert pd("14/11") == "2026-11-14" and pd("2026-11-14") == "2026-11-14" and pd("16-11") == "2026-11-16"
for bad in ("", "garbage", "99/99", "2026-11-08", "2026-11-17", "01/01"):   # empty, junk, bad date, yesterday, past the horizon, next year
    try:
        pd(bad); raise SystemExit(f"expected error: {bad!r}")
    except ValueError:
        pass

# --- handle: every command, state persistence, bad input leaves state alone
s = new_state(g=G)
text = lambda r: r[0][1]
assert "/chart" in text(bot.handle(s, "/help")) and text(bot.handle(s, "/start")) == bot.HELP and "/help" in text(bot.handle(s, "/nope"))
assert "Salamanca" in text(bot.handle(s, "/today")) and "work" in text(bot.handle(s, "/tomorrow@my_bot"))
assert text(bot.handle(s, "/date")).startswith("Which day")
assert "outside the forecast" in text(bot.handle(s, "/date 2030-01-01")) and "don't understand" in text(bot.handle(s, "/date blah"))
assert text(bot.handle(s, "/place")) == "Place: Salamanca, Castille and León, ES"
assert "Place set to Leon" in text(bot.handle(s, "/place Leon, es")) and bot.State(s.path).data["g"]["name"] == "Leon"   # persisted
assert bot.State(s.path).data["g"]["country_code"] == "ES"
ride_geo = ride.geocode
ride.geocode = lambda p, c=None: (_ for _ in ()).throw(LookupError(f"place not found: {p}"))
assert "place not found" in text(bot.handle(s, "/place Nowhere")) and bot.State(s.path).data["g"]["name"] == "Leon"   # failed change keeps the old place
ride.geocode = ride_geo
assert text(bot.handle(s, "/trips")).startswith("Trips: work 07:30")
assert "Trips set" in text(bot.handle(s, "/trips gym 18:00-20:00 mon,wed")) and bot.State(s.path).data["trips"] == "gym 18:00-20:00 mon,wed"
assert "bad trip" in text(bot.handle(s, "/trips nonsense")) and bot.State(s.path).data["trips"] == "gym 18:00-20:00 mon,wed"
assert "Trips set: none" in text(bot.handle(s, "/trips none")) and "Trips set" in text(bot.handle(s, "/trips default"))
assert bot.State(s.path).trips_spec() == ride.DEFAULT_TRIPS
os.environ["RIDE_TRIPS"] = "x 6-7"
assert new_state().trips_spec() == "x 6-7"   # env is the default when nothing is saved
os.environ.pop("RIDE_TRIPS")
try:
    new_state().place(); raise SystemExit("expected LookupError")   # no place anywhere
except LookupError:
    pass
os.environ["RIDE_PLACE"], os.environ["RIDE_COUNTRY"] = "Valladolid", "ES"
assert new_state().place()["name"] == "Valladolid"
os.environ.pop("RIDE_PLACE"); os.environ.pop("RIDE_COUNTRY")
broken = new_state(g=G)
ride.fetch_sources = lambda g: (_ for _ in ()).throw(RuntimeError("down"))
with quiet:
    assert "Something broke" in text(bot.handle(broken, "/today"))   # an outage is reported, not raised
ride.fetch_sources = lambda g: sources

# --- chart: a real PNG, or a text fallback when matplotlib is missing
r = bot.handle(s, "/chart")[0]
assert (r[0] == "photo" and r[1][:8] == b"\x89PNG\r\n\x1a\n" and s.place()["name"] in r[2]) or r[0] == "text"
assert bot.handle(s, "/chart tomorrow")[0][0] in ("photo", "text")
real_render = charts.render
charts.render = lambda rep: (_ for _ in ()).throw(ImportError("no matplotlib"))
with quiet:
    fb = bot.handle(s, "/chart")[0]
assert fb[0] == "text" and "No chart available" in fb[1] and "█" in fb[1]
charts.render = real_render

# --- push timing: once per local day, also late, never before the time
assert not bot.push_due(datetime(2026, 11, 9, 20, 59, tzinfo=tz), None, dtime(21, 0))
assert bot.push_due(datetime(2026, 11, 9, 21, 0, tzinfo=tz), None, dtime(21, 0))
assert bot.push_due(datetime(2026, 11, 9, 23, 30, tzinfo=tz), "2026-11-08", dtime(21, 0))      # missed (server down): late is fine
assert not bot.push_due(datetime(2026, 11, 9, 23, 30, tzinfo=tz), "2026-11-09", dtime(21, 0))  # already sent today
assert not bot.push_due(datetime(2026, 11, 10, 0, 30, tzinfo=tz), "2026-11-09", dtime(21, 0))  # after midnight, before 21:00


# --- the bot against a stub Telegram API: auth, commands, photo upload, push, outages
class Stub(BaseHTTPRequestHandler):
    queue, sent, fail = [], [], False

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        method = self.path.rsplit("/", 1)[1]
        if Stub.fail:
            self.send_response(500); self.end_headers(); return
        if method == "getUpdates":
            out, Stub.queue[:] = list(Stub.queue), []
        else:
            Stub.sent.append((method, self.headers["Content-Type"], body)); out = {"message_id": 1}
        data = json.dumps({"ok": True, "result": out}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    log_message = lambda *a: None


srv = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
tg = bot.Telegram("TOK", "42", base=f"http://127.0.0.1:{srv.server_port}")
bot.POLL_SECONDS = 0
upd = lambda i, chat, txt: {"update_id": i, "message": {"chat": {"id": chat}, "text": txt}}
st = new_state(g=G, last_push=now_real.date().isoformat())   # push already done today: only commands

Stub.queue[:] = [upd(1, 999, "/today"), upd(2, 42, "/today"), upd(3, 42, "hello, no slash"), upd(4, 42, "/chart")]
with quiet:
    bot.serve(st, tg, dtime(23, 59), once=True)
methods = [m for m, _, _ in Stub.sent]
assert methods[0] == "sendMessage" and methods[1] in ("sendPhoto", "sendMessage") and len(methods) == 2, methods   # stranger and chatter ignored
assert b"Salamanca" in Stub.sent[0][2] and b"chat_id=42" in Stub.sent[0][2]
if methods[1] == "sendPhoto":
    ctype, body = Stub.sent[1][1], Stub.sent[1][2]
    assert ctype.startswith("multipart/form-data; boundary=") and b"\x89PNG" in body and b'name="caption"' in body and b"42" in body

Stub.sent.clear()   # the nightly push: report plus chart, then remembered
st2 = new_state(g=G)
with quiet:
    bot.serve(st2, tg, dtime(0, 0), once=True)
assert [m for m, _, _ in Stub.sent][0] == "sendMessage" and len(Stub.sent) == 2
assert bot.State(st2.path).data["last_push"] == st2.now().date().isoformat()
Stub.sent.clear()
with quiet:
    bot.serve(st2, tg, dtime(0, 0), once=True)
assert Stub.sent == []   # not twice in a day

Stub.fail = True   # Telegram down: the loop survives, backs off, and the push is retried later
bot_sleep = bot.time.sleep
bot.time.sleep = lambda s: None
st3 = new_state(g=G)
with quiet:
    bot.serve(st3, tg, dtime(0, 0), once=True)
assert "last_push" not in st3.data
bot.time.sleep = bot_sleep
Stub.fail = False
print("ok")
