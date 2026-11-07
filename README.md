# ride-forecast

A script that looks at tomorrow's weather and tells you whether it's a good day to ride a motorbike. It runs at night, posts the verdict to a webhook and/or Telegram, and needs no API keys.

```
Great day to ride (85/100)
Salamanca, Castille and León, ES, 2026-11-08
Best window 13:00-16:00 (96/100)
7-19°C, rain 0.2 mm (up to 25% in some models), gusts 36 km/h, humidity 35-95%
6 sources, sources agree: MeteoFr 93 · GFS 91 · UKMO 84 · MET.no 83 · ECMWF 81 · ICON 78
```

## How it works

You give it a place name. It looks up the coordinates with the Open-Meteo geocoding API, then asks six forecast sources for the hourly forecast of the target day:

- Five Open-Meteo models: ECMWF, GFS, ICON, Météo-France and the UK Met Office.
- MET Norway, a separate API.

Weather apps disagree, so the script scores each source on its own and combines the scores instead of trusting one forecast. The five Open-Meteo models are different forecast models behind one API, so MET Norway is the only fully independent source.

### Scoring

Each riding hour (08:00 to 20:00, local time) gets a score from 0 to 100:

| Factor | Effect |
|---|---|
| Temperature | No penalty from 18 to 26 °C. It loses points as it gets colder or hotter. |
| Rain | Both the chance and the amount count. |
| Wind | Gusts over 40 km/h lose points. |
| Humidity | Above 80% loses points. Fog costs 20. |
| Dangerous hour | Thunderstorm, snow, freezing rain, gusts of 70 km/h or more, and ice risk (2 °C or colder with rain or 90% humidity) score 0. |

The score of an hour is the median across sources. The day score is the average of the hours, with three adjustments:

- A good 3-hour window can lift a mediocre day, but never above "mixed".
- If two or more sources report a dangerous hour, the day is capped at "marginal" and the message names the hour.
- If the sources are more than 30 points apart, the score is lowered, and the message says the sources disagree.

The result is a score and one of five verdicts instead of a yes or no:

| Score | Verdict |
|---|---|
| 80 and up | Great day to ride |
| 65 to 79 | Good day, ride |
| 50 to 64 | Mixed: ride in the best window |
| 35 to 49 | Marginal: short ride only if you must |
| Below 35 | Stay home |

The weights are judgment calls, not tuned values. They are constants at the top of `ride.py`.

### Which day

Run before 12:00 local time and it forecasts today. Run after 12:00 and it forecasts tomorrow. This keeps a timer that catches up after midnight from forecasting the wrong day. Pass `--date YYYY-MM-DD` to choose a day.

## How it delivers

It always prints the message. Unless you pass `--dry`, it also sends it to every channel you configure:

- **Webhook:** a JSON POST with `text` (Slack), `content` (Discord) and `score`.
- **Telegram:** a message through the Bot API.

The channels are independent, so one failing does not block the other. If a channel fails, or the whole forecast fails, the script exits with code 1 and sends a "ride-forecast FAILED" message to the channels that still work, so a broken night does not go unnoticed. If one source is down, the report still goes out from the rest.

## Quickstart

You need Python 3.9 or newer. There are no dependencies.

```
git clone https://github.com/n0nuser/ride-forecast
cd ride-forecast
python3 test_ride.py                          # prints "ok"
python3 ride.py "Salamanca" --country ES --dry
```

`--country` takes an ISO code. Use it, because "Leon" alone resolves to Nicaragua and "Cordoba" to Argentina. The second line of the message shows the place it picked.

To send for real, set a channel in the environment:

```
export WEBHOOK_URL="https://hooks.slack.com/services/..."
python3 ride.py "Salamanca" --country ES
```

For Telegram, talk to @BotFather to create a bot and get its token, send your bot a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`:

```
export TELEGRAM_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

### Options

| Option | Env variable | Meaning |
|---|---|---|
| `place` | `RIDE_PLACE` | Place name to forecast. |
| `--country` | `RIDE_COUNTRY` | ISO country code to disambiguate the place. |
| `--date` | | Day to forecast, `YYYY-MM-DD`. |
| `--dry` | | Print only, send nothing. |
| | `WEBHOOK_URL` | Webhook to POST to. |
| | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram bot and chat. |

Without `--dry`, the script refuses to run if no channel is configured. Unknown flags are errors, so a typo like `--dry-run` can't send a message by accident.

## Run it every night with systemd

The `systemd/` folder has a service and a timer. The timer fires at 21:00 server time. The service runs once, as a throwaway user, with a read-only view of the system.

### Install

```
sudo mkdir -p /opt/ride-forecast
sudo cp ride.py /opt/ride-forecast/

sudo cp systemd/ride-forecast.env.example /etc/ride-forecast.env
sudo chmod 600 /etc/ride-forecast.env
sudo $EDITOR /etc/ride-forecast.env           # set RIDE_PLACE, RIDE_COUNTRY and a channel

sudo cp systemd/ride-forecast.service systemd/ride-forecast.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ride-forecast.timer
```

### Check it

```
sudo systemctl start ride-forecast.service    # run it now
journalctl -u ride-forecast -n 20             # see the output
systemctl list-timers ride-forecast.timer     # see when it fires next
```

A run that sent everything shows `Deactivated successfully` in the journal. A failed channel shows `Failed with result 'exit-code'`, plus a line saying which channel failed.

### Change the time or the place

Edit the timer without touching the packaged file:

```
sudo systemctl edit ride-forecast.timer
```

Put this in the editor:

```
[Timer]
OnCalendar=
OnCalendar=*-*-* 20:30
```

The empty `OnCalendar=` line clears the old time. To change the place, edit `/etc/ride-forecast.env`; the next run picks it up.

### Update or remove

To update, copy the new `ride.py` to `/opt/ride-forecast/`. Nothing else needs a restart.

To remove it:

```
sudo systemctl disable --now ride-forecast.timer
sudo rm /etc/systemd/system/ride-forecast.{service,timer} /etc/ride-forecast.env
sudo rm -r /opt/ride-forecast
sudo systemctl daemon-reload
```

### Notes

- The timer uses `Persistent=true`, so if the server was off at 21:00 the run happens when it boots. Because the script forecasts today before noon and tomorrow after, a late run still reports the right day.
- The service waits for `network-online.target`. That only works if a wait-online service is enabled on your system (`systemd-networkd-wait-online` or `NetworkManager-wait-online`). If the first run after boot fails with a network error, enable the one you use.
- The service has a 300-second timeout, and the script has its own 180-second limit.

## Limits

- Forecasts are forecasts. The score is a rule of thumb, not a safety guarantee. Check the road, your gear and your own judgment.
- It scores weather only. Daylight, traffic and road works are not in it, so it can suggest a window after sunset.
- Only MET Norway is independent of the Open-Meteo API. Adding more providers is a small change in `ride.py`.

## Tests

```
python3 test_ride.py
```

The tests cover the scoring, the dangerous-hour rule, the source-disagreement rule, the target-day logic, the independent sending of channels, and the command-line checks. They don't call any network service.

## License

MIT. See [LICENSE](LICENSE).
