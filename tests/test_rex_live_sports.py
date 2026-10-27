"""rex-live-sports against a real live-sports catalog (zen, 2026-10-26), a real channel's probe
and iptv-org's real channel and logo lists, cut to the names in that catalog.

`AgainstRex` also syncs into a real rex binary: REX_BIN.

    REX_BIN=/path/to/rex python3 -m unittest discover -s tests
"""
import json
import os
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
sys.path.insert(0, os.path.dirname(HERE))
import rex_live_sports  # noqa: E402

with open(os.path.join(HERE, "fixtures", "catalog.json")) as _handle:
    CATALOG = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "probe-dm-sky-sports-racing.json")) as _handle:
    PROBE = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "probe-slate-cv-2897253.json")) as _handle:
    SLATE = json.load(_handle)
with open(os.path.join(HERE, "fixtures", "iptv-org.json")) as _handle:
    IPTV_ORG = json.load(_handle)
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
            self.answer(200, routes[self.path]) if self.path in routes else self.send_error(404)

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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def config(work, base, **extra):
    path = os.path.join(work, "config.json")
    with open(path, "w") as handle:
        json.dump(dict({"live_sports_url": base, "rex_url": base, "ffprobe": os.path.join(HERE, "fake_ffprobe.py"),
                        "ffmpeg": os.path.join(HERE, "fake_ffmpeg.py")}, **extra), handle)
    return rex_live_sports.load_config(path)


def offline(test, base, refused=()):
    """iptv-org's lists from the fixture, served at `base`, and logos fetched from nowhere: a PNG
    naming its url, or a 404 for a url in `refused`."""
    rex_live_sports.IPTV_ORG, iptv_org = base + "/iptv-org", rex_live_sports.IPTV_ORG
    test.addCleanup(setattr, rex_live_sports, "IPTV_ORG", iptv_org)

    def fetch(url):
        if url in refused:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return PNG + url.encode()
    patcher = unittest.mock.patch.object(rex_live_sports, "fetch", fetch)
    patcher.start()
    test.addCleanup(patcher.stop)


def iptv_routes():
    return {"/iptv-org/%s.json" % name: IPTV_ORG[name] for name in ("channels", "logos", "countries")}


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
        self.assertEqual(registered["url"], "http://127.0.0.1:9983")
        parts = self.snapshots()
        self.assertEqual([part["part"] for part in parts], [0, 1, 2, 3])
        self.assertEqual([part["complete"] for part in parts], [False, False, False, True])
        pushed = {item["id"]: item for part in parts for item in part["items"]}
        self.assertEqual(len(pushed), 623)
        refused = next(key for key, channel in self.sync.routes.items() if channel == "dm-nfl-network")
        self.assertNotIn("ls-" + refused, pushed)
        self.assertNotIn("force", parts[-1])  # nothing in rex to remove
        self.assertEqual(len([name for name in os.listdir(self.config["cache"]) if name.endswith(".json")]), 624)  # + iptv-org's
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
        self.assertEqual(sum(len(part["items"]) for part in self.snapshots()), 624)

    def test_a_placeholder_is_probed_again(self):
        os.environ["FAKE_FFPROBE_SLATE"] = "dm-nfl-network"
        self.addCleanup(os.environ.pop, "FAKE_FFPROBE_SLATE", None)
        self.sync.once()
        path = os.path.join(self.config["cache"], "dm-nfl-network.json")
        self.assertFalse(os.path.exists(path))  # not pushed, not kept
        self.assertEqual(sum(len(part["items"]) for part in self.snapshots()), 623)
        os.environ.pop("FAKE_FFPROBE_SLATE")
        self.calls.clear()
        self.sync.once()
        self.assertEqual(sum(len(part["items"]) for part in self.snapshots()), 624)

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


class Relay(unittest.TestCase):
    def test_redirects_to_live_sports(self):
        work = tempfile.mkdtemp(prefix="rex-live-sports-")
        os.makedirs(os.path.join(work, "art"))
        with open(os.path.join(work, "art", "sky-sports-racing-0a1b2c3d-poster.png"), "wb") as handle:
            handle.write(PNG)
        cfg = dict(rex_live_sports.DEFAULTS, live_sports_url="http://zen:8765", cache=work)
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_live_sports.handler(cfg, {"sky-sports-racing": "dm-sky-sports-racing"}))
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


@unittest.skipUnless(os.path.exists(os.environ.get("REX_BIN", "/nonexistent")), "REX_BIN names no rex binary")
class AgainstRex(unittest.TestCase):
    TOKEN = "REX-LIVE-SPORTS-TEST"

    def get(self, path):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                         headers={"X-Plex-Token": self.TOKEN, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def test_channels_become_a_library_with_collections(self):
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
        calls = []
        catalog = {"metas": [meta for meta in CATALOG["metas"] if meta["genre"].startswith("Live:")]}
        live, base = serve(dict(iptv_routes(), **{"/catalog/tv/all.json": catalog}), calls)
        self.addCleanup(live.server_close)
        self.addCleanup(live.shutdown)
        offline(self, base)
        listen = "127.0.0.1:%d" % free_port()
        sync = rex_live_sports.Sync(config(work, base, rex_url="http://127.0.0.1:%d" % self.port, listen=listen))
        relay = ThreadingHTTPServer(("127.0.0.1", int(listen.rsplit(":", 1)[1])), rex_live_sports.handler(sync.config, sync.routes))
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        self.addCleanup(relay.server_close)
        self.addCleanup(relay.shutdown)
        sync.once()
        sections = self.get("/library/sections")["MediaContainer"]["Directory"]
        section = next(section for section in sections if section["title"] == "Live Sports")
        rows = self.get("/library/sections/%s/all" % section["key"])["MediaContainer"].get("Metadata") or []
        self.assertEqual(len(rows), len(rex_live_sports.channels(sync.config, {"metas": [m for m in CATALOG["metas"] if m["genre"].startswith("Live:")]})))
        racing = next(row for row in rows if row["title"] == "Sky Sports Racing")
        # rex fetched the poster from the relay and serves it as the row's thumb.
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, racing["thumb"]), headers={"X-Plex-Token": self.TOKEN})
        with urllib.request.urlopen(request, timeout=30) as response:
            self.assertEqual(response.read(), PNG + b"https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/512/512/skychb1354.png")
        self.assertTrue(racing.get("art"))
        collections = self.get("/library/sections/%s/collections" % section["key"])["MediaContainer"].get("Metadata") or []
        self.assertIn("Live: Football", {row["title"] for row in collections})
        # Restarted beside a live-sports still checking its channels: rex keeps what it holds.
        catalog["metas"][:] = catalog["metas"][:2]
        rex_live_sports.Sync(sync.config).once()
        rows = self.get("/library/sections/%s/all" % section["key"])["MediaContainer"].get("Metadata") or []
        self.assertEqual(len([row for row in rows if not row.get("deletedAt")]), len(rex_live_sports.channels(sync.config, {"metas": [m for m in CATALOG["metas"] if m["genre"].startswith("Live:")]})))


if __name__ == "__main__":
    unittest.main()
