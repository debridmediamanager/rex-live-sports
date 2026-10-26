#!/usr/bin/env python3
"""Answers as ffmpeg does for rex-live-sports: a brightness reading (a dark logo: 40 on black,
70 on white), the picture it was given, or `TITLE:` and the text of a name drawn, written where
it is asked."""
import re
import sys

url = sys.argv[sys.argv.index("-i") + 1]
if "signalstats" in " ".join(sys.argv):
    print("frame:0\nlavfi.signalstats.YAVG=%s" % ("40" if "color=black" in " ".join(sys.argv) else "70"))
    sys.exit(0)
if "lavfi" in sys.argv:
    lines = [open(name, encoding="utf-8").read() for name in re.findall(r"textfile='([^']+)'", sys.argv[sys.argv.index("-vf") + 1])]
    data = ("TITLE:" + " ".join(lines)).encode()
else:
    data = open(url, "rb").read()
with open(sys.argv[-1], "wb") as handle:
    handle.write(data)
