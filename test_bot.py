import contextlib, io, json, os, socket, tempfile, threading, time, urllib.parse
from datetime import datetime, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo
import bot, charts, ride
from bot import Text, Photo
from ride import Hour

OK = Hour(22, 50, 0, 0.0, 10, 0)
G = {"name": "Salamanca", "admin1": "Castille and León", "country_code": "ES", "timezone": "Europe/Madrid", "latitude": 1, "longitude": 1}
tz = ZoneInfo("Europe/Madrid")
TMP = tempfile.mkdtemp()
now_real = datetime.now(tz)
sources = {n: {f"{(now_real.date() + timedelta(days=d)).isoformat()}T{h:02d}:00": OK for d in range(8) for h in range(24)} for n in ("a", "b", "c")}
fetches = []
ride.fetch_sources = lambda g: fetches.append(1) or sources
ride.geocode = lambda p, c=None: {**G, "name": p, "country_code": (c or "ES").upper()}
for k in ("RIDE_PLACE", "RIDE_COUNTRY", "RIDE_TRIPS", "NOTIFY_SOCKET"):
    os.environ.pop(k, None)
TRIPS = "work 07:30-09:00 mon-fri;home 17:00-19:00 mon-fri"   # test data: the code has no built-in trips


def capture():
    buf = io.StringIO()
    return buf, contextlib.redirect_stderr(buf)


def new_state(**data):
    s = bot.State(os.path.join(TMP, f"s{len(os.listdir(TMP))}.json"))
    s.data.update(data)
    return s


text = lambda r: r[0].text

# --- parse_day: <day> takes today, tomorrow, weekdays, ISO dates, DD/MM
now = datetime(2026, 11, 9, 10, 0, tzinfo=tz)   # a Monday
pd = lambda a: bot.parse_day(a, now)
assert pd("today") == "2026-11-09" and pd("tomorrow") == "2026-11-10" and pd("mañana") == "2026-11-10" and pd("hoy") == "2026-11-09"
assert pd("TODAY") == "2026-11-09" and pd("  tomorrow ") == "2026-11-10"
assert pd("sat") == "2026-11-14" and pd("monday") == "2026-11-16" and pd("viernes") == "2026-11-13"   # weekday: next one, from tomorrow
assert pd("14/11") == "2026-11-14" and pd("2026-11-14") == "2026-11-14" and pd("16-11") == "2026-11-16"
for bad in ("", "garbage", "99/99", "31/04", "29/02", "2026-02-30", "2026-11-08", "2026-11-17", "01/01"):   # junk, not a date, yesterday, past the horizon, next year
    try:
        pd(bad); raise SystemExit(f"expected error: {bad!r}")
    except ride.UserError:
        pass

# --- handle: every command, today and tomorrow everywhere, state persistence, bad input leaves state alone
s = new_state(g=G, trips=TRIPS)
assert "today, tomorrow" in bot.HELP and "/chart" in text(bot.handle(s, "/help")) and text(bot.handle(s, "/start")) == bot.HELP and "/help" in text(bot.handle(s, "/nope"))
for cmd in ("/today", "/tomorrow", "/tomorrow@my_bot", "/date today", "/date tomorrow", "/date\nsat", "/date  \t sat"):
    assert "Salamanca" in text(bot.handle(s, cmd)), cmd
assert text(bot.handle(s, "/date")).startswith("Which day")
assert "outside the forecast" in text(bot.handle(s, "/date 2030-01-01")) and "don't understand" in text(bot.handle(s, "/date blah"))
assert "work" in text(bot.handle(s, "/date monday")) or "home" in text(bot.handle(s, "/date monday"))
for day_arg in ("today", "tomorrow", "sat"):
    assert bot.handle(s, f"/chart {day_arg}")[0].__class__ in (Photo, Text)
assert text(bot.handle(s, "/place")) == "Place: Salamanca, Castille and León, ES"
assert "Place set to Leon" in text(bot.handle(s, "/place Leon, es")) and bot.State(s.path).data["g"]["name"] == "Leon"   # persisted
assert bot.State(s.path).data["g"]["country_code"] == "ES"
real_geo = ride.geocode
ride.geocode = lambda p, c=None: (_ for _ in ()).throw(ride.UserError(f"place not found: {p}"))
assert "place not found" in text(bot.handle(s, "/place Nowhere")) and bot.State(s.path).data["g"]["name"] == "Leon"   # failed change keeps the old place
ride.geocode = real_geo
s.update(last_push="2026-11-08")
bot.handle(s, "/place Madrid")
assert "last_push" not in bot.State(s.path).data   # a new place clears it, so tonight's push is not skipped across time zones
assert "Trips set" in text(bot.handle(s, "/trips gym 18:00-20:00 mon,wed")) and bot.State(s.path).data["trips"] == "gym 18:00-20:00 mon,wed"
assert "bad trip" in text(bot.handle(s, "/trips nonsense")) and bot.State(s.path).data["trips"] == "gym 18:00-20:00 mon,wed"

