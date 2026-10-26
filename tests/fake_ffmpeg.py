#!/usr/bin/env python3
"""Answers as ffmpeg does for rex-live-sports: a brightness reading (a dark logo: 40 on black,
70 on white), or the picture it was given, written where it is asked."""
import sys

url = sys.argv[sys.argv.index("-i") + 1]
if "signalstats" in " ".join(sys.argv):
    print("frame:0\nlavfi.signalstats.YAVG=%s" % ("40" if "color=black" in " ".join(sys.argv) else "70"))
    sys.exit(0)
with open(url, "rb") as source, open(sys.argv[-1], "wb") as handle:
    handle.write(source.read())
