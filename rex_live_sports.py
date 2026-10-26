#!/usr/bin/env python3
"""rex-live-sports: the channels of a live-sports server as a rex library.

live-sports (https://github.com/yowmamasita/live-sports) finds live sports channels, checks
which play and serves each one as HLS. This puts the ones that play into rex, as a "Live Sports"
library with a collection per group ("Live: Football", "Sports: UK"), so they play in every Plex
app. Nothing is recorded: rex reads the channel from live-sports when someone presses play.

One process does two things:

* a relay rex reads from: `GET /<directory>/<channel>.m3u8` answers 302 to live-sports'
  `/stream/<channel>` (rex takes an HLS playlist by its `.m3u8` name, which live-sports' own
  path does not have).
* a sync loop that registers with rex's feed API (`docs/feed-api.md` in every rex download)
  and pushes every channel that plays, each probed once a day.

Standard library only; ffprobe is a separate executable.
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("rex-live-sports")

DEFAULTS = {
    "live_sports_url": "http://127.0.0.1:8765",
    "rex_url": "http://127.0.0.1:32400",
    "source": "live-sports",
    "listen": "127.0.0.1:9983",
    "relay_url": None,
    "mount": "/mnt/rex-live-sports",
    "directory": "live-sports",
    "library": "Live Sports",
    # live-sports also lists general TV ("TV: Other", Pluto); its sports groups start so.
    "groups": ["Live:", "Sports"],
    "cache": "cache",
    "ffprobe": "ffprobe",
    "interval_minutes": 5,
    "probe_hours": 24,
}

CHANNEL = re.compile(r"[A-Za-z0-9_.-]+")


def get_json(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": "rex-live-sports", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def load_config(path):
    with open(path) as handle:
        config = dict(DEFAULTS, **json.load(handle))
    if not os.path.isabs(config["cache"]):
        config["cache"] = os.path.join(os.path.dirname(os.path.abspath(path)), config["cache"])
    config["relay_url"] = (config["relay_url"] or "http://" + config["listen"]).rstrip("/")
    config["live_sports_url"] = config["live_sports_url"].rstrip("/")
    return config


def channels(config, catalog):
    """The catalog's channels in the chosen groups, as (channel id, meta). live-sports lists only
    channels whose stream passed its last check, one per channel it has several copies of."""
    out = []
    for meta in catalog.get("metas") or []:
        if not isinstance(meta, dict) or not str(meta.get("id", "")).startswith("lstv-"):
            continue
        channel, group = meta["id"][len("lstv-"):], str(meta.get("genre") or "")
        if CHANNEL.fullmatch(channel) and group.startswith(tuple(config["groups"])):
            out.append((channel, meta))
    return out


def item(config, channel, meta, probe):
    """One feed item: a live channel nothing identifies, named by `display`, in its group's
    collection. Its own release, or rex would group every channel of the folder as one."""
    path = "%s/%s.m3u8" % (config["directory"], channel)
    group = str(meta.get("genre") or "")
    display = {"title": meta.get("name") or channel, "summary": group, "thumb": meta.get("poster")}
    return {
        "id": "ls-" + channel,
        "path": path,
        "file": "%s/%s" % (config["mount"].rstrip("/"), path),
        "name": meta.get("name") or channel,
        "release": "live-sports:" + channel,
        "media_info": probe,
        "display": {key: value for key, value in display.items() if value},
        "collections": [group] if group else [],
    }


def playable(probe):
    """An HLS probe with a picture: what rex plays as a live channel."""
    return (isinstance(probe, dict) and (probe.get("format") or {}).get("format_name") == "hls"
            and any(stream.get("codec_type") == "video" for stream in probe.get("streams") or []))


class Sync:
    def __init__(self, config):
        self.config = config
        self.pushed, self.short_since = 0, None
        self.picky = None

    def rex(self, method, path, body=None):
        request = urllib.request.Request(self.config["rex_url"].rstrip("/") + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read() or b"{}")

    def register(self):
        answer = self.rex("PUT", "/rex/sources/" + self.config["source"], {
            "url": self.config["relay_url"], "mount": self.config["mount"],
            "directories": [{"name": self.config["directory"], "movies": self.config["library"]}]})
        for ignored in answer.get("ignored") or []:
            LOG.warning("rex ignored %s: %s", ignored.get("entry"), ignored.get("reason"))

    def ffprobe(self, url):
        command = [self.config["ffprobe"], "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
        if self.picky is None:
            # FFmpeg 7.1+ refuses HLS segments named without a media extension unless told not to.
            help_text = subprocess.run([self.config["ffprobe"], "-h", "demuxer=hls"], capture_output=True, text=True).stdout
            self.picky = "extension_picky" in help_text
        if self.picky:
            command += ["-extension_picky", "0"]
        out = subprocess.run(command + [url], capture_output=True, timeout=90)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode(errors="replace").strip()[-300:] or "ffprobe failed")
        return json.loads(out.stdout)

    def probe(self, channel):
        """The channel's probe, kept `probe_hours`: a channel can change mirror, and with it its
        streams. None when it does not play now."""
        path = os.path.join(self.config["cache"], channel + ".json")
        try:
            if time.time() - os.path.getmtime(path) < self.config["probe_hours"] * 3600:
                with open(path) as handle:
                    return json.load(handle)
        except (OSError, ValueError):
            pass
        try:
            probe = self.ffprobe("%s/stream/%s" % (self.config["live_sports_url"], channel))
        except Exception as error:
            LOG.info("%s: %s", channel, error)
            return None
        if not playable(probe):
            LOG.info("%s: no HLS picture in it", channel)
            return None
        os.makedirs(self.config["cache"], exist_ok=True)
        with open(path + ".tmp", "w") as handle:
            json.dump(probe, handle)
        os.replace(path + ".tmp", path)
        return probe

    def once(self):
        listed = channels(self.config, get_json(self.config["live_sports_url"] + "/catalog/tv/all.json"))
        if not listed:
            # live-sports lists nothing until its first checks finish after a start.
            LOG.info("live-sports lists no channel that plays yet")
            return
        self.register()
        with ThreadPoolExecutor(8) as pool:
            probes = list(pool.map(lambda row: self.probe(row[0]), listed))
        items = [item(self.config, channel, meta, probe) for (channel, meta), probe in zip(listed, probes) if probe]
        if not items:
            return
        # Events end and others start, so a sync can rightly replace most channels; rex refuses
        # that unless forced. A list under half of the last one is more likely live-sports still
        # checking after a start, so rex's guard is left to refuse it, for half an hour.
        if len(items) * 2 >= self.pushed:
            self.short_since = None
        elif self.short_since is None:
            self.short_since = time.time()
        force = self.short_since is None or time.time() - self.short_since > 1800
        session = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        chunks = [items[at:at + 200] for at in range(0, len(items), 200)]
        for part, chunk in enumerate(chunks):
            last = part == len(chunks) - 1
            body = {"session": session, "part": part, "complete": last, "items": chunk}
            if last and force:
                body["force"] = True
            answer = self.rex("PUT", "/rex/feed/%s/snapshot" % self.config["source"], body)
            for rejected in answer.get("rejected") or []:
                LOG.warning("rex rejected %s: %s", rejected.get("id"), rejected.get("reason"))
        self.pushed = len(items)
        LOG.info("pushed %d of %d channels: %s", len(items), len(listed),
                 {key: answer.get(key) for key in ("created", "updated", "removed")})

    def forever(self):
        while True:
            try:
                self.once()
            except Exception as error:
                LOG.warning("sync: %s", error)
            time.sleep(self.config["interval_minutes"] * 60)


def handler(config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            match = re.fullmatch(r"/%s/(%s)\.m3u8" % (re.escape(config["directory"]), CHANNEL.pattern),
                                 urllib.parse.urlsplit(self.path).path)
            if not match:
                self.send_error(404)
                return
            self.send_response(302)
            self.send_header("Location", "%s/stream/%s" % (config["live_sports_url"], match[1]))
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_GET

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true", help="sync once, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not os.path.exists(args.config):
        with open(args.config, "w") as handle:
            json.dump({key: os.environ.get("REX_LIVE_SPORTS_" + key.upper()) or DEFAULTS[key]
                       for key in ("live_sports_url", "rex_url", "listen")}, handle, indent=2)
    config = load_config(args.config)
    sync = Sync(config)
    if args.once:
        sync.once()
        return 0
    host, port = config["listen"].rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), handler(config))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("relay on http://%s/, live-sports at %s, rex at %s", config["listen"], config["live_sports_url"], config["rex_url"])
    sync.forever()


if __name__ == "__main__":
    sys.exit(main())