# --- trips are local: nothing built in; the environment is only a default; the chat can override or reset
fresh = new_state(g=G)
assert fresh.trips_spec() == "" and text(bot.handle(fresh, "/trips")).startswith("Trips: none") and "No trips set" in text(bot.handle(fresh, "/trips"))
assert text(bot.handle(fresh, "/today")).endswith(bot.NO_TRIPS) and "Whole day 08:00-20:00" in text(bot.handle(fresh, "/today"))   # the day line, plus a hint how to add trips
assert "No trips set" not in text(bot.handle(s, "/today"))
os.environ["RIDE_TRIPS"] = "x 06:00-07:00"
env_only = new_state(g=G)
assert env_only.trips_spec() == "x 06:00-07:00" and "x 06:00" in text(bot.handle(env_only, "/today"))
bot.handle(env_only, "/trips none")
assert env_only.trips_spec() == "none" and "No trips set" in text(bot.handle(env_only, "/today"))
assert "reset to the server's setting: x 06:00-07:00" in text(bot.handle(env_only, "/trips reset")) and env_only.trips_spec() == "x 06:00-07:00"
os.environ.pop("RIDE_TRIPS")
assert "none" in text(bot.handle(env_only, "/trips reset"))   # no env either: back to no trips
try:
    new_state().place(); raise SystemExit("expected UserError")   # no place anywhere
except ride.UserError:
    pass
os.environ["RIDE_PLACE"], os.environ["RIDE_COUNTRY"] = "Valladolid", "ES"
assert new_state().place()["name"] == "Valladolid" and new_state().has_place()
os.environ.pop("RIDE_PLACE"); os.environ.pop("RIDE_COUNTRY")

# --- our faults are not "your mistake": a changed API shape reports a generic failure and logs a traceback
broken = new_state(g=G)
for exc in (RuntimeError("down"), KeyError("hourly"), json.JSONDecodeError("bad", "", 0)):
    ride.fetch_sources = lambda g, e=exc: (_ for _ in ()).throw(e)
    buf, ctx = capture()
    with ctx:
        out = text(bot.handle(broken, "/today"))
    assert out.startswith("Something broke") and "hourly" not in out and "Traceback" in buf.getvalue(), (exc, out)
ride.fetch_sources = lambda g: fetches.append(1) or sources

# --- the weather cache works, and expires
st_c = new_state(g=G, trips=TRIPS)
fetches.clear()
bot.handle(st_c, "/today"); bot.handle(st_c, "/tomorrow"); bot.handle(st_c, "/date sat")
assert len(fetches) == 1
bot.CACHE_SECONDS = -1
bot.handle(st_c, "/today")
assert len(fetches) == 2
bot.CACHE_SECONDS = 600

# --- state: corrupt or odd files are survived; a refusing disk changes nothing
for junk in ("null", "[]", "42", "{broken", ""):
    p = os.path.join(TMP, "junk.json"); open(p, "w").write(junk)
    assert bot.State(p).data == {}, junk
ro_dir = os.path.join(TMP, "ro"); os.mkdir(ro_dir)
ro = bot.State(os.path.join(ro_dir, "state.json")); ro.data.update(g=G); os.chmod(ro_dir, 0o500)
try:
    if os.access(ro_dir, os.W_OK):
        raise SystemExit("cannot make a read-only dir here")
    buf, ctx = capture()
    with ctx:
        out = text(bot.handle(ro, "/place Elsewhere"))
        bot.handle(ro, "/trips a 8-9")
    assert out.startswith("Something broke") and ro.data["g"]["name"] == "Salamanca" and "trips" not in ro.data   # not half-applied
    with capture()[1]:
        ro.mark_pushed("2026-11-09")   # a failed save is logged, not raised, and the push is remembered in memory
    assert ro.data["last_push"] == "2026-11-09"
finally:
    os.chmod(ro_dir, 0o700)

