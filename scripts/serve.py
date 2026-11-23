#!/usr/bin/env python3
"""Tiny local web interface (standard library only)."""

from __future__ import annotations

import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.planner import Planner  # noqa: E402

PLANNER = Planner(ROOT / "output" / "transport.db")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Delhi bus crowding planner</title>
<style>
body{{font:14px -apple-system,Helvetica,sans-serif;margin:24px;color:#222;max-width:1100px}}
input,select,button{{padding:6px;font-size:14px}} table{{border-collapse:collapse;width:100%;margin-top:18px}}
th,td{{border:1px solid #ddd;padding:6px;text-align:left}} th{{background:#f2f4f7}}
details{{margin:4px 0}} .note{{color:#666;font-size:12px}} .warn{{color:#8a4b00}}
</style></head><body>
<h2>Crowdedness-aware Delhi bus journey planner</h2>
<form method="get"><label>Boarding <input name="board" value="{board}" size="32" required></label>
&nbsp;<label>Destination <input name="alight" value="{alight}" size="32" required></label><br><br>
<label>Date <input type="date" name="date" value="{date}"></label>
&nbsp;<label>Time <input name="time" value="{time}" size="8"></label>
&nbsp;<label>Window ± min <input name="window" type="number" min="0" max="30" value="{window}" size="4"></label>
&nbsp;<label>Sort <select name="sort"><option value="arrival"{arr_sel}>earliest arrival</option>
<option value="crowding"{crowd_sel}>least recorded crowding</option></select></label>
&nbsp;<button type="submit">Plan</button></form>
{content}
<p class="note">Recorded ticket demand only — not total passengers or seats. Earlier departures are context, not recommendations.</p>
</body></html>"""


def render(result: dict) -> str:
    if "error" in result:
        return f"<p class='warn'>{html.escape(result['error'])}</p>"
    rows = []
    for o in result["options"]:
        stops = "".join(
            f"<li>{html.escape(s['from'])} &rarr; {html.escape(s['to'])}: {s['minutes']} min, "
            f"load {s['load']} (p75 {s['p75']}, n={s['support']}), {s['recorded_class']}</li>"
            for s in o["stops"])
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>"
            "<tr><td colspan='11'><details><summary>Stop-by-stop</summary><ul>{}</ul></details></td></tr>".format(
                html.escape(o["route"]), html.escape(o["direction"]), o["departure_at_board"],
                o["arrival_at_destination"], o["wait_minutes"], o["ride_minutes"],
                o["expected_recorded_load"], o["busiest_stretch"], o["minutes_crowded"],
                o["confidence"], o["evidence"], stops))
    head = ("<p>Window {}-{} inclusive; {} distinct scheduled departures. {} direct options today. "
            "Stop matching: board {}, alight {}. Pool: {} recorded passengers / {} {} departures = {} per departure.</p>"
            .format(*result["query"]["window_seconds"], result["distinct_departures_in_window"],
                    result["total_scheduled_direct_options_today"],
                    result["stop_matching"].get("board_match"), result["stop_matching"].get("alight_match"),
                    result["pooled"]["total_recorded_passengers"], result["pooled"]["eligible_departures"],
                    result["pooled"]["denominator_source"],
                    round(result["pooled"]["average_recorded_per_departure"] or 0, 1)))
    table = ("<table><tr><th>route</th><th>direction</th><th>dep</th><th>arr</th><th>wait</th><th>ride</th>"
             "<th>load</th><th>busiest stretch</th><th>crowded min</th><th>confidence</th><th>evidence</th></tr>"
             + "".join(rows) + "</table>")
    return head + table


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            result = self._plan(q)
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            q = {k: v[0] for k, v in q.items()}
            defaults = {"board": "Tara Apartment", "alight": "Punj Sons / Govind Puri Metro Station",
                        "date": "2026-10-23", "time": "08:40", "window": "2", "sort": "arrival"}
            for k, v in defaults.items():
                q.setdefault(k, v)
            result = self._plan(q)
            arr_sel = " selected" if q["sort"] == "arrival" else ""
            crowd_sel = " selected" if q["sort"] == "crowding" else ""
            body = PAGE.format(board=html.escape(q["board"]), alight=html.escape(q["alight"]),
                               date=q["date"], time=q["time"], window=q["window"],
                               arr_sel=arr_sel, crowd_sel=crowd_sel, content=render(result)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _plan(self, q):
        def val(name, default):
            value = q.get(name, default)
            return value[0] if isinstance(value, list) and value else value

        try:
            return PLANNER.plan(val("board", ""), val("alight", ""), val("date", "2026-10-23"),
                                val("time", "08:40"), int(val("window", "2")),
                                val("sort", "arrival"))
        except Exception as exc:
            return {"error": str(exc)}

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Open http://localhost:{port}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
