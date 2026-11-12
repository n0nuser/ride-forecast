"""Hourly chart for one day: score, temperature and rain, gusts, visibility. Bands and lines follow ride.LEVELS and the
thresholds in ride.py, so the charts never disagree with the text. matplotlib is imported lazily."""
import io

import ride

BLOCKS = "▁▂▃▄▅▆▇█"
COLORS = ["#2e9e5b", "#e0a800", "#f08a24", "#d64545"]  # one per ride.LEVELS entry


def sparkline(rows):
    """Text fallback when matplotlib is missing: one block per hour, 00-23, '·' where there is no data."""
    by = {r["hour"]: r["score"] for r in rows}
    return "".join(BLOCKS[min(7, int(by[h] / 12.5))] if h in by else "·" for h in range(24))


def _lines(ax, marks):
    """Dashed threshold lines: (value, level colour index)."""
    for v, i in marks:
        ax.axhline(v, color=COLORS[i], ls="--", lw=0.8)


def _draw(fig, axes, report, plt):
    rows = report["rows"]
    x = [r["hour"] for r in rows]
    ax1, ax2, ax3, ax4 = axes
    fig.suptitle(f"{report['label']} · {report['day']}", fontsize=11)

    # score panel: the four level bands, each source thin, the median thick, stops marked
    floors = [lo for _, _, lo in ride.LEVELS]
    for i, lo in enumerate(floors):
        ax1.axhspan(lo, 100 if i == 0 else floors[i - 1], color=COLORS[i], alpha=0.10, lw=0)
    for n in sorted({n for r in rows for n in r["per"]}):
        pts = [(r["hour"], r["per"][n]) for r in rows if n in r["per"]]
        ax1.plot(*zip(*pts), lw=0.8, alpha=0.55, label=n)
    ax1.plot(x, [r["score"] for r in rows], color="black", lw=2.4, label="median")
    stops = [r for r in rows if r["stop"]]
    ax1.scatter([r["hour"] for r in stops], [0] * len(stops), marker="X", color=COLORS[-1], s=60, zorder=5, label="DON'T RIDE")
    for e in report["trips"]:
        a, b = e["hours"][0], e["hours"][-1] + 1
        for ax in axes:
            ax.axvspan(a, b, color="#4f81bd", alpha=0.10, lw=0)
        ax1.text((a + b) / 2, 104, f"{e['name']}\n{e['verdict']}", ha="center", va="bottom", fontsize=8)
    ax1.set_ylim(0, 100)
    ax1.set_ylabel("ride score")

    # weather panel: temperature line (3 °C frost and 32 °C heat lines), rain bars on the right axis, coloured by rain level
    ax2.plot(x, [r["w"].t for r in rows], color="#c0504d", lw=2)
    ax2.set_ylabel("°C", color="#c0504d")
    lo, hi = ax2.get_ylim()
    _lines(ax2, [(ride.FROST_T, 1), (ride.HEAT_LEVELS[0], 1), (ride.HEAT_LEVELS[1], 2)])
    ax2.set_ylim(min(lo, ride.FROST_T + 1) if lo < 8 else lo, max(hi, 10) if hi < 30 else hi)
    twin = ax2.twinx()
    twin.bar(x, [r["w"].mm for r in rows], width=0.7, alpha=0.7,  # blue = tip only; yellow, orange, red = the rain levels
             color=["#4f81bd" if (n := ride._step(r["w"].mm, ride.RAIN_LEVELS)) == 0 else COLORS[n] for r in rows])
    twin.set_ylabel("rain mm/h", color="#4f81bd")
    top = max(1.2, max(r["w"].mm for r in rows) * 1.2)
    twin.set_ylim(0, top)

    # wind panel: gust levels
    ax3.plot(x, [r["w"].gust or 0 for r in rows], color="#555", lw=1.6)
    _lines(ax3, list(zip(ride.GUST_LEVELS, (1, 2, 3))))
    ax3.set_ylabel("gusts km/h")
    ax3.set_ylim(0, max(ride.GUST_LEVELS[-1] + 5, max((r["w"].gust or 0) for r in rows) * 1.1))

    # visibility panel (log scale, km): the worst any source reports; fog levels
    vis = [(r["hour"], r["w"].vis / 1000) for r in rows if r["w"].vis is not None]
    if vis:
        ax4.plot(*zip(*[(h, min(max(v, 0.03), 20)) for h, v in vis]), color="#7a6fb0", lw=1.6)
        ax4.set_yscale("log")
        ax4.set_ylim(0.05, 20)
        ax4.set_yticks([0.1, 0.2, 1, 10])
        ax4.set_yticklabels(["0.1", "0.2", "1", "10"])
        _lines(ax4, [(v / 1000, i) for v, i in zip(ride.VIS_LEVELS, (1, 2, 3))])
    else:
        ax4.text(0.5, 0.5, "no source reports visibility", transform=ax4.transAxes, ha="center", va="center", color="#777", fontsize=8)
        ax4.set_yticks([])
    fog = [r["hour"] for r in rows if r["w"].code in ride.FOG]
    for h in fog:  # fog flagged by the weather code, even where no visibility figure exists
        ax4.axvspan(h - 0.5, h + 0.5, color=COLORS[1], alpha=0.25, lw=0)
    ax4.set_ylabel("visibility km")
    ax4.set_xlim(-0.5, 23.5)
    ax4.set_xticks(range(0, 24, 2))
    ax4.set_xlabel("hour (local)")
    for ax in axes:
        ax.grid(alpha=0.25)
    h, l = ax1.get_legend_handles_labels()
    fig.legend(h, l, ncol=7, fontsize=8, loc="lower center", frameon=False)
    fig.tight_layout(rect=(0, 0.04, 1, 0.96))


