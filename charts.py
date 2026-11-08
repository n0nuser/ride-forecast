"""Hourly chart for one day: scores, temperature and rain, gusts. matplotlib is imported lazily."""
import io

import ride

BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(rows):
    """Text fallback when matplotlib is missing: one block per hour, 00-23, '·' where there is no data."""
    by = {r["hour"]: r["score"] for r in rows}
    return "".join(BLOCKS[min(7, int(by[h] / 12.5))] if h in by else "·" for h in range(24))


def _draw(fig, axes, report, plt):
    rows = report["rows"]
    x = [r["hour"] for r in rows]
    ax1, ax2, ax3 = axes
    fig.suptitle(f"{report['label']} · {report['day']}", fontsize=11)

    # score panel: verdict bands, each source thin, the median thick, stops marked
    (ok, _), (caution, _), _ = ride.TRIP_BANDS
    for lo, hi, c in ((ok, 100, "#2e9e5b"), (caution, ok, "#e0a800"), (0, caution, "#d64545")):
        ax1.axhspan(lo, hi, color=c, alpha=0.10, lw=0)
    for n in sorted({n for r in rows for n in r["per"]}):
        pts = [(r["hour"], r["per"][n]) for r in rows if n in r["per"]]
        ax1.plot(*zip(*pts), lw=0.8, alpha=0.55, label=n)
    ax1.plot(x, [r["score"] for r in rows], color="black", lw=2.4, label="median")
    stops = [r for r in rows if r["stop"]]
    ax1.scatter([r["hour"] for r in stops], [0] * len(stops), marker="X", color="#d64545", s=60, zorder=5, label="stop")
    for e in report["trips"]:
        a, b = e["hours"][0], e["hours"][-1] + 1
        for ax in (ax1, ax2, ax3):
            ax.axvspan(a, b, color="#4f81bd", alpha=0.10, lw=0)
        ax1.text((a + b) / 2, 104, f"{e['name']}\n{e['verdict']}", ha="center", va="bottom", fontsize=8)
    ax1.set_ylim(0, 100)
    ax1.set_ylabel("score")

    # weather panel: temperature line, rain bars on the right axis
    ax2.plot(x, [r["w"].t for r in rows], color="#c0504d", lw=2)
    ax2.set_ylabel("°C", color="#c0504d")
    twin = ax2.twinx()
    twin.bar(x, [r["w"].mm for r in rows], width=0.7, color="#4f81bd", alpha=0.7)
    twin.set_ylabel("rain mm/h", color="#4f81bd")
    twin.set_ylim(0, max(1.0, max(r["w"].mm for r in rows) * 1.2))

    # wind panel
    ax3.plot(x, [r["w"].gust or 0 for r in rows], color="#555", lw=1.6)
    ax3.axhline(ride.GUST_FROM, color="#e0a800", ls="--", lw=0.8)
    ax3.axhline(ride.GUST_STOP, color="#d64545", ls="--", lw=0.8)
    ax3.set_ylabel("gusts km/h")
    ax3.set_ylim(0, max(ride.GUST_STOP + 5, max((r["w"].gust or 0) for r in rows) * 1.1))
    ax3.set_xlim(-0.5, 23.5)
    ax3.set_xticks(range(0, 24, 2))
    ax3.set_xlabel("hour (local)")
    for ax in (ax1, ax2, ax3):
        ax.grid(alpha=0.25)
    h, l = ax1.get_legend_handles_labels()
    fig.legend(h, l, ncol=7, fontsize=8, loc="lower center", frameon=False)
    fig.tight_layout(rect=(0, 0.04, 1, 0.96))



def render(report):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(9, 7), sharex=True, gridspec_kw={"height_ratios": [3, 2, 1.3]})
    try:
        _draw(fig, axes, report, plt)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        return buf.getvalue()
    finally:
        plt.close(fig)


def render_week(reports):
    """One chart for the week: day score bars coloured by verdict, temperature range, rain."""
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
        col = ["#2e9e5b" if v >= ride.BANDS[1][0] else "#e0a800" if v >= ride.BANDS[2][0] else "#d64545" for v in sc]
        ax1.bar(x, sc, color=col, width=0.6)
        for i, r in enumerate(reports):
            ax1.text(i, sc[i] + 2, f"{sc[i]}" if r["summary"] else "n/a", ha="center", fontsize=9)
        ax1.set_ylim(0, 110)
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
