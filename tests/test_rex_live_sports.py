"""rex-live-sports against a real live-sports catalog (zen, 2026-10-26), a real channel's probe
and iptv-org's real channel and logo lists, cut to the names in that catalog; and real guides
(2026-11-01): Pluto TV's schedule of three of its channels, epgshare01's listing and its UK
guide, cut to four channels from 08:00 to 20:00 UTC. For lineups: live-sports' catalog with its
keys (zen, 2026-11-01 23:48 UTC), cut to the default lineups' channels and its `Live:` groups, and
ESPN's scoreboards of Sunday and Monday as they were at 23:37 UTC, Chiefs at Broncos in its fourth
quarter (the NBA's Monday cut to three games).

`AgainstRex` also syncs into a real rex binary: REX_BIN.

    REX_BIN=/path/to/rex python3 -m unittest discover -s tests
"""
import copy
import gzip
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import rex_live_sports  # noqa: E402

with open(os.path.join(HERE, "fixtures", "catalog.json")) as _handle:
    CATALOG = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "probe-dm-sky-sports-racing.json")) as _handle:
    PROBE = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "probe-slate-cv-2897253.json")) as _handle:
    SLATE = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "iptv-org.json")) as _handle:
    IPTV_ORG = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "pluto-tv.json")) as _handle:
    PLUTO_TV = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "epgshare01.html"), "rb") as _handle:
    EPGSHARE01 = _handle.read()
with open(os.path.join(HERE, "fixtures", "epgshare01-UK1.xml.gz"), "rb") as _handle:
    EPGSHARE01_UK = _handle.read()
with open(os.path.join(HERE, "fixtures", "catalog-keyed.json")) as _handle:
    CATALOG_KEYED = json.load(_handle)
ESPN = {}
for _short, _league in (("nfl", "football/nfl"), ("nba", "basketball/nba"), ("eng.1", "soccer/eng.1"), ("f1", "racing/f1")):
    with open(os.path.join(HERE, "fixtures", "espn-%s.json" % _short)) as _handle:
        ESPN[_league] = json.load(_handle)  # {day: scoreboard}