# --- Telegram limits are counted in UTF-16 units, not characters
for n in (3000, 5000):
    out = bot.clip("😀" * n, bot.TEXT_LIMIT)
    assert len(out.encode("utf-16-le")) // 2 <= bot.TEXT_LIMIT and len(out) == 2000
assert bot.clip("abc", 10) == "abc" and len(bot.clip("é" * 5000, 4000)) == 4000

# --- chart: a real PNG when matplotlib is there, a text fallback when it is not or breaks; a failed render closes its figure
import importlib.util
have_mpl = importlib.util.find_spec("matplotlib") is not None
r = bot.handle(s, "/chart")[0]
if have_mpl:
    assert isinstance(r, Photo) and r.png[:8] == b"\x89PNG\r\n\x1a\n" and s.place()["name"] in r.caption, r   # strict: no silent fallback
else:
    assert isinstance(r, Text)
real_render = charts.render
charts.render = lambda rep: (_ for _ in ()).throw(ImportError("no matplotlib"))
buf, ctx = capture()
with ctx:
    fb = bot.handle(s, "/chart")[0]
assert isinstance(fb, Text) and "No chart available" in fb.text and "█" in fb.text
charts.render = real_render
if have_mpl:
    import matplotlib.pyplot as plt
    real_draw = charts._draw
    charts._draw = lambda *a: (_ for _ in ()).throw(ValueError("draw failed"))
    try:
        charts.render(ride.build_report(G, now_real.date().isoformat(), sources, []))
    except ValueError:
        pass
    assert plt.get_fignums() == [], "a failed render leaked its figure"
    charts._draw = real_draw

# --- week: one line per day, and a week chart
w = bot.handle(s, "/week")[0]
assert isinstance(w, Text) and w.text.count("/100") + w.text.count("not enough data") == 7, w.text
wc = bot.handle(s, "/chart week")[0]
assert isinstance(wc, Photo if have_mpl else Text), wc
if have_mpl:
    assert wc.png[:8] == b"\x89PNG\r\n\x1a\n"

# --- push timing: once per local day, also late, never before the time
assert not bot.push_due(datetime(2026, 11, 9, 20, 59, tzinfo=tz), None, dtime(21, 0))
assert bot.push_due(datetime(2026, 11, 9, 21, 0, tzinfo=tz), None, dtime(21, 0))
assert bot.push_due(datetime(2026, 11, 9, 23, 30, tzinfo=tz), "2026-11-08", dtime(21, 0))      # missed (server down): late is fine
assert not bot.push_due(datetime(2026, 11, 9, 23, 30, tzinfo=tz), "2026-11-09", dtime(21, 0))  # already sent today
assert not bot.push_due(datetime(2026, 11, 10, 0, 30, tzinfo=tz), "2026-11-09", dtime(21, 0))  # after midnight, before 21:00


# --- the bot against a stub Telegram API
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
tg = bot.Telegram("123456:TOK", "42", base=f"http://127.0.0.1:{srv.server_port}")
bot.POLL_SECONDS = 0
upd = lambda i, chat, txt: {"update_id": i, "message": {"chat": {"id": chat}, "text": txt}}
done_today = dict(g=G, trips=TRIPS, last_push=now_real.date().isoformat())   # push already done today: only commands
raw = lambda: [b.decode(errors="replace") for _, _, b in Stub.sent]
bodies = lambda: [urllib.parse.parse_qs(r).get("text", [r])[0] if not c.startswith("multipart") else r for r, (_, c, _) in zip(raw(), Stub.sent)]

# auth: strangers, string ids, edits and chatter get no reply; the stranger is logged once
Stub.queue[:] = [upd(1, 999, "/today"), upd(2, 999, "/today"), upd(3, "42", "/today"),
                 {"update_id": 4, "edited_message": {"chat": {"id": 42}, "text": "/today"}},
                 {"update_id": 5, "callback_query": {}}, upd(6, 42, "hello, no slash"), upd(7, 42, "/today")]
buf, ctx = capture()
with ctx:
    bot.serve(new_state(**done_today), tg, dtime(23, 59), loops=1)
assert [m for m, _, _ in Stub.sent] == ["sendMessage"] and "chat_id=42" in raw()[0] and "Salamanca" in bodies()[0]
assert buf.getvalue().count("ignored a message from chat 999") == 1 and "chat None" not in buf.getvalue()

