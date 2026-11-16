"""Build contribution snakes from GitHub's publicly visible daily calendar.

Only anonymous dates, contribution levels and counts are read or published.
No credentials, repository names or private repository contents are required.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen


def count_from_text(text: str) -> int | None:
    if re.search(r"\bno contributions?\b", text, re.I):
        return 0
    match = re.search(r"\b([\d,]+)\s+contributions?\b", text, re.I)
    return int(match[1].replace(",", "")) if match else None


class CalendarParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = {}
        self.tooltips = {}
        self.text = []
        self.tip_id = None
        self.tip_text = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in ("td", "rect") and attrs.get("data-date"):
            day = date.fromisoformat(attrs["data-date"])
            raw_count = attrs.get("data-count")
            count = int(raw_count.replace(",", "")) if raw_count is not None else count_from_text(attrs.get("title", ""))
            if "data-level" in attrs:
                level = int(attrs["data-level"])
            else:
                # Older GitHub SVG calendars expose color and count, not a level.
                old_colors = {"#ebedf0": 0, "#eff2f5": 0, "#161b22": 0,
                              "#9be9a8": 1, "#c6e48b": 1, "#033a16": 1,
                              "#40c463": 2, "#7bc96f": 2, "#196c2e": 2,
                              "#30a14e": 3, "#239a3b": 3, "#2ea043": 3,
                              "#216e39": 4, "#196127": 4, "#56d364": 4}
                level = old_colors.get(attrs.get("fill", "").lower(), 1 if count and count > 0 else 0)
            if not 0 <= level <= 4:
                raise ValueError("Contribution level outside the range 0–4")
            cell = {"date": day.isoformat(), "level": level, "count": count,
                    "element_id": attrs.get("id")}
            old = self.cells.get(day)
            if old is None or cell["count"] is not None or old["element_id"] is None:
                self.cells[day] = cell
        if tag == "tool-tip":
            self.tip_id = attrs.get("for")
            self.tip_text = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "tool-tip":
            if self.tip_id:
                self.tooltips[self.tip_id] = " ".join(self.tip_text)
            self.tip_id = None
            self.tip_text = []

    def handle_data(self, text):
        self.text.append(text)
        if self.tip_id:
            self.tip_text.append(text)


def parse_calendar(html: str):
    parser = CalendarParser()
    parser.feed(html)
    if not parser.cells:
        raise ValueError("GitHub returned no dated contribution calendar cells; refusing to publish an empty replacement")
    days = []
    for observed, original in sorted(parser.cells.items()):
        cell = dict(original)
        tooltip = parser.tooltips.get(cell.pop("element_id"), "")
        parsed = count_from_text(tooltip)
        if parsed is not None:
            cell["count"] = parsed
        if cell["count"] is None and cell["level"] == 0:
            cell["count"] = 0
        days.append(cell)
    first, last = date.fromisoformat(days[0]["date"]), date.fromisoformat(days[-1]["date"])
    origin = first - timedelta(days=(first.weekday() + 1) % 7)
    for cell in days:
        offset = (date.fromisoformat(cell["date"]) - origin).days
        cell["x"], cell["y"] = offset // 7, offset % 7
    columns = max(cell["x"] for cell in days) + 1
    if columns > 55:
        raise ValueError("Calendar spans more than a year; refusing an unexpected response")
    joined = " ".join(parser.text)
    match = re.search(r"([\d,]+)\s+contributions?\s+in\s+(?:the\s+)?last\s+year", joined, re.I)
    known_counts = [cell["count"] for cell in days if cell["count"] is not None]
    complete = len(known_counts) == len(days)
    total = int(match[1].replace(",", "")) if match else (sum(known_counts) if complete else None)
    warnings = []
    if not complete:
        warnings.append("Some public calendar cells omit numeric counts; their observed color levels are preserved")
    if not any(cell["level"] for cell in days):
        warnings.append("GitHub's public calendar contains no colored days; check public contribution visibility if unexpected")
    if complete and match and sum(known_counts) != total:
        warnings.append("GitHub's heading and daily count sum differ; the heading total is preserved")
    return days, columns, origin, total, warnings


def plan_snake(days, columns):
    remaining = {(cell["x"], cell["y"]) for cell in days if cell["level"] > 0}
    all_food = set(remaining)
    snake = tuple((-1 - i, 3) for i in range(5))
    states, eaten = [snake], {}

    def nearest_route(start, goals, exit_mode=False):
        queue, visited = deque([(start, ())]), {start}
        while queue:
            state, path = queue.popleft()
            if state[0] in goals:
                return path
            x, y = state[0]
            for dx, dy in ((1, 0), (0, -1), (0, 1), (-1, 0)):
                point = (x + dx, y + dy)
                if not (0 <= point[0] <= (columns + 5 if exit_mode else columns - 1) and 0 <= point[1] < 7):
                    continue
                if point in state[:-1]:
                    continue
                nxt = (point,) + state[:-1]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, path + (point,)))
        raise RuntimeError("No safe contribution-snake route found")

    while remaining:
        for point in nearest_route(snake, remaining):
            snake = (point,) + snake[:-1]
            states.append(snake)
            if point in remaining:
                remaining.remove(point)
                eaten[point] = len(states) - 1
    for point in nearest_route(snake, {(columns + 5, snake[0][1])}, True):
        snake = (point,) + snake[:-1]
        states.append(snake)
    if set(eaten) != all_food:
        raise RuntimeError("Snake route failed to consume every colored day")
    for prev, cur in zip(states, states[1:]):
        if len(set(cur)) != len(cur) or cur[1:] != prev[:-1] or sum(abs(a-b) for a, b in zip(prev[0], cur[0])) != 1:
            raise RuntimeError("Invalid snake movement")
    return states, eaten


def render_svg(days, columns, origin, total, states, eaten, generated_at, dark=False):
    pitch, tile, left, top = 20, 14, 90, 101
    width, height = left + columns * pitch + 34, 316
    background, border, text, muted = ("#0d1117", "#3d444d", "#f0f6fc", "#9198a1") if dark else ("#ffffff", "#d1d9e0", "#1f2328", "#59636e")
    palette = ["#151b23", "#033a16", "#196c2e", "#2ea043", "#56d364"] if dark else ["#eff2f5", "#9be9a8", "#40c463", "#30a14e", "#216e39"]
    snake_color = "#00E5A0" if dark else "#00C78A"
    hold, step_ms, rest, reset = 800, 85, 900, 500
    last_move = hold + (len(states) - 1) * step_ms
    duration = last_move + rest + reset
    percentage = lambda milliseconds: milliseconds * 100 / duration
    css = [f"text{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif}}",
           f".snake{{fill:{snake_color};animation-duration:{duration}ms;animation-timing-function:linear;animation-iteration-count:infinite}}"]
    for index in range(5):
        poses = [(0, states[0][index]), (percentage(hold), states[0][index])]
        poses += [(percentage(hold + step * step_ms), state[index]) for step, state in enumerate(states[1:], 1)]
        poses += [(100, states[-1][index])]
        keys = "".join(f"{p:.5f}%{{transform:translate({point[0]*pitch}px,{point[1]*pitch}px)}}" for p, point in poses)
        css.append(f"@keyframes s{index}{{{keys}}}.s{index}{{animation-name:s{index}}}")
    for cell in days:
        point = (cell["x"], cell["y"])
        if point not in eaten:
            continue
        key = f"d{cell['x']}_{cell['y']}"
        eat = percentage(hold + eaten[point] * step_ms)
        resume = percentage(last_move + rest)
        color = palette[cell["level"]]
        css.append("@keyframes " + key + "{" + f"0%,{max(0,eat-.0001):.5f}%" +
                   "{fill:" + color + "}" + f"{eat:.5f}%,{resume:.5f}%" +
                   "{fill:" + palette[0] + "}" + "100%{fill:" + color + "}}")
        css.append(f".{key}{{animation:{key} {duration}ms linear infinite}}")
    heading = f"{total:,} contributions in the last year" if total is not None else "GitHub contributions in the last year"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-labelledby="title description">',
           f'<title id="title">{escape(heading)} — animated contribution snake</title>',
           '<desc id="description">A green snake consumes the publicly visible daily contribution squares, then the calendar resets. Automatically regenerated from GitHub.</desc>',
           f'<style>{"".join(css)}</style>',
           f'<rect width="{width}" height="{height}" fill="{background}"/>',
           f'<text x="4" y="29" font-size="22" fill="{text}">{escape(heading)}</text>',
           f'<rect x="1" y="52" width="{width-2}" height="263" rx="8" fill="none" stroke="{border}"/>',
           f'<defs><clipPath id="grid"><rect x="-3" y="-3" width="{columns*pitch+6}" height="{7*pitch+6}"/></clipPath></defs>']
    months = []
    previous_month = None
    for x in range(columns):
        week = origin + timedelta(days=x * 7)
        label_date = week + timedelta(days=6)
        if label_date.month != previous_month:
            months.append((x, label_date.strftime("%b")))
            previous_month = label_date.month
    # Avoid two overlapping month labels at the right edge of a partial week.
    for i, (x, label) in enumerate(months):
        if i + 1 < len(months) and months[i+1][0] - x < 2:
            continue
        out.append(f'<text x="{left+x*pitch}" y="86" font-size="14" fill="{muted}">{label}</text>')
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append(f'<text x="23" y="{top+row*pitch+11}" font-size="13" fill="{muted}">{label}</text>')
    out.append(f'<g transform="translate({left},{top})">')
    for cell in days:
        key = f"d{cell['x']}_{cell['y']}" if cell["level"] else ""
        out.append(f'<rect class="{key}" x="{cell["x"]*pitch}" y="{cell["y"]*pitch}" width="{tile}" height="{tile}" rx="3" fill="{palette[cell["level"]]}"><title>{escape(cell["date"])}: {cell["count"] if cell["count"] is not None else "count unavailable"}</title></rect>')
    out.append('<g clip-path="url(#grid)">')
    for i in reversed(range(5)):
        size = 18 - i * 1.65
        offset = (tile - size) / 2
        out.append(f'<g class="snake s{i}"><rect x="{offset:.2f}" y="{offset:.2f}" width="{size:.2f}" height="{size:.2f}" rx="{size*.3:.2f}" opacity="{1-i*.075:.3f}"/>')
        if i == 0:
            out.append('<circle cx="11" cy="3.7" r="1.7" fill="#0d1117"/><circle cx="11" cy="10.3" r="1.7" fill="#0d1117"/>')
        out.append('</g>')
    out.append('</g></g>')
    legend_x = width - 196
    out.append(f'<text x="{legend_x-38}" y="268" font-size="12" fill="{muted}">Less</text>')
    for i, color in enumerate(palette):
        out.append(f'<rect x="{legend_x+i*20}" y="255" width="14" height="14" rx="3" fill="{color}"/>')
    out.append(f'<text x="{legend_x+104}" y="268" font-size="12" fill="{muted}">More</text>')
    stamp = generated_at.strftime("%d %b %Y, %H:%M UTC")
    out.append(f'<text x="{left}" y="297" font-size="11" fill="{muted}">Updated {stamp} · Rolling last year</text></svg>')
    return "".join(out)


def self_test():
    # A partial first week, comma-formatted tooltip, old rect format and HTML escaping.
    html = '''<h2>1,239 contributions in the last year</h2>
    <td id="a" data-date="2026-11-02" data-level="1"></td>
    <tool-tip for="a">1 contribution on November 2.</tool-tip>
    <td id="b" data-date="2026-11-08" data-level="4"></td>
    <tool-tip for="b"><strong>1,233 contributions</strong> on November 8.</tool-tip>
    <rect data-date="2026-11-09" fill="#30a14e" data-count="5"/>
    <rect data-date="2026-11-10" data-level="0" data-count="0"/>
    '''
    days, columns, origin, total, warnings = parse_calendar(html)
    assert total == 1239 and days[0]["count"] == 1 and days[1]["count"] == 1233
    assert days[2]["count"] == 5 and days[2]["level"] == 3
    assert origin == date(2026, 11, 1) and (days[0]["x"], days[0]["y"]) == (0, 1)
    assert (days[1]["x"], days[1]["y"]) == (1, 0)
    synthetic = [{"date": (date(2026, 11, 1)+timedelta(days=x*7+y)).isoformat(), "x": x, "y": y, "count": (x+y)%5, "level": (x+y)%5} for x in range(9) for y in range(7)]
    states, eaten = plan_snake(synthetic, 9)
    assert len(eaten) == sum(cell["level"] > 0 for cell in synthetic)
    svg = render_svg(synthetic, 9, date(2026, 11, 1), 123, states, eaten, datetime.now(timezone.utc), True)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(svg)
    assert not any(node.tag.rsplit("}", 1)[-1] in {"script", "foreignObject", "image"} for node in root.iter())
    assert "@keyframes" in svg and "123 contributions" in svg
    empty_days = [dict(cell, count=0, level=0) for cell in synthetic]
    empty_states, empty_eaten = plan_snake(empty_days, 9)
    assert not empty_eaten and len(empty_states) > 5
    assert empty_states[-1][-1][0] > 9
    try:
        parse_calendar("<html>Sign in</html>")
        raise AssertionError("Empty calendar response was accepted")
    except ValueError:
        pass
    print(json.dumps({"self_test": "passed", "synthetic_active_days": len(eaten), "valid_snake_moves": len(states)-1}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="yuviii0509")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--html-file", type=Path, help="Read a saved public calendar fixture instead of making a network request")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", args.user):
        parser.error("Invalid GitHub username")
    if args.html_file:
        html = args.html_file.read_text(encoding="utf-8")
    else:
        request = Request(f"https://github.com/users/{args.user}/contributions", headers={"User-Agent": "public-contribution-snake/1.0", "Accept": "text/html", "Accept-Language": "en"})
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"GitHub calendar returned HTTP {response.status}")
            html = response.read().decode("utf-8")
    days, columns, origin, total, warnings = parse_calendar(html)
    if not args.html_file and len(days) < 300:
        raise ValueError("Public GitHub calendar has fewer than 300 dated cells; refusing an incomplete response")
    generated_at = datetime.now(timezone.utc)
    states, eaten = plan_snake(days, columns)
    args.output.mkdir(parents=True, exist_ok=True)
    for dark, filename in ((False, "github-snake-live.svg"), (True, "github-snake-live-dark.svg")):
        svg = render_svg(days, columns, origin, total, states, eaten, generated_at, dark)
        (args.output / filename).write_text(svg, encoding="utf-8")
    metadata = {"generated_at": generated_at.isoformat(), "source": "public GitHub contribution calendar", "total_contributions": total,
                "active_days": len(eaten), "calendar_days": len(days), "from": days[0]["date"], "to": days[-1]["date"],
                "warnings": warnings, "days": [{key: cell[key] for key in ("date", "count", "level")} for cell in days]}
    (args.output / "contribution-calendar.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in metadata.items() if key != "days"}))
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)


if __name__ == "__main__":
    main()
