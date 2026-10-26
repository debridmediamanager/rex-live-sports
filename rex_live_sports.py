#!/usr/bin/env python3
"""rex-live-sports: the channels of a live-sports server as a rex library.

live-sports (https://github.com/yowmamasita/live-sports-public) finds live sports channels,
checks which play and serves each one as HLS. This puts the ones that play into rex, as a "Live
Sports" library with a collection per group ("Live: Football", "Sports: UK"), so they play in
every Plex app. Nothing is recorded: rex reads the channel from live-sports when someone presses
play.

One process does two things:

* a relay rex reads from: `GET /<directory>/<channel name>.m3u8` answers 302 to live-sports'
  `/stream/<channel id>` of the copy listed now (rex takes an HLS playlist by its `.m3u8` name,
  which live-sports' own path does not have), and `GET /<directory>/art/<picture>.png` serves
  each channel's poster and background.
* a sync loop that registers with rex's feed API (`docs/feed-api.md` in every rex download)
  and pushes every channel that plays, each probed once a day.

The pictures are made here, with ffmpeg: rex fetches artwork only from public hosts or from the
source itself, and live-sports' own poster is an SVG on your network. A channel iptv-org has a
logo for gets that logo; any other its name, as live-sports draws it.

Standard library only; ffprobe and ffmpeg are separate executables.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
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
    "ffmpeg": "ffmpeg",
    "interval_minutes": 5,
    "probe_hours": 24,
}

CHANNEL = re.compile(r"[A-Za-z0-9_.-]+")

AGENT = "rex-live-sports (https://github.com/debridmediamanager/rex-live-sports)"

IPTV_ORG = "https://iptv-org.github.io/api"

# live-sports' own poster colour, behind its name card and behind a logo drawn in light colours.
DARK, LIGHT = "0x1a1a2e", "0xf2f2f2"

# (width, height, logo box): a film's 2:3 poster and a 16:9 background.
SHAPES = {"poster": (600, 900, 480), "art": (1280, 720, 400)}


def get_json(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": "rex-live-sports", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def fetch(url):
    """A picture's bytes. Wikimedia, which holds many logos, answers 429 to a burst: asked again
    once, after the wait it names."""
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read(16 << 20)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt:
                raise
            time.sleep(min(float(error.headers.get("Retry-After") or 5), 30))


def load_config(path):
    with open(path) as handle:
        config = dict(DEFAULTS, **json.load(handle))
    if not os.path.isabs(config["cache"]):
        config["cache"] = os.path.join(os.path.dirname(os.path.abspath(path)), config["cache"])
    config["relay_url"] = (config["relay_url"] or "http://" + config["listen"]).rstrip("/")
    config["live_sports_url"] = config["live_sports_url"].rstrip("/")
    return config


def channels(config, catalog):
    """The catalog's channels in the chosen groups, as (key, channel id, meta). live-sports lists
    only channels whose stream passed its last check, one copy of each channel it has several
    of, and which copy can change from one check to the next (measured while it starts). So rex
    knows a channel by its name (`key`), and the relay plays whichever copy is listed now."""
    out, keys = [], set()
    for meta in catalog.get("metas") or []:
        if not isinstance(meta, dict) or not str(meta.get("id", "")).startswith("lstv-"):
            continue
        channel, group = meta["id"][len("lstv-"):], str(meta.get("genre") or "")
        if CHANNEL.fullmatch(channel) and group.startswith(tuple(config["groups"])):
            key = re.sub(r"[^a-z0-9]+", "-", str(meta.get("name") or "").lower()).strip("-") or channel
            while key in keys:
                key += "-2"
            keys.add(key)
            out.append((key, channel, meta))
    return out


def plain(name):
    """A channel name as names are compared: no `(Country)`, no HD/TV, letters and digits only."""
    name = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", name.lower())
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\b(?:hd|fhd|uhd|4k|sd|tv)\b", "", name))


def logo_index(channels_json, logos_json, countries_json):
    """iptv-org's channels -> {plain name: [(country code, logo url), ...]}, each channel by its
    name and its other names, with the logo of the channel itself (not of one of its feeds)."""
    logos = {}
    for logo in logos_json:
        if logo.get("feed") is None and logo.get("in_use") and str(logo.get("url", "")).startswith("https://"):
            logos.setdefault(logo["channel"], logo["url"])
    index = {}
    for channel in channels_json:
        if channel.get("id") in logos and not channel.get("closed"):
            for name in [channel.get("name") or ""] + list(channel.get("alt_names") or []):
                if plain(name):
                    index.setdefault(plain(name), []).append((channel.get("country"), logos[channel["id"]]))
    return {"names": index, "countries": {str(country.get("name", "")).lower(): country.get("code") for country in countries_json}}


def logo_for(index, name):
    """The logo of the channel iptv-org knows by this name, the one of the country live-sports
    names in it (`ESPN (Argentina)`, or `ESPN Argentina` when iptv-org has no channel of that
    whole name) when there are several."""
    found = index["names"].get(plain(name)) or []
    country = re.search(r"\(([^)]+)\)\s*$", name)
    code = country and index["countries"].get(country[1].lower())
    if not found:
        for country, at in index["countries"].items():
            if plain(name).endswith(plain(country)) and index["names"].get(plain(name)[:-len(plain(country))]):
                found, code = index["names"][plain(name)[:-len(plain(country))]], at
                break
    return next((url for at, url in found if at == code), found[0][1] if found else None)


def item(config, key, meta, probe, art=None):
    """One feed item: a live channel nothing identifies, named by `display`, in its group's
    collection. Its own release, or rex would group every channel of the folder as one."""
    path = "%s/%s.m3u8" % (config["directory"], key)
    group = str(meta.get("genre") or "")
    art = art or {}
    display = {"title": meta.get("name") or key, "summary": group, "thumb": art.get("poster"), "art": art.get("art")}
    return {
        "id": "ls-" + key,
        "path": path,
        "file": "%s/%s" % (config["mount"].rstrip("/"), path),
        "name": meta.get("name") or key,
        "release": "live-sports:" + key,
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
        self.short_since = None
        self.picky = None
        self.logos, self.logos_at = None, 0
        self.routes = {}  # key -> the channel id the relay plays it from

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

    def logo_index(self):
        """iptv-org's logos, asked once a day; the last good copy when iptv-org does not answer."""
        path = os.path.join(self.config["cache"], "iptv-org-logos.json")
        if self.logos is not None and time.time() - self.logos_at < 86400:
            return self.logos
        try:
            fresh = logo_index(*(get_json("%s/%s.json" % (IPTV_ORG, name), 120) for name in ("channels", "logos", "countries")))
            os.makedirs(self.config["cache"], exist_ok=True)
            with open(path + ".tmp", "w") as handle:
                json.dump(fresh, handle)
            os.replace(path + ".tmp", path)
            self.logos = fresh
        except Exception as error:
            LOG.warning("iptv-org logos: %s", error)
            try:
                with open(path) as handle:
                    self.logos = json.load(handle)
            except (OSError, ValueError):
                self.logos = {"names": {}, "countries": {}}
        self.logos_at = time.time()
        return self.logos

    def luma(self, source, background):
        """The picture's mean brightness (0-255) drawn on a background, by ffmpeg."""
        out = subprocess.run([self.config["ffmpeg"], "-v", "error", "-i", source, "-filter_complex",
                              "color=%s:s=64x64[bg];[0]scale=64:64,format=rgba[fg];[bg][fg]overlay,signalstats,"
                              "metadata=print:key=lavfi.signalstats.YAVG:file=-" % background,
                              "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        found = re.search(r"YAVG=([\d.]+)", out.stdout)
        if not found:
            raise RuntimeError(out.stderr.strip()[-300:] or "ffmpeg read no picture")
        return float(found[1])

    def draw(self, source, background, box, shape, path):
        width, height, logo = SHAPES[shape]
        box = logo if box else min(width, height)
        out = subprocess.run([self.config["ffmpeg"], "-v", "error", "-y", "-i", source, "-filter_complex",
                              "color=%s:s=%dx%d[bg];[0]scale=%d:%d:force_original_aspect_ratio=decrease,format=rgba[fg];"
                              "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=rgb24" % (background, width, height, box, box),
                              "-frames:v", "1", path + ".tmp.png"], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[-300:] or "ffmpeg failed")
        os.replace(path + ".tmp.png", path)

    def artwork(self, key, meta):
        """The channel's poster and background, as rex reads them from the relay: its logo when
        iptv-org has one, else live-sports' name card. A picture is drawn once; its name carries
        what it was drawn from, so a new logo is a new path, which clients do not hold cached."""
        name = str(meta.get("name") or key)
        card = "%s/poster/%s.svg" % (self.config["live_sports_url"], urllib.parse.quote(name, safe=""))
        folder = os.path.join(self.config["cache"], "art")
        os.makedirs(folder, exist_ok=True)
        for url in [logo_for(self.logo_index(), name), card]:
            if not url:
                continue
            stem = "%s-%s" % (key, hashlib.sha1(url.encode()).hexdigest()[:8])
            out = {shape: os.path.join(folder, "%s-%s.png" % (stem, shape)) for shape in SHAPES}
            try:
                if not all(os.path.exists(path) for path in out.values()):
                    data = fetch(url)
                    if url == card:
                        # Square corners: the card is drawn on its own colour, where round ones leave marks.
                        data = data.replace(b' rx="16"', b"")
                    # ffmpeg tells an SVG only by its name; other pictures by their bytes.
                    source = os.path.join(folder, stem + (".svg" if data.lstrip()[:5] in (b"<?xml", b"<svg ") else ".source"))
                    with open(source, "wb") as handle:
                        handle.write(data)
                    try:
                        background = DARK
                        if url != card:
                            # How much the logo changes the two backgrounds says how much of it is
                            # drawn and how bright that is; a mostly light logo goes on the dark one.
                            on_black, on_white = self.luma(source, "black"), self.luma(source, "white")
                            drawn = 1 - (on_white - on_black) / 255
                            background = DARK if drawn > 0.05 and on_black / drawn > 150 else LIGHT
                        for shape, path in out.items():
                            self.draw(source, background, url != card, shape, path)
                    finally:
                        os.remove(source)
                return {shape: "%s/%s/art/%s" % (self.config["relay_url"], self.config["directory"], os.path.basename(path))
                        for shape, path in out.items()}
            except Exception as error:
                LOG.info("%s: picture from %s: %s", key, url.split("?")[0][:80], error)
        return {}

    def once(self):
        listed = channels(self.config, get_json(self.config["live_sports_url"] + "/catalog/tv/all.json"))
        if not listed:
            # live-sports lists nothing until its first checks finish after a start.
            LOG.info("live-sports lists no channel that plays yet")
            return
        self.register()
        self.routes.update({key: channel for key, channel, _ in listed})
        self.logo_index()
        with ThreadPoolExecutor(8) as pool:
            probes = list(pool.map(lambda row: self.probe(row[1]), listed))
            art = list(pool.map(lambda row, probe: self.artwork(row[0], row[2]) if probe else None, listed, probes))
        items = [item(self.config, key, meta, probe, pictures)
                 for (key, _, meta), probe, pictures in zip(listed, probes, art) if probe]
        if not items:
            return
        try:
            answer = self.snapshot(items, force=False)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            # rex refuses a snapshot that would remove more than half of what it holds (`of`).
            # Events end and others start, so that can be right; but a list under half of what
            # rex holds is more likely live-sports still checking after a start (measured: 36
            # of 750 channels), so that one is left to rex's guard, for half an hour.
            held = json.loads(error.read() or b"{}").get("of") or 0
            if len(items) * 2 < held:
                self.short_since = self.short_since or time.time()
                if time.time() - self.short_since < 1800:
                    LOG.info("live-sports lists %d channels where rex holds %d: waiting", len(items), held)
                    return
            answer = self.snapshot(items, force=True)
        self.short_since = None
        LOG.info("pushed %d of %d channels: %s", len(items), len(listed),
                 {key: answer.get(key) for key in ("created", "updated", "removed")})

    def snapshot(self, items, force):
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
        return answer

    def forever(self):
        while True:
            try:
                self.once()
            except Exception as error:
                LOG.warning("sync: %s", error)
            time.sleep(self.config["interval_minutes"] * 60)


def handler(config, routes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            picture = re.fullmatch(r"/%s/art/(%s\.png)" % (re.escape(config["directory"]), CHANNEL.pattern),
                                   urllib.parse.urlsplit(self.path).path)
            if picture:
                try:
                    with open(os.path.join(config["cache"], "art", picture[1]), "rb") as handle:
                        body = handle.read()
                except OSError:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            match = re.fullmatch(r"/%s/(%s)\.m3u8" % (re.escape(config["directory"]), CHANNEL.pattern),
                                 urllib.parse.urlsplit(self.path).path)
            if not match or match[1] not in routes:
                self.send_error(404)
                return
            self.send_response(302)
            self.send_header("Location", "%s/stream/%s" % (config["live_sports_url"], routes[match[1]]))
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
    server = ThreadingHTTPServer((host, int(port)), handler(config, sync.routes))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("relay on http://%s/, live-sports at %s, rex at %s", config["listen"], config["live_sports_url"], config["rex_url"])
    sync.forever()


if __name__ == "__main__":
    sys.exit(main())