# photo upload is multipart with a PNG and a caption
Stub.sent.clear(); Stub.queue[:] = [upd(1, 42, "/chart")]
with capture()[1]:
    bot.serve(new_state(**done_today), tg, dtime(23, 59), loops=1)
if have_mpl:
    m, ctype, body = Stub.sent[0]
    assert m == "sendPhoto" and ctype.startswith("multipart/form-data; boundary=") and b"\x89PNG" in body and b'name="caption"' in body

# a long emoji reply fits Telegram's limit
Stub.sent.clear(); Stub.queue[:] = [upd(1, 42, "/date " + "😀" * 3000)]
with capture()[1]:
    bot.serve(new_state(**done_today), tg, dtime(23, 59), loops=1)
sent_text = urllib.parse.parse_qs(Stub.sent[0][2].decode())["text"][0]
assert len(sent_text.encode("utf-16-le")) // 2 <= 4096 and sent_text.startswith("I don't understand")   # echoed input is clipped to what Telegram accepts

# a bad RIDE_PLACE must not make the bot deaf: /place still arrives and fixes it
Stub.sent.clear(); Stub.queue[:] = [upd(1, 42, "/place Salamanca")]
os.environ["RIDE_PLACE"] = "Nowhere"
ride.geocode = lambda p, c=None: (_ for _ in ()).throw(ride.UserError("place not found")) if p == "Nowhere" else {**G, "name": p}
deaf = new_state()
with capture()[1]:
    bot.serve(deaf, tg, dtime(0, 0), loops=1)
os.environ.pop("RIDE_PLACE")
ride.geocode = real_geo
assert any("Place set to Salamanca" in b for b in bodies()) and deaf.data["g"]["name"] == "Salamanca"
assert Stub.queue == []

# the nightly push: tomorrow's report then the chart, remembered, not repeated
Stub.sent.clear()
st2 = new_state(g=G, trips=TRIPS)
with capture()[1]:
    bot.serve(st2, tg, dtime(0, 0), loops=1)
tomorrow = (st2.now().date() + timedelta(days=1)).strftime("%a %d %b")
assert Stub.sent[0][0] == "sendMessage" and tomorrow in bodies()[0] and len(Stub.sent) == 2
assert bot.State(st2.path).data["last_push"] == st2.now().date().isoformat()
Stub.sent.clear()
with capture()[1]:
    bot.serve(st2, tg, dtime(0, 0), loops=2)
assert Stub.sent == []

# a chart failure after the text must not repeat the text on the next loop
charts.render = lambda rep: (_ for _ in ()).throw(RuntimeError("no chart"))
Stub.sent.clear(); st4 = new_state(g=G, trips=TRIPS)
with capture()[1]:
    bot.serve(st4, tg, dtime(0, 0), loops=3)
charts.render = real_render
assert [m for m, _, _ in Stub.sent].count("sendMessage") == 2 and st4.data["last_push"]   # push text + the chart's text fallback, once

# a failed save after the send must not cause a resend every few minutes
Stub.sent.clear()
ro_dir2 = os.path.join(TMP, "ro2"); os.mkdir(ro_dir2)
st5 = bot.State(os.path.join(ro_dir2, "state.json")); st5.data.update(g=G, trips=TRIPS); os.chmod(ro_dir2, 0o500)
bot.PUSH_RETRY_SECONDS = 0
try:
    with capture()[1]:
        bot.serve(st5, tg, dtime(0, 0), loops=3)
finally:
    os.chmod(ro_dir2, 0o700)
    bot.PUSH_RETRY_SECONDS = 300
assert [m for m, _, _ in Stub.sent].count("sendMessage") <= 2 and sum("Forecast" in b or "Salamanca" in b for b in bodies()) <= 2

# push fails: not hammered (one attempt per retry window), then one notice after three failures, and the log never has the token
attempts = []
ride.fetch_sources = lambda g: attempts.append(1) or (_ for _ in ()).throw(RuntimeError("boom https://api.telegram.org/bot123456:TOKENSECRET/x"))
Stub.sent.clear(); st6a = new_state(g=G, trips=TRIPS)
with capture()[1]:
    bot.serve(st6a, tg, dtime(0, 0), loops=5)
assert len(attempts) == 1, len(attempts)      # PUSH_RETRY_SECONDS is 300: the next try is minutes away, not the next loop
Stub.sent.clear(); st6 = new_state(g=G, trips=TRIPS)
bot.PUSH_RETRY_SECONDS = 0
buf, ctx = capture()
with ctx:
    bot.serve(st6, tg, dtime(0, 0), loops=5)
