#!/usr/bin/env python3
"""Answers as ffprobe does, with the probe of a real live-sports channel; FAKE_FFPROBE_FAIL names
channels that do not play."""
import os
import sys

if "-h" in sys.argv:
    sys.exit(0)
url = sys.argv[-1]
if any(url.endswith("/" + channel) for channel in os.environ.get("FAKE_FFPROBE_FAIL", "").split(",") if channel):
    sys.stderr.write("%s: Server returned 503 Service Unavailable\n" % url)
    sys.exit(1)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "probe-dm-sky-sports-racing.json")) as handle:
    sys.stdout.write(handle.read())
