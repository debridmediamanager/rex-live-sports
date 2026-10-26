#!/usr/bin/env python3
"""Answers as ffprobe does, with the probe of a real live-sports channel; FAKE_FFPROBE_FAIL names
channels that do not play; FAKE_FFPROBE_SLATE names channels that answer with live-sports'
placeholder picture (fun, 2026-10-27)."""
import os
import sys

if "-h" in sys.argv:
    sys.exit(0)
url = sys.argv[-1]
if any(url.endswith("/" + channel) for channel in os.environ.get("FAKE_FFPROBE_FAIL", "").split(",") if channel):
    sys.stderr.write("%s: Server returned 503 Service Unavailable\n" % url)
    sys.exit(1)
slate = any(url.endswith("/" + channel) for channel in os.environ.get("FAKE_FFPROBE_SLATE", "").split(",") if channel)
fixture = "probe-slate-cv-2897253.json" if slate else "probe-dm-sky-sports-racing.json"
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", fixture)) as handle:
    sys.stdout.write(handle.read())