bot.PUSH_RETRY_SECONDS = 300
ride.fetch_sources = lambda g: fetches.append(1) or sources
notices = [b for b in bodies() if "nightly forecast failed" in b]
assert len(notices) == 1 and "failed 3 times" in notices[0] and "last_push" not in st6.data and "TOKENSECRET" not in buf.getvalue()

# no place anywhere: no push attempts, no failure notices, and the commands still arrive
for k in ("RIDE_PLACE", "RIDE_COUNTRY"):
    os.environ.pop(k, None)
Stub.sent.clear(); Stub.queue[:] = [upd(1, 42, "/place Salamanca")]
bot.PUSH_RETRY_SECONDS = 0
with capture()[1]:
    bot.serve(new_state(), tg, dtime(0, 0), loops=4)
bot.PUSH_RETRY_SECONDS = 300
assert not any("nightly forecast failed" in b for b in bodies()) and any("Place set" in b for b in bodies())

# push sends tomorrow's day, not today's
seen = []
st7 = new_state(g=G, trips=TRIPS); real_report = st7.report
st7.report = lambda day: seen.append(day) or real_report(day)
with capture()[1]:
    bot.serve(st7, tg, dtime(0, 0), loops=1)
assert seen == [(st7.now().date() + timedelta(days=1)).isoformat()]

# a slow command is cut off, answered, and the loop goes on
real_handle, real_timeout = bot.handle, bot.COMMAND_TIMEOUT
bot.handle, bot.COMMAND_TIMEOUT = lambda st, t: time.sleep(5), 1
Stub.sent.clear(); Stub.queue[:] = [upd(1, 42, "/today")]
t0 = time.time()
with capture()[1]:
    bot.serve(new_state(**done_today), tg, dtime(23, 59), loops=1)
bot.handle, bot.COMMAND_TIMEOUT = real_handle, real_timeout
assert time.time() - t0 < 4 and any("took too long" in b for b in bodies())

# the watchdog: READY once, then a ping per loop
sock_path = os.path.join(TMP, "notify.sock")
rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); rx.bind(sock_path); rx.settimeout(2)
os.environ["NOTIFY_SOCKET"] = sock_path
with capture()[1]:
    bot.serve(new_state(**done_today), tg, dtime(23, 59), loops=2)
pings = []
rx.settimeout(0.2)
try:
    while True:
        pings.append(rx.recv(100))
except socket.timeout:
    pass
os.environ.pop("NOTIFY_SOCKET")
assert pings == [b"READY=1", b"WATCHDOG=1", b"WATCHDOG=1"], pings

# Telegram down: the loop survives and backs off
Stub.fail = True
sleeps = []
real_sleep = bot.time.sleep
bot.time.sleep = sleeps.append
with capture()[1]:
    bot.serve(new_state(g=G, trips=TRIPS), tg, dtime(0, 0), loops=3)
bot.time.sleep = real_sleep
Stub.fail = False
assert sleeps == [5, 10, 20], sleeps

# --- startup validation: config mistakes exit 2 (systemd will not restart them), good config reaches serve()
def run_main(**env):
    base = {"TELEGRAM_TOKEN": "123456:ABC-def_1", "TELEGRAM_CHAT_ID": "42"}
    for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "RIDE_PUSH_TIME", "RIDE_TRIPS"):
        os.environ.pop(k, None)
    os.environ.update({**base, **env})
    called = []
    real_serve, bot.serve = bot.serve, lambda *a, **k: called.append(a)
    try:
        with capture()[1]:
            bot.main()
        return called
    except SystemExit as e:
        return e.code
    finally:
        bot.serve = real_serve
assert len(run_main()) == 1 and run_main(RIDE_PUSH_TIME="06:30")[0][2] == dtime(6, 30) and len(run_main(RIDE_TRIPS="a 8-9 mon")) == 1
assert run_main(TELEGRAM_TOKEN="123456:ABC # my bot") == 2 and run_main(TELEGRAM_CHAT_ID="@name") == 2 and run_main(TELEGRAM_TOKEN="") == 2
assert run_main(RIDE_PUSH_TIME="24:00") == 2 and run_main(RIDE_PUSH_TIME="soon") == 2 and run_main(RIDE_TRIPS="nonsense") == 2
for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "RIDE_PUSH_TIME", "RIDE_TRIPS"):
    os.environ.pop(k, None)
print("ok")
