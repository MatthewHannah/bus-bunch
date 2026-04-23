#!/usr/bin/env python3
"""Dump a table of upcoming bus arrivals at a MARTA stop.

Usage:
    python stop_etas.py [stop_id ...] [--route ROUTE_ID]

Reads MARTA's GTFS-realtime tripupdates feed and prints, for each
stop_id given, the upcoming vehicles with route, trip, vehicle, the
predicted arrival time, and minutes-until-arrival.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import requests
from google.transit import gtfs_realtime_pb2

TRIP_URL = "https://gtfs-rt.itsmarta.com/TMGTFSRealTimeWebService/tripupdate/tripupdates.pb"
DEFAULT_STOPS = ["104078"]


def fetch_feed(url: str = TRIP_URL, timeout: int = 30) -> gtfs_realtime_pb2.FeedMessage:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed


def collect_etas(feed, stop_ids: set[str], route_id: str | None, now: int):
    rows = []
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        if route_id and tu.trip.route_id != route_id:
            continue
        for stu in tu.stop_time_update:
            if stu.stop_id not in stop_ids:
                continue
            t = stu.arrival.time or stu.departure.time
            if not t:
                continue
            rows.append({
                "stop_id": stu.stop_id,
                "route": tu.trip.route_id,
                "vehicle": tu.vehicle.id or "",
                "trip": tu.trip.trip_id,
                "arrival_ts": t,
                "eta_s": t - now,
            })
    rows.sort(key=lambda r: (r["stop_id"], r["arrival_ts"]))
    return rows


def fmt_eta(s: int) -> str:
    if s < 0:
        return f"-{(-s)//60:>2d}m{(-s)%60:02d}s"
    return f" {s//60:>2d}m{s%60:02d}s"


def render(rows, feed_ts: int, now: int) -> str:
    header = f"{'stop':<8} {'route':<6} {'vehicle':<8} {'trip':<12} {'arrival':<8} {'eta':<8}"
    sep = "-" * len(header)
    lines = [
        f"feed timestamp: {datetime.fromtimestamp(feed_ts).strftime('%Y-%m-%d %H:%M:%S')}  "
        f"(age {now - feed_ts}s)",
        header,
        sep,
    ]
    if not rows:
        lines.append("(no upcoming arrivals)")
        return "\n".join(lines)
    for r in rows:
        arr = datetime.fromtimestamp(r["arrival_ts"]).strftime("%H:%M:%S")
        lines.append(
            f"{r['stop_id']:<8} {r['route']:<6} {r['vehicle']:<8} "
            f"{r['trip']:<12} {arr:<8} {fmt_eta(r['eta_s'])}"
        )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stops", nargs="*", default=DEFAULT_STOPS,
                   help=f"stop_id(s) to query (default: {' '.join(DEFAULT_STOPS)})")
    p.add_argument("--route", help="optional route_id filter")
    p.add_argument("--include-past", action="store_true",
                   help="include arrivals whose predicted time is already in the past")
    args = p.parse_args()

    feed = fetch_feed()
    now = int(time.time())
    rows = collect_etas(feed, set(args.stops), args.route, now)
    if not args.include_past:
        rows = [r for r in rows if r["eta_s"] >= 0]
    print(render(rows, feed.header.timestamp, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