NOW = rex_live_sports.epoch("2026-11-01T12:00:00Z")  # when the guides were fetched
ESPN_NOW = rex_live_sports.epoch("2026-11-01T23:37:00Z")  # when the scoreboards were
US = os.path.join(ROOT, "lineups", "us.json")
PNG = b"\x89PNG\r\n\x1a\n"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(routes, calls):
    """A live-sports and a rex in one: GET answers `routes`, PUT records its body in `calls`. A
    snapshot that would remove more than half of what it holds is refused unless forced, as rex
    refuses it (`docs/feed-api.md`)."""
    held, session = set(), set()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            body = routes.get(self.path, routes.get(self.path.split("?")[0]))
            if body is None:
                self.send_error(404)
            elif isinstance(body, bytes):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.answer(200, body)

        def do_PUT(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if body.get("part") == 0:
                session.clear()
            session.update(pushed["id"] for pushed in body.get("items") or [])
            if body.get("complete"):
                if len(held - session) * 2 > len(held) and not body.get("force"):
                    self.answer(409, {"error": "would remove more than half", "would_remove": len(held - session), "of": len(held)})
                    return
                held.clear()
                held.update(session)
            self.answer(200, {"created": 1})

    class Server(ThreadingHTTPServer):
        request_queue_size = 64  # ESPN's scoreboards are asked eight at a time
    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def config(work, base, **extra):
    path = os.path.join(work, "config.json")
    with open(path, "w") as handle:
        json.dump(dict({"live_sports_url": base, "rex_url": base, "ffprobe": os.path.join(HERE, "fake_ffprobe.py"),
                        "ffmpeg": os.path.join(HERE, "fake_ffmpeg.py")}, **extra), handle)
    return rex_live_sports.load_config(path)


def offline(test, base, refused=()):
    """iptv-org's lists and the guides from the fixtures, served at `base`, and logos fetched from
    nowhere: a PNG naming its url, or a 404 for a url in `refused`."""
    for name, url in (("IPTV_ORG", base + "/iptv-org"), ("EPGSHARE01", base + "/epgshare01/"), ("PLUTO_TV", base + "/pluto"),
                      ("ESPN", base + "/espn/")):
        test.addCleanup(setattr, rex_live_sports, name, getattr(rex_live_sports, name))
        setattr(rex_live_sports, name, url)

    def fetch(url):
        if url in refused:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return PNG + url.encode()
    patcher = unittest.mock.patch.object(rex_live_sports, "fetch", fetch)
    patcher.start()
    test.addCleanup(patcher.stop)


def iptv_routes():
    """iptv-org's lists and the guides; epgshare01's other files are not found."""
    return dict({"/iptv-org/%s.json" % name: IPTV_ORG[name] for name in ("channels", "logos", "countries")},
                **{"/epgshare01/": EPGSHARE01, "/epgshare01/epg_ripper_UK1.xml.gz": EPGSHARE01_UK, "/pluto": PLUTO_TV})


def espn_routes():
    """ESPN's scoreboards; its other leagues and days are not found."""
    return {"/espn/%s/scoreboard?dates=%s" % (league, day): answer for league, days in ESPN.items() for day, answer in days.items()}


class Channels(unittest.TestCase):
    def test_sports_groups_only(self):
        listed = rex_live_sports.channels(rex_live_sports.DEFAULTS, CATALOG)
        self.assertEqual(len(CATALOG["metas"]), 1117)
        self.assertEqual(len(listed), 624)
        groups = {meta["genre"] for _, _, meta in listed}
        self.assertIn("Live: Football", groups)
        self.assertIn("Sports: UK", groups)
        self.assertNotIn("TV: Other", groups)
        self.assertFalse(any(group.startswith("Pluto") for group in groups))
        self.assertIn(("sky-sports-racing", "dm-sky-sports-racing"), [(key, channel) for key, channel, _ in listed])
        # live-sports lists some names twice (two copies it does not count as one).
        self.assertEqual(len({key for key, _, _ in listed}), 624)
        self.assertIn("sky-sports-racing-uk-2", {key for key, _, _ in listed})

    def test_a_channel_keeps_its_key_when_live_sports_lists_another_copy(self):
        meta = next(meta for meta in CATALOG["metas"] if meta["id"] == "lstv-dm-sky-sports-racing")
        before = rex_live_sports.channels(rex_live_sports.DEFAULTS, {"metas": [meta]})
        after = rex_live_sports.channels(rex_live_sports.DEFAULTS, {"metas": [dict(meta, id="lstv-cv-123")]})
        self.assertEqual([(key, channel) for key, channel, _ in before + after],
                         [("sky-sports-racing", "dm-sky-sports-racing"), ("sky-sports-racing", "cv-123")])

    def test_ids_a_path_cannot_hold_are_left_out(self):
        catalog = {"metas": [{"id": "lstv-a/b", "genre": "Sports"}, {"id": "other-1", "genre": "Sports"}, "junk",
                             {"id": "lstv-ok", "genre": "Sports"}]}
        self.assertEqual([channel for _, channel, _ in rex_live_sports.channels(rex_live_sports.DEFAULTS, catalog)], ["ok"])

    def test_logos(self):
        index = rex_live_sports.logo_index(IPTV_ORG["channels"], IPTV_ORG["logos"], IPTV_ORG["countries"])
        self.assertEqual(rex_live_sports.logo_for(index, "Sky Sports Racing"),
                         "https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/512/512/skychb1354.png")
        # By another name of the channel's (iptv-org's FoxCricket.au is also Fox Sports 501).
        self.assertEqual(rex_live_sports.logo_for(index, "Fox Sports 501 (Australia)"), "https://i.imgur.com/V8hb6tJ.png")
        self.assertIsNone(rex_live_sports.logo_for(index, "Geylang vs Hougang"))
        # live-sports' other spelling of a country's channel (kids, 2026-10-26).
        self.assertEqual(rex_live_sports.logo_for(index, "ESPN Premium Argentina"),
                         "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/ESPN_Premium_logo.svg/960px-ESPN_Premium_logo.svg.png")
        listed = rex_live_sports.channels(rex_live_sports.DEFAULTS, CATALOG)
        self.assertEqual(sum(1 for _, _, meta in listed if rex_live_sports.logo_for(index, meta["name"])), 486)

    def test_the_logo_of_the_country_named(self):
        index = {"names": {"espn": [("US", "us.png"), ("AR", "ar.png")]}, "countries": {"argentina": "AR"}}
        self.assertEqual(rex_live_sports.logo_for(index, "ESPN (Argentina)"), "ar.png")
        self.assertEqual(rex_live_sports.logo_for(index, "ESPN (Chile)"), "us.png")
        self.assertEqual(rex_live_sports.logo_for(index, "ESPN HD"), "us.png")
        self.assertEqual(rex_live_sports.logo_for(index, "ESPN Argentina"), "ar.png")

    def test_item(self):
        meta = next(meta for meta in CATALOG["metas"] if meta["id"] == "lstv-dm-sky-sports-racing")
        pushed = rex_live_sports.item(dict(rex_live_sports.DEFAULTS), "sky-sports-racing", meta, PROBE,
                                      {"poster": "http://relay/p.png", "art": "http://relay/a.png"})
        self.assertEqual(pushed["id"], "ls-sky-sports-racing")
        self.assertEqual(pushed["path"], "live-sports/sky-sports-racing.m3u8")
        self.assertEqual(pushed["file"], "/mnt/rex-live-sports/live-sports/sky-sports-racing.m3u8")
        self.assertEqual(pushed["release"], "live-sports:sky-sports-racing")
        self.assertEqual((pushed["display"]["thumb"], pushed["display"]["art"]), ("http://relay/p.png", "http://relay/a.png"))
        self.assertEqual(pushed["collections"], ["Live: 24/7 Streams"])
        self.assertEqual(pushed["display"]["title"], "Sky Sports Racing")
        self.assertNotIn("kind", pushed)  # nothing identifies a channel
        self.assertNotIn("size", pushed)
        self.assertTrue(rex_live_sports.playable(pushed["media_info"]))

    def test_playable(self):
        self.assertFalse(rex_live_sports.playable({"format": {"format_name": "hls"}, "streams": [{"codec_type": "audio"}]}))
        self.assertFalse(rex_live_sports.playable({"format": {"format_name": "mpegts"}, "streams": PROBE["streams"]}))
        # live-sports' placeholder while a channel warms up: a 0x0 png and no sound. rex
        # refused every conversion of the six channels pushed with it (fun, 2026-10-27).
        self.assertFalse(rex_live_sports.playable(SLATE))


class SyncOnce(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="rex-live-sports-")
        self.calls = []
        self.catalog = {"metas": CATALOG["metas"][:]}
        self.server, base = serve(dict(iptv_routes(), **{"/catalog/tv/all.json": self.catalog}), self.calls)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        offline(self, base, refused={"https://i.imgur.com/V8hb6tJ.png"})
        self.config = config(self.work, base)
        self.sync = rex_live_sports.Sync(self.config)

    def snapshots(self):
        return [body for path, body in self.calls if path == "/rex/feed/live-sports/snapshot"]

    def test_pushes_every_channel_that_plays(self):
        os.environ["FAKE_FFPROBE_FAIL"] = "dm-nfl-network"
        self.addCleanup(os.environ.pop, "FAKE_FFPROBE_FAIL")
        self.sync.once()
        path, registered = self.calls[0]
        self.assertEqual(path, "/rex/sources/live-sports")
        self.assertEqual(registered["directories"], [{"name": "live-sports", "movies": "Live Sports"}])
        self.assertIs(registered["trash"], False)
        self.assertEqual(registered["url"], "http://127.0.0.1:9983")
        parts = self.snapshots()
        self.assertEqual([part["part"] for part in parts], [0, 1, 2, 3])
        self.assertEqual([part["complete"] for part in parts], [False, False, False, True])
        pushed = {item["id"]: item for part in parts for item in part["items"]}
        # 624 play; five names are listed twice and both copies play, and NFL Network's other
        # copy (dl-405) plays where dm-nfl-network is refused: one row a name.
        self.assertEqual(len(pushed), 619)
        names = [item["name"].strip().lower() for item in pushed.values()]
        self.assertEqual(len(names), len(set(names)), [n for n in set(names) if names.count(n) > 1])
        refused = next(key for key, channel in self.sync.routes.items() if channel == "dm-nfl-network")
        self.assertNotIn("ls-" + refused, pushed)
        self.assertIn("NFL Network", [item["name"] for item in pushed.values()])
        self.assertNotIn("force", parts[-1])  # nothing in rex to remove
        self.assertEqual(len([name for name in os.listdir(self.config["cache"]) if name.endswith(".json")]), 625)  # + iptv-org's, channels
        self.assertEqual(self.sync.routes["sky-sports-racing"], "dm-sky-sports-racing")
        # Every channel has its pictures, under the relay's url: rex fetches artwork from the
        # source itself, never from a private address it was not registered with.
        art = os.path.join(self.config["cache"], "art")
        for item in pushed.values():
            for field in ("thumb", "art"):
                url = item["display"][field]
                self.assertTrue(url.startswith("http://127.0.0.1:9983/live-sports/art/"), url)
                self.assertTrue(os.path.exists(os.path.join(art, url.rsplit("/", 1)[1])), url)
        self.assertEqual(len(os.listdir(art)), 623 * 2)  # no source picture left behind
        with open(os.path.join(art, pushed["ls-sky-sports-racing"]["display"]["thumb"].rsplit("/", 1)[1]), "rb") as handle:
            self.assertIn(b"skychb1354.png", handle.read())  # drawn from its logo
        with open(os.path.join(art, pushed["ls-geylang-vs-hougang"]["display"]["thumb"].rsplit("/", 1)[1]), "rb") as handle:
            self.assertEqual(handle.read(), b"TITLE:Geylang vs Hougang")  # no logo: its name
        # Fox Sports 501's logo does not open: its name instead.
        with open(os.path.join(art, pushed["ls-fox-sports-501-australia"]["display"]["thumb"].rsplit("/", 1)[1]), "rb") as handle:
            self.assertEqual(handle.read(), b"TITLE:Fox Sports 501 (Australia)")

    def test_a_dark_logo_goes_on_a_light_background(self):
        commands = []
        run = subprocess.run
        with unittest.mock.patch.object(rex_live_sports.subprocess, "run", lambda command, **kw: commands.append(command) or run(command, **kw)):
            self.sync.artwork("sky-sports-racing", {"name": "Sky Sports Racing"})
            self.sync.artwork("geylang-vs-hougang", {"name": "Geylang vs Hougang"})
        logos = [command[command.index("-filter_complex") + 1] for command in commands
                 if "-filter_complex" in command and "signalstats" not in " ".join(command)]
        names = [command[command.index("-i") + 1] for command in commands if "lavfi" in command]
        self.assertEqual(len(logos), 2)
        self.assertTrue(all(graph.startswith("color=0xf2f2f2") for graph in logos), logos)  # fake ffmpeg: a dark logo
        self.assertEqual(names, ["color=0x1a1a2e:s=600x900", "color=0x1a1a2e:s=1280x720"])

    def test_a_name_is_drawn_as_it_is_written(self):
        commands = []
        run = subprocess.run
        with unittest.mock.patch.object(rex_live_sports.subprocess, "run", lambda command, **kw: commands.append(command) or run(command, **kw)):
            pictures = self.sync.artwork("x", {"name": "Tennis ATP Basel: Fritz – Shelton 100% 'live'"})
        poster = commands[0][commands[0].index("-vf") + 1]
        # Wrapped to lines; `%` is no expansion (gyan.dev's ffmpeg 9 refuses the frame, Debian's
        # 7.1 draws nothing); the name goes in through files, so nothing in it needs escaping.
        self.assertEqual(poster.count("drawtext="), 4)
        self.assertEqual(poster.count("expansion=none"), 4)
        self.assertNotIn("Fritz", poster)
        with open(os.path.join(self.config["cache"], "art", pictures["poster"].rsplit("/", 1)[1]), "rb") as handle:
            self.assertEqual(handle.read(), "TITLE:Tennis ATP Basel: Fritz – Shelton 100% 'live'".encode())
        self.assertEqual(sorted(os.listdir(os.path.join(self.config["cache"], "art"))), sorted(url.rsplit("/", 1)[1] for url in pictures.values()))

    def test_pictures_are_drawn_once(self):
        first = self.sync.artwork("sky-sports-racing", {"name": "Sky Sports Racing"})
        with unittest.mock.patch.object(rex_live_sports, "fetch", side_effect=AssertionError("fetched again")):
            self.assertEqual(self.sync.artwork("sky-sports-racing", {"name": "Sky Sports Racing"}), first)

    def test_probes_are_kept(self):
        self.sync.once()
        os.environ["FAKE_FFPROBE_FAIL"] = "dm-nfl-network"  # would fail if it were asked again
        self.addCleanup(os.environ.pop, "FAKE_FFPROBE_FAIL")
        self.calls.clear()
        self.sync.once()
        self.assertEqual(sum(len(part["items"]) for part in self.snapshots()), 619)

    def test_a_placeholder_is_probed_again(self):
        os.environ["FAKE_FFPROBE_SLATE"] = "dm-nfl-network"
        self.addCleanup(os.environ.pop, "FAKE_FFPROBE_SLATE", None)
        self.sync.once()
        path = os.path.join(self.config["cache"], "dm-nfl-network.json")
        self.assertFalse(os.path.exists(path))  # not pushed, not kept
        self.assertEqual(sum(len(part["items"]) for part in self.snapshots()), 619)  # NFL Network by dl-405
        os.environ.pop("FAKE_FFPROBE_SLATE")
        self.calls.clear()
        self.sync.once()
        self.assertEqual(sum(len(part["items"]) for part in self.snapshots()), 619)
        self.assertTrue(os.path.exists(path))  # probed again, and kept

    def test_a_kept_placeholder_is_probed_again(self):
        # Probes kept before placeholders were refused stay a day otherwise.
        self.sync.once()
        path = os.path.join(self.config["cache"], "dm-nfl-network.json")
        with open(path, "w") as handle:
            json.dump(SLATE, handle)
        self.calls.clear()
        self.sync.once()
        with open(path) as handle:
            self.assertEqual(json.load(handle)["streams"][0]["codec_name"], "h264")

    def test_interlacing_is_read_from_frames(self):
        # A live channel joined mid-GOP probes with field_order unknown; rex needs it to know the
        # picture is interlaced, which Chrome cannot play as rex copies it (fun, 2026-10-27).
        os.environ["FAKE_FFPROBE_INTERLACED"] = "dm-nfl-network"
        self.addCleanup(os.environ.pop, "FAKE_FFPROBE_INTERLACED", None)
        self.sync.once()
        orders = {}
        for name in ("dm-nfl-network", "dm-sky-sports-racing"):
            with open(os.path.join(self.config["cache"], name + ".json")) as handle:
                orders[name] = [stream.get("field_order") for stream in json.load(handle)["streams"] if stream["codec_type"] == "video"]
        self.assertEqual(orders, {"dm-nfl-network": ["tt"], "dm-sky-sports-racing": ["progressive"]})

    def test_a_kept_probe_without_field_order_is_probed_again(self):
        self.sync.once()
        path = os.path.join(self.config["cache"], "dm-nfl-network.json")
        with open(path, "w") as handle:
            json.dump(PROBE, handle)  # as kept before field_order was read
        os.environ["FAKE_FFPROBE_INTERLACED"] = "dm-nfl-network"
        self.addCleanup(os.environ.pop, "FAKE_FFPROBE_INTERLACED", None)
        self.calls.clear()
        self.sync.once()
        with open(path) as handle:
            self.assertEqual(json.load(handle)["streams"][0]["field_order"], "tt")

    def test_a_list_under_half_of_what_rex_holds_is_not_forced(self):
        self.sync.once()
        self.calls.clear()
        self.catalog["metas"][:] = [meta for meta in CATALOG["metas"] if meta["genre"].startswith("Live:")]
        self.sync.once()  # rex refuses it; nothing forced
        parts = self.snapshots()
        self.assertLess(sum(len(part["items"]) for part in parts) * 2, 624)
        self.assertFalse(any(part.get("force") for part in parts))
        self.sync.short_since -= 1801  # still short half an hour on: the events ended
        self.calls.clear()
        self.sync.once()
        self.assertTrue(self.snapshots()[-1]["force"])

    def test_a_restart_beside_live_sports_is_not_forced(self):
        # kids, 2026-10-26: both restarted; live-sports listed 36 of 750 channels while it
        # checked them, and a forced push trashed 718 of them in rex.
        self.sync.once()
        self.calls.clear()
        self.catalog["metas"][:] = CATALOG["metas"][:36]
        rex_live_sports.Sync(self.config).once()
        self.assertFalse(any(part.get("force") for part in self.snapshots()))

    def test_churn_of_the_same_size_is_forced(self):
        self.sync.once()
        self.calls.clear()
        self.catalog["metas"][:] = [dict(meta, id=meta["id"] + "-next", name=meta["name"] + " next") for meta in CATALOG["metas"]]
        self.sync.once()
        parts = self.snapshots()
        self.assertEqual([part.get("force", False) for part in parts if part["complete"]], [False, True])  # refused, then forced

    def test_another_copy_of_a_channel_is_the_same_item(self):
        self.sync.once()
        before = {item["id"] for part in self.snapshots() for item in part["items"]}
        self.calls.clear()
        self.catalog["metas"][:] = [dict(meta, id=meta["id"] + "-copy") for meta in CATALOG["metas"]]
        self.sync.once()
        self.assertEqual({item["id"] for part in self.snapshots() for item in part["items"]}, before)  # nothing for rex to remove
        self.assertEqual(self.sync.routes["sky-sports-racing"], "dm-sky-sports-racing-copy")

    def test_nothing_listed_pushes_nothing(self):
        self.catalog["metas"][:] = []
        self.sync.once()
        self.assertEqual(self.calls, [])

    def pushed(self):
        return {item["id"]: item for part in self.snapshots() for item in part["items"]}

    def test_every_channel_is_a_live_tv_channel(self):
        self.sync.now = lambda: NOW
        self.sync.once()
        channels = {key: item["channel"] for key, item in self.pushed().items()}
        self.assertEqual(len(channels), 619)  # one row a channel name (bd00b1d)
        self.assertEqual(len({channel["number"] for channel in channels.values()}), 619)
        football = channels["ls-sky-sports-football-uk"]
        self.assertEqual({key: football[key] for key in ("title", "call_sign")},
                         {"title": "Sky Sports Football (UK)", "call_sign": "Sky Sports Football"})
        # Matched by its name and its country in epgshare01's UK guide: from three hours back.
        self.assertEqual(football["guide"][0], {
            "start": NOW - 3 * 3600, "end": NOW - 3 * 3600 + 900, "title": "EFL Highlights",
            "summary": "Bolton Wanderers v Bristol City in the Championship.",
            "thumb": "https://tv.assets.pressassociation.io/7bf3220a-0429-5350-b6f8-1206c2cf015a.jpg"})
        self.assertEqual(channels["ls-sky-sports-football-uk-2"]["guide"], football["guide"])  # `Sky Sports Football UK`
        self.assertEqual(channels["ls-mutv-uk"]["guide"][0]["categories"], ["Sports", "Football"])
        # A match, and a channel no guide has: its name, from the block before now to a day on.
        self.assertEqual(channels["ls-geylang-vs-hougang"]["guide"], rex_live_sports.placeholder(
            next(meta for meta in CATALOG["metas"] if meta["name"] == "Geylang vs Hougang"), NOW))
        self.assertEqual(channels["ls-geylang-vs-hougang"]["guide"][0]["categories"], ["Sports", "Singapore Premier League"])
        # A guide that has not moved is not sent again: rex keeps the channel of an item pushed
        # without one. A block later the names' guides have, epgshare01's not.
        self.calls.clear()
        self.sync.once()
        self.assertEqual([key for key, item in self.pushed().items() if "channel" in item], [])
        self.calls.clear()
        self.sync.now = lambda: NOW + 6 * 3600
        self.sync.once()
        sent = [key for key, item in self.pushed().items() if "channel" in item]
        self.assertIn("ls-geylang-vs-hougang", sent)
        self.assertNotIn("ls-sky-sports-football-uk", sent)
        # A channel live-sports stops listing is sent whole when it comes back.
        meta = next(meta for meta in self.catalog["metas"] if meta["name"] == "Geylang vs Hougang")
        self.catalog["metas"].remove(meta)
        self.sync.once()
        self.catalog["metas"].append(meta)
        self.calls.clear()
        self.sync.once()
        self.assertEqual([key for key, item in self.pushed().items() if "channel" in item], ["ls-geylang-vs-hougang"])

    def test_live_tv_off_ends_the_channels(self):
        sync = rex_live_sports.Sync(config(self.work, self.config["live_sports_url"], live_tv=False))
        sync.once()
        self.assertEqual({repr(item["channel"]) for item in self.pushed().values()}, {"None"})
        self.assertFalse(os.path.exists(os.path.join(sync.config["cache"], "channels.json")))

    def test_pluto_channels_have_pluto_tvs_guide(self):
        sync = rex_live_sports.Sync(config(self.work, self.config["live_sports_url"], groups=["Pluto:"]))
        sync.now = lambda: NOW
        sync.once()
        channels = {item["name"]: item["channel"] for item in self.pushed().values()}
        self.assertEqual(len(channels), 195)
        jump = channels["21 Jump Street"]["guide"]
        self.assertEqual(jump, [rex_live_sports.pluto_programme(entry) for entry in PLUTO_TV[2]["timelines"]])
        self.assertEqual(channels["Pluto TV Kultfilme"]["guide"][0]["title"], "Pluto TV Kultfilme")  # not in the fixture


class Numbers(unittest.TestCase):
    def setUp(self):
        self.config = {"cache": tempfile.mkdtemp(prefix="rex-live-sports-")}
        self.metas = {"ls-" + key: meta for key, _, meta in rex_live_sports.channels(rex_live_sports.DEFAULTS, CATALOG)}

    def test_groups_cluster_in_the_order_of_their_names(self):
        numbers = rex_live_sports.Sync(self.config).numbers(self.metas, NOW)
        groups = sorted({meta["genre"] for meta in self.metas.values()})
        for key, meta in self.metas.items():
            self.assertEqual(numbers[key].split(".")[0], str(groups.index(meta["genre"]) + 100), key)
        self.assertEqual(groups[0], "Live: 24/7 Streams")
        self.assertEqual(numbers["ls-sky-sports-racing"], "100.1")
        uk = sorted((meta["name"], numbers[key]) for key, meta in self.metas.items() if meta["genre"] == "Sports: UK")
        self.assertEqual([number for _, number in uk], ["%d.%d" % (groups.index("Sports: UK") + 100, at) for at in range(1, len(uk) + 1)])

    def test_a_channel_keeps_its_number(self):
        numbers = rex_live_sports.Sync(self.config).numbers(self.metas, NOW)
        # Restarted, listed in another order, a channel gone, another moved to another group.
        metas = dict(reversed(list(self.metas.items())))
        del metas["ls-sky-sports-racing"]
        metas["ls-mutv-uk"] = dict(metas["ls-mutv-uk"], genre="Live: Football")
        again = rex_live_sports.Sync(self.config).numbers(metas, NOW + 86400)
        self.assertEqual(again, {key: numbers[key] for key in metas})
        # A new channel takes the lowest number free in its group; one gone a week frees its own.
        sync = rex_live_sports.Sync(self.config)
        new = dict(metas, **{"ls-new": dict(self.metas["ls-sky-sports-racing"], name="Racing 2")})
        self.assertEqual(sync.numbers(new, NOW + 86400)["ls-new"], "100.2")
        sync.numbers(new, NOW + 8 * 86400)
        self.assertEqual(sync.numbers(dict(new, **{"ls-new2": new["ls-new"]}), NOW + 8 * 86400)["ls-new2"], "100.1")

    def test_weeks_it_does_not_run_do_not_count(self):
        numbers = rex_live_sports.Sync(self.config).numbers(self.metas, NOW)
        some = dict(list(self.metas.items())[::2])
        self.assertEqual(rex_live_sports.Sync(self.config).numbers(some, NOW + 30 * 86400), {key: numbers[key] for key in some})

    def test_a_new_group_takes_the_lowest_free_number(self):
        sync = rex_live_sports.Sync(self.config)
        sync.numbers(self.metas, NOW)
        gone = {key: meta for key, meta in self.metas.items() if meta["genre"] != "Live: Afl"}
        sync.numbers(gone, NOW + 8 * 86400)
        event = {"ls-event": {"id": "lstv-x", "name": "A vs B", "genre": "Live: Darts"}}
        self.assertEqual(sync.numbers(dict(gone, **event), NOW + 8 * 86400)["ls-event"], "101.1")  # Live: Afl's


class Guides(unittest.TestCase):
    def test_pluto_tv(self):
        movies, red_bull, jump = ([rex_live_sports.pluto_programme(entry) for entry in channel["timelines"]] for channel in PLUTO_TV)
        self.assertEqual(jump[1], {
            "start": rex_live_sports.epoch("2026-11-01T10:31:21Z"), "end": rex_live_sports.epoch("2026-11-01T11:25:46Z"),
            "title": "Veränderungen", "type": "episode", "show": "21 Jump Street", "season": 2, "episode": 22,
            "summary": "In den Sommerferien pausiert das Jump Street-Programm. Ist das schon das Ende für das Projekt?",
            "categories": ["Action & Adventure", "Crime Action"], "thumb": jump[1]["thumb"], "previously_shown": True})
        self.assertTrue(jump[1]["thumb"].startswith("https://images.pluto.tv/episodes/"))
        # Pluto TV's own filler between programmes, with no information.
        self.assertEqual({key for key in jump[0] if key not in ("start", "end", "thumb")}, {"title", "previously_shown"})
        self.assertEqual(movies[1]["title"], "Der schwarze Hengst")
        self.assertNotIn("type", movies[1])  # a film
        self.assertEqual(red_bull[0]["title"], "Red Bull Soapbox Race")
        self.assertEqual(red_bull[0]["categories"], ["Sports"])
        self.assertNotIn("previously_shown", red_bull[0])  # live

    def test_xmltv(self):
        names, programmes = rex_live_sports.xmltv(gzip.GzipFile(fileobj=io.BytesIO(EPGSHARE01_UK)), "UK",
                                                  {"skyhistoryhd", "mutv", "nothing"}, NOW - 3 * 3600, NOW + 6 * 3600)
        self.assertEqual(names, {"skyhistoryhd": [("UK", "Sky.HistoryHD.uk")], "mutv": [("UK", "MUTV.HD.uk")]})
        history = programmes["Sky.HistoryHD.uk"]
        self.assertTrue(all(programme["end"] > NOW - 3 * 3600 and programme["start"] < NOW + 6 * 3600 for programme in history))
        self.assertEqual(len(history), 10)
        episodes = [programme for programme in history if programme.get("type") == "episode"]
        self.assertTrue(episodes)
        for programme in episodes:
            self.assertGreaterEqual(programme["season"], 1)  # xmltv_ns counts from 0
            self.assertGreaterEqual(programme["episode"], 1)
            self.assertNotEqual(programme["show"], programme["title"])

    def test_matched_by_name_and_country(self):
        index = rex_live_sports.logo_index(IPTV_ORG["channels"], IPTV_ORG["logos"], IPTV_ORG["countries"])
        sync = rex_live_sports.Sync({"cache": tempfile.mkdtemp(prefix="rex-live-sports-")})
        sync.logos, sync.logos_at = index, time.time()
        guides = {"names": {"tntsports1": [("UK", "TNT.Sports.1.HD.uk")], "maxsport2": [("BG", "MAX.Sport.2.bg")]},
                  "programmes": {"TNT.Sports.1.HD.uk": [{"start": 1, "end": 2, "title": "UEFA"}],
                                 "MAX.Sport.2.bg": [{"start": 1, "end": 2, "title": "Bulgarian"}]}}
        def guide(name, genre="Sports"):
            return sync.guide(guides, {"id": "lstv-x", "name": name, "genre": genre}, NOW)[0]["title"]
        self.assertEqual(guide("TNT Sports 1 (UK)"), "UEFA")
        self.assertEqual(guide("TNT Sports 1 UK"), "UEFA")
        self.assertEqual(guide("TNT SPORTS 1 .b", "Sports: UK"), "UEFA")
        self.assertEqual(guide("TNT Sports 1"), "UEFA")  # iptv-org's TNT Sports 1 is the UK's
        # Another country's channel of the name is not this one; with no country, the only one is.
        self.assertEqual(guide("TNT Sports 1 (Argentina)"), "TNT Sports 1 (Argentina)")
        self.assertEqual(guide("Max Sport 2 (Croatia)"), "Max Sport 2 (Croatia)")
        self.assertEqual(guide("Max Sport 2"), "Bulgarian")

    def test_placeholder(self):
        guide = rex_live_sports.placeholder({"name": "Real Madrid vs Barcelona - NO Delay", "genre": "Live: Football"}, NOW + 60)
        self.assertEqual([programme["start"] for programme in guide], [NOW - 6 * 3600 + 6 * 3600 * at for at in range(6)])
        self.assertEqual({programme["end"] - programme["start"] for programme in guide}, {6 * 3600})
        self.assertGreaterEqual(guide[-1]["end"], NOW + 24 * 3600)
        self.assertEqual(guide[0]["title"], "Real Madrid vs Barcelona - NO Delay")
        self.assertEqual(guide[0]["categories"], ["Sports", "Football"])
        self.assertNotIn("categories", rex_live_sports.placeholder({"name": "Arte", "genre": "TV: Germany"}, NOW)[0])


class Relay(unittest.TestCase):
    def test_redirects_to_live_sports(self):
        work = tempfile.mkdtemp(prefix="rex-live-sports-")
        os.makedirs(os.path.join(work, "art"))
        with open(os.path.join(work, "art", "sky-sports-racing-0a1b2c3d-poster.png"), "wb") as handle:
            handle.write(PNG)
        cfg = dict(rex_live_sports.DEFAULTS, live_sports_url="http://zen:8765", cache=work)
        routes = {"sky-sports-racing": "dm-sky-sports-racing", "slot-nfl-5": None}
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_live_sports.handler(cfg, routes))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(base + "/live-sports/sky-sports-racing.m3u8")
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(caught.exception.headers["Location"], "http://zen:8765/stream/dm-sky-sports-racing")
        with opener.open(base + "/live-sports/art/sky-sports-racing-0a1b2c3d-poster.png") as response:
            self.assertEqual((response.headers["Content-Type"], response.read()), ("image/png", PNG))
        for path in ("/live-sports/a%2Fb.m3u8", "/other/x.m3u8", "/live-sports/x.ts", "/live-sports/unknown.m3u8",
                     "/live-sports/art/missing.png", "/live-sports/art/..%2Fconfig.json", "/live-sports/art/sky-sports-racing-0a1b2c3d-poster.jpg"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(base + path)
            self.assertEqual(caught.exception.code, 404, path)
        # A slot with no game on.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(base + "/live-sports/slot-nfl-5.m3u8")
        self.assertEqual(caught.exception.code, 503)


class Lineup(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="rex-live-sports-")
        self.calls = []
        self.catalog = {"metas": CATALOG_KEYED["metas"][:]}
        self.routes = dict(iptv_routes(), **espn_routes(), **{"/catalog/tv/all.json": self.catalog})
        self.server, self.base = serve(self.routes, self.calls)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        offline(self, self.base)
        self.sync = self.lineup()

    def lineup(self, **extra):
        sync = rex_live_sports.Sync(config(self.work, self.base, lineup=US, **extra))
        sync.now = lambda: ESPN_NOW
        return sync

    def pushed(self):
        return {item["id"]: item for path, body in self.calls if path == "/rex/feed/live-sports/snapshot" for item in body["items"]}

    def test_a_lineup_is_its_channels_at_their_numbers(self):
        self.sync.once()
        pushed = self.pushed()
        def keys(entry):
            return entry["key"] if isinstance(entry["key"], list) else [entry["key"]]
        with open(US) as handle:
            lineup = {"ls-" + rex_live_sports.slug(keys(entry)[0]): entry for entry in json.load(handle)}
        listed = {meta["key"] for meta in self.catalog["metas"]}
        # The 26 of its 30 channels live-sports lists now, and nothing else of live-sports': the
        # games are in their slots.
        channels = {key: item for key, item in pushed.items() if not key.startswith("ls-slot-")}
        self.assertEqual(set(channels), {key for key, entry in lineup.items() if set(keys(entry)) & listed})
        self.assertEqual(len(channels), 26)
        self.assertEqual({key: item["channel"]["number"] for key, item in channels.items()},
                         {key: lineup[key]["number"] for key in channels})
        self.assertEqual({key: item["name"] for key, item in channels.items()}, {key: lineup[key]["name"] for key in channels})
        espn = channels["ls-espn-united-states"]
        self.assertEqual({key: espn["channel"][key] for key in ("number", "title", "call_sign")}, {"number": "2", "title": "ESPN", "call_sign": "ESPN"})
        self.assertEqual(espn["collections"], ["Sports: USA"])  # its group's, as every channel's
        self.assertEqual(self.sync.routes["espn-united-states"], "cv-3585682")  # `ESPN (USA)`
        # Another copy, by another name: the same channel, played from it.
        self.catalog["metas"][:] = [dict(meta, id=meta["id"] + "-copy", name="ESPN USA HD") if meta["key"] == "espn/united states" else meta
                                    for meta in CATALOG_KEYED["metas"]]
        self.calls.clear()
        self.sync.once()
        self.assertEqual(self.pushed()["ls-espn-united-states"]["name"], "ESPN")
        self.assertEqual(self.sync.routes["espn-united-states"], "cv-3585682-copy")

    def test_a_channel_by_the_first_of_its_keys_listed(self):
        # zen, 2026-11-01: live-sports keeps `Sky Sport Bundesliga 1 (Germany)`, `SKY BUNDESLIGA 1
        # .b` and `SKY BUNDESLIGA .c` apart, under three keys.
        entry = {"number": "2", "name": "Sky Sport Bundesliga 1",
                 "key": ["skysportbundesliga1/germany", "skybundesliga1/germany", "skybundesliga/germany"]}
        meta = next(meta for meta in CATALOG_KEYED["metas"] if meta["key"] == "skysportbundesliga1/germany")
        def listed(*keys):
            catalog = {"metas": [dict(meta, id="lstv-%d" % at, key=key) for at, key in enumerate(keys)]}
            return [(key, channel, found["title"]) for key, channel, found in rex_live_sports.channels(rex_live_sports.DEFAULTS, catalog, [entry])]
        # Whichever is listed, the same channel under its first key; of several, the lineup's first.
        self.assertEqual(listed("skybundesliga/germany"), [("skysportbundesliga1-germany", "0", "Sky Sport Bundesliga 1")])
        self.assertEqual(listed("skybundesliga/germany", "skybundesliga1/germany"), [("skysportbundesliga1-germany", "1", "Sky Sport Bundesliga 1")])
        self.assertEqual(listed("other/germany"), [])

    def test_the_rest_of_live_sports_when_asked(self):
        sync = self.lineup(lineup_only=False)
        sync.once()
        numbers = {item["name"]: item["channel"]["number"] for item in self.pushed().values()}
        self.assertEqual((numbers["ESPN"], numbers["NFL Network"], numbers["NFL 1"]), ("2", "15", "700.1"))
        # The single games too, numbered as before, by group from 100.
        self.assertRegex(numbers["Kansas City Chiefs vs Denver Broncos"], r"^1\d\d\.\d+$")
        with open(US) as handle:
            fixed = {entry["name"] for entry in json.load(handle)}
        rest = [number for name, number in numbers.items() if name not in fixed and not number.startswith("70")]
        self.assertGreater(len(rest), 140)
        self.assertTrue(all(re.fullmatch(r"1\d\d\.\d+", number) for number in rest), rest)

    def test_espn_games(self):
        games = rex_live_sports.espn_games("football/nfl", ESPN["football/nfl"]["20261101"])
        self.assertEqual(len(games), 12)
        game = next(game for game in games if game["programme"]["title"] == "Kansas City Chiefs at Denver Broncos")
        kickoff = rex_live_sports.epoch("2026-11-01T21:25Z")
        self.assertEqual(game["programme"], {
            "start": kickoff, "end": kickoff + 195 * 60, "title": "Kansas City Chiefs at Denver Broncos",
            "summary": "NFL, Week 8", "categories": ["Sports", "Football", "NFL"],
            "thumb": "https://a.espncdn.com/i/teamlogos/nfl/500/scoreboard/den.png"})  # the home side's
        self.assertEqual((game["state"], game["teams"], game["networks"]), ("in", [["Broncos"], ["Chiefs"]], {"cbs"}))
        # A club by its name or its last word; a race weekend's sessions, each a programme of its own.
        (epl,) = rex_live_sports.espn_games("soccer/eng.1", ESPN["soccer/eng.1"]["20261102"])
        self.assertEqual((epl["programme"]["title"], epl["programme"]["summary"], epl["programme"]["end"] - epl["programme"]["start"]),
                         ("Everton at Newcastle United", "Premier League", 7200))
        self.assertEqual(epl["teams"], [["Newcastle United", "United"], ["Everton", "Everton"]])
        f1 = rex_live_sports.espn_games("racing/f1", ESPN["racing/f1"]["20261101"])
        self.assertEqual([game["programme"]["title"] for game in f1], ["Mexico City Grand Prix " + kind for kind in ("FP1", "FP2", "FP3", "Qual", "Race")])
        self.assertEqual({(game["sport"], game["state"], str(game["teams"])) for game in f1}, {("F1", "post", "[['F1']]")})
        # ESPN lists the regional games of one network at once: one at a time on its channel.
        cbs = rex_live_sports.one_at_a_time([game["programme"] for game in games if "cbs" in game["networks"]])
        self.assertEqual([programme["title"] for programme in cbs], ["Indianapolis Colts at Jacksonville Jaguars", "New England Patriots at Miami Dolphins"])

    def test_espns_schedule_on_a_lineup_channel(self):
        self.sync.now = lambda: rex_live_sports.epoch("2026-11-02T20:00:00Z")
        self.sync.once()
        channels = {item["name"]: item["channel"] for item in self.pushed().values()}
        kickoff = rex_live_sports.epoch("2026-11-03T01:15Z")
        # Monday night's game on ESPN, in its name's blocks, which are cut where it is on.
        guide = [(programme["title"], programme["start"], programme["end"]) for programme in channels["ESPN"]["guide"]]
        self.assertEqual(guide[2:5], [("ESPN", kickoff - 4500, kickoff), ("Chicago Bears at Seattle Seahawks", kickoff, kickoff + 195 * 60),
                                      ("ESPN", kickoff + 195 * 60, kickoff + 17100)])
        self.assertTrue(all(before[2] <= after[1] for before, after in zip(guide, guide[1:])), guide)
        self.assertEqual(channels["ESPN"]["guide"][3]["categories"], ["Sports", "Football", "NFL"])
        # A channel with no game on it has its guide as before.
        self.assertEqual({programme["title"] for programme in channels["NFL Network"]["guide"]}, {"NFL Network"})

    def test_games_go_to_the_first_free_slot(self):
        games = sorted((game for day in ("20261101", "20261102") for game in rex_live_sports.espn_games("football/nfl", ESPN["football/nfl"][day])),
                       key=lambda game: game["programme"]["start"])
        def dealt(count):
            return {slot: [game["programme"]["title"] for game in lane] for slot, lane in rex_live_sports.deal(games, {"NFL": count}).items()}
        # Ten slots: Sunday's eight 1 pm games, the 4 pm ones where an early one has ended, and
        # where none has (the 4:05 pm game), a slot of its own; Sunday and Monday night in NFL 1.
        self.assertEqual(dealt(10), {
            "NFL 1": ["Baltimore Ravens at Buffalo Bills", "Kansas City Chiefs at Denver Broncos",
                      "Philadelphia Eagles at Washington Commanders", "Chicago Bears at Seattle Seahawks"],
            "NFL 2": ["Tennessee Titans at Cincinnati Bengals", "New England Patriots at Miami Dolphins"],
            "NFL 3": ["Arizona Cardinals at Dallas Cowboys"], "NFL 4": ["Minnesota Vikings at Detroit Lions"],
            "NFL 5": ["Las Vegas Raiders at New York Jets"], "NFL 6": ["Cleveland Browns at Pittsburgh Steelers"],
            "NFL 7": ["Atlanta Falcons at Tampa Bay Buccaneers"], "NFL 8": ["Indianapolis Colts at Jacksonville Jaguars"],
            "NFL 9": ["Los Angeles Chargers at Los Angeles Rams"]})
        # Eight: none is free at 4:05 pm, so that game goes where one ends soonest, the first.
        eight = dealt(8)
        self.assertEqual((eight["NFL 1"][:2], eight["NFL 2"], eight["NFL 3"]), (
            ["Baltimore Ravens at Buffalo Bills", "Los Angeles Chargers at Los Angeles Rams"],
            ["Tennessee Titans at Cincinnati Bengals", "Kansas City Chiefs at Denver Broncos"],
            ["Arizona Cardinals at Dallas Cowboys", "New England Patriots at Miami Dolphins"]))

    def test_slots(self):
        # The shortest name of a game's channel, `Chiefs vs Broncos`, with a smaller picture.
        meta = next(meta for meta in self.catalog["metas"] if meta["name"] == "Kansas City Chiefs vs Denver Broncos")
        self.catalog["metas"].append(dict(meta, id="lstv-test-chiefs-broncos", name="Chiefs vs Broncos", key="chiefsvsbroncos/"))
        smaller = dict(PROBE, streams=[dict(stream, height=720) if stream["codec_type"] == "video" else stream for stream in PROBE["streams"]])
        probe = self.sync.probe
        self.sync.probe = lambda channel: smaller if channel == "test-chiefs-broncos" else probe(channel)
        self.sync.once()
        slots = {key: item for key, item in self.pushed().items() if key.startswith("ls-slot-")}
        # Only the slots with a game on now: the others wait for their first.
        self.assertEqual({key: (item["name"], item["channel"]["number"], item["collections"]) for key, item in slots.items()}, {
            "ls-slot-nfl-1": ("NFL 1", "700.1", ["NFL"]), "ls-slot-nfl-2": ("NFL 2", "700.2", ["NFL"]),
            "ls-slot-nfl-9": ("NFL 9", "700.9", ["NFL"]), "ls-slot-nba-2": ("NBA 2", "701.2", ["NBA"])})
        # A slot's guide is its games, in the first slot free at their kickoff.
        self.assertEqual([programme["title"] for programme in slots["ls-slot-nfl-1"]["channel"]["guide"]],
                         ["Baltimore Ravens at Buffalo Bills", "Kansas City Chiefs at Denver Broncos",
                          "Philadelphia Eagles at Washington Commanders", "Chicago Bears at Seattle Seahawks"])
        # Each plays the channel of its game: of `Chiefs vs Broncos`, `Kansas City Chiefs vs
        # Denver Broncos` and `Denver Broncos vs Kansas City Chiefs (LOW SERVER)`, the one of the
        # taller picture, then of the shorter name, with its probe.
        self.assertEqual(self.sync.routes["slot-nfl-1"], "rx-nfl-streams-10")
        self.assertEqual(self.sync.routes["slot-nfl-9"], "sf-los-angeles-chargers-vs-los-angeles-rams")
        self.assertEqual(self.sync.routes["slot-nfl-2"], "rx-nfl-streams-11")
        self.assertIn(self.sync.routes["slot-nba-2"], {"sf-brooklyn-nets-vs-indiana-pacers", "fw-SW5kaWFuYSBQYWNlcnMgdnMgQnJvb2tseW4gTmV0cy5odG1s",
                                                        "el-01a19947-05c5-74ba-a8c9-aeb8c30209a3"})
        with open(os.path.join(self.sync.config["cache"], "rx-nfl-streams-10.json")) as handle:
            self.assertEqual(slots["ls-slot-nfl-1"]["media_info"], json.load(handle))
        self.assertIsNone(self.sync.routes["slot-nfl-3"])  # its game is over
        # The game ends: the slot stays, with its game's probe, and its relay answers 503.
        day = copy.deepcopy(ESPN["football/nfl"]["20261101"])
        game = next(event for event in day["events"] if event["shortName"] == "KC @ DEN")
        for status in (game["status"], game["competitions"][0]["status"]):
            status["type"]["state"] = "post"
        self.routes["/espn/football/nfl/scoreboard?dates=20261101"] = day
        self.calls.clear()
        self.sync.once()
        self.assertIsNone(self.sync.routes["slot-nfl-1"])
        self.assertEqual(self.pushed()["ls-slot-nfl-1"]["media_info"], slots["ls-slot-nfl-1"]["media_info"])
        # A restart keeps it; ten minutes before Sunday night's kickoff it plays that game.
        sync = self.lineup()
        self.calls.clear()
        sync.once()
        self.assertIsNone(sync.routes["slot-nfl-1"])
        self.assertIn("ls-slot-nfl-1", self.pushed())
        sync.now = lambda: rex_live_sports.epoch("2026-11-02T01:10:00Z")
        self.calls.clear()
        sync.once()
        self.assertIn(sync.routes["slot-nfl-1"], {"rx-nfl-streams-12", "el-01a19947-f1bd-7188-99ed-7a873f72069e"})


@unittest.skipUnless(os.path.exists(os.environ.get("REX_BIN", "/nonexistent")), "REX_BIN names no rex binary")
class AgainstRex(unittest.TestCase):
    TOKEN = "REX-LIVE-SPORTS-TEST"

    def get(self, path):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                         headers={"X-Plex-Token": self.TOKEN, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def start(self, catalog, **extra):
        """A rex, and a sync into it with its relay, of `catalog`."""
        work = tempfile.mkdtemp(prefix="rex-live-sports-")
        self.port = free_port()
        env = dict(os.environ, REX_TRANSCODER="off", REX_DIRECT_URL="off", REX_PLEXTV_URL="http://127.0.0.1:9")
        rex = subprocess.Popen([os.environ["REX_BIN"], "--port", str(self.port), "--catalog", os.path.join(work, "catalog.json"),
                                "--token", self.TOKEN, "--no-state", "--assert-claimed"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.addCleanup(rex.wait)
        self.addCleanup(rex.terminate)
        for _ in range(100):
            try:
                self.get("/identity")
                break
            except OSError:
                time.sleep(0.1)
        live, base = serve(dict(iptv_routes(), **espn_routes(), **{"/catalog/tv/all.json": catalog}), [])
        self.addCleanup(live.server_close)
        self.addCleanup(live.shutdown)
        offline(self, base)
        listen = "127.0.0.1:%d" % free_port()
        sync = rex_live_sports.Sync(config(work, base, rex_url="http://127.0.0.1:%d" % self.port, listen=listen, **extra))
        relay = ThreadingHTTPServer(("127.0.0.1", int(listen.rsplit(":", 1)[1])), rex_live_sports.handler(sync.config, sync.routes))
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        self.addCleanup(relay.server_close)
        self.addCleanup(relay.shutdown)
        return sync

    def test_a_lineup_is_rexs_live_tv(self):
        sync = self.start({"metas": CATALOG_KEYED["metas"][:]}, lineup=US)
        sync.now = lambda: ESPN_NOW
        sync.once()
        self.assertEqual(sync.rejected, set())
        lineup = {channel["vcn"]: channel["title"] for channel in self.get("/tv.plex.providers.epg.xmltv:2/lineups/dvr/channels")["MediaContainer"]["Channel"]}
        self.assertEqual(len(lineup), 30)  # 26 networks, 4 slots
        self.assertEqual((lineup["2"], lineup["15"], lineup["700.1"], lineup["701.2"]), ("ESPN", "NFL Network", "NFL 1", "NBA 2"))

    def test_channels_become_a_library_with_collections(self):
        catalog = {"metas": [meta for meta in CATALOG["metas"] if meta["genre"].startswith("Live:")]}
        sync = self.start(catalog)
        sync.once()
        sections = self.get("/library/sections")["MediaContainer"]["Directory"]
        section = next(section for section in sections if section["title"] == "Live Sports")
        rows = self.get("/library/sections/%s/all" % section["key"])["MediaContainer"].get("Metadata") or []
        # One row a channel name (bd00b1d): the Live: groups list Willow twice.
        names = {meta["name"].strip().lower() for _, _, meta in rex_live_sports.channels(sync.config, catalog)}
        self.assertEqual(len(rows), len(names))
        racing = next(row for row in rows if row["title"] == "Sky Sports Racing")
        # rex fetched the poster from the relay and serves it as the row's thumb.
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, racing["thumb"]), headers={"X-Plex-Token": self.TOKEN})
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.read(), PNG + b"https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/512/512/skychb1354.png")
        self.assertTrue(racing.get("art"))
        collections = self.get("/library/sections/%s/collections" % section["key"])["MediaContainer"].get("Metadata") or []
        self.assertIn("Live: Football", {row["title"] for row in collections})
        # And Live TV: one DVR whose lineup is every channel by its number, each with its guide
        # (the fixture's guides are of another day, so these are the channels' names).
        self.assertEqual(len(self.get("/livetv/dvrs")["MediaContainer"]["Dvr"]), 1)
        lineup = self.get("/tv.plex.providers.epg.xmltv:2/lineups/dvr/channels")["MediaContainer"]["Channel"]
        self.assertEqual(len(lineup), len(rows))
        channel = next(channel for channel in lineup if channel["title"] == "Sky Sports Racing")
        self.assertEqual(channel["vcn"], "100.1")
        grid = self.get("/tv.plex.providers.epg.xmltv:2/grid?channelGridKey=%s&date=%s"
                        % (channel["gridKey"], time.strftime("%Y-%m-%d", time.gmtime())))["MediaContainer"]
        self.assertEqual({row["title"] for row in grid["Metadata"]}, {"Sky Sports Racing"})
        # Restarted beside a live-sports still checking its channels: rex keeps what it holds.
        catalog["metas"][:] = catalog["metas"][:2]
        rex_live_sports.Sync(sync.config).once()
        rows = self.get("/library/sections/%s/all" % section["key"])["MediaContainer"].get("Metadata") or []
        self.assertEqual(len([row for row in rows if not row.get("deletedAt")]), len(names))


if __name__ == "__main__":
    unittest.main()