def render(report):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(4, 1, figsize=(9, 8.5), sharex=True, gridspec_kw={"height_ratios": [3, 2, 1.3, 1.3]})
    try:
        _draw(fig, axes, report, plt)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        return buf.getvalue()
    finally:
        plt.close(fig)


def render_week(reports):
    """One chart for the week: day score bars coloured and labelled by level, temperature range, rain."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import date

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    try:
        names = [date.fromisoformat(r["day"]).strftime("%a\n%d") for r in reports]
        x = range(len(reports))
        fig.suptitle(f"{reports[0]['label']} · next {len(reports)} days", fontsize=11)
        sc = [(r["summary"] or {}).get("score", 0) for r in reports]
        lv = [ride.level_of(v) for v in sc]
        ax1.bar(x, sc, color=[COLORS[i] for i in lv], width=0.6)
        for lo in (ride.LEVELS[0][2], ride.LEVELS[1][2], ride.LEVELS[2][2]):
            ax1.axhline(lo, color="#888", ls=":", lw=0.7)
        for i, r in enumerate(reports):
            ax1.text(i, sc[i] + 2, f"{sc[i]}\n{ride.LEVELS[lv[i]][0]}" if r["summary"] else "n/a", ha="center", fontsize=8)
        ax1.set_ylim(0, 120)
        ax1.set_ylabel("day score")
        rs = [[q for q in r["rows"] if q["hour"] in ride.DAY_HOURS] for r in reports]
        ax2.vlines(x, [min([q["w"].t for q in d] or [0]) for d in rs], [max([q["w"].t for q in d] or [0]) for d in rs], color="#c0504d", lw=6)
        ax2.set_ylabel("°C (08-20h)", color="#c0504d")
        twin = ax2.twinx()
        twin.bar([i + 0.25 for i in x], [max([q["w"].mm for q in d] or [0]) for d in rs], width=0.25, color="#4f81bd", alpha=0.8)
        twin.set_ylabel("max rain mm/h", color="#4f81bd")
        ax2.set_xticks(list(x))
        ax2.set_xticklabels(names)
        for ax in (ax1, ax2):
            ax.grid(alpha=0.25)
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        return buf.getvalue()
    finally:
        plt.close(fig)
