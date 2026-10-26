"""rex-live-sports against a real live-sports catalog (zen, 2026-10-26) and a real channel's probe.

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
                    self.answer(409, {"error": "would remove more than half"})
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
        json.dump(dict({"live_sports_url": base, "rex_url": base, "ffprobe": os.path.join(HERE, "fake_ffprobe.py")}, **extra), handle)
    return rex_live_sports.load_config(path)


class Channels(unittest.TestCase):
    def test_sports_groups_only(self):
        listed = rex_live_sports.channels(rex_live_sports.DEFAULTS, CATALOG)
        self.assertEqual(len(CATALOG["metas"]), 1117)
        self.assertEqual(len(listed), 624)
        groups = {meta["genre"] for _, meta in listed}
        self.assertIn("Live: Football", groups)
        self.assertIn("Sports: UK", groups)
        self.assertNotIn("TV: Other", groups)
        self.assertFalse(any(group.startswith("Pluto") for group in groups))
        self.assertIn(("dm-sky-sports-racing", "Sky Sports Racing"), [(channel, meta["name"]) for channel, meta in listed])

    def test_ids_a_path_cannot_hold_are_left_out(self):
        catalog = {"metas": [{"id": "lstv-a/b", "genre": "Sports"}, {"id": "other-1", "genre": "Sports"}, "junk",
                             {"id": "lstv-ok", "genre": "Sports"}]}
        self.assertEqual([channel for channel, _ in rex_live_sports.channels(rex_live_sports.DEFAULTS, catalog)], ["ok"])

    def test_item(self):
        meta = next(meta for meta in CATALOG["metas"] if meta["id"] == "lstv-dm-sky-sports-racing")
        pushed = rex_live_sports.item(dict(rex_live_sports.DEFAULTS), "dm-sky-sports-racing", meta, PROBE)
        self.assertEqual(pushed["path"], "live-sports/dm-sky-sports-racing.m3u8")
        self.assertEqual(pushed["file"], "/mnt/rex-live-sports/live-sports/dm-sky-sports-racing.m3u8")
        self.assertEqual(pushed["release"], "live-sports:dm-sky-sports-racing")
        self.assertEqual(pushed["collections"], ["Live: 24/7 Streams"])
        self.assertEqual(pushed["display"]["title"], "Sky Sports Racing")
        self.assertNotIn("kind", pushed)  # nothing identifies a channel
        self.assertNotIn("size", pushed)
        self.assertTrue(rex_live_sports.playable(pushed["media_info"]))

    def test_playable(self):
        self.assertFalse(rex_live_sports.playable({"format": {"format_name": "hls"}, "streams": [{"codec_type": "audio"}]}))
        self.assertFalse(rex_live_sports.playable({"format": {"format_name": "mpegts"}, "streams": PROBE["streams"]}))


class SyncOnce(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="rex-live-sports-")
        self.calls = []
        self.catalog = {"metas": CATALOG["metas"][:]}
        self.server, base = serve({"/catalog/tv/all.json": self.catalog}, self.calls)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
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
        ids = [pushed["id"] for part in parts for pushed in part["items"]]
        self.assertEqual(len(ids), 623)
        self.assertNotIn("ls-dm-nfl-network", ids)
        self.assertTrue(parts[-1]["force"])  # the first push: nothing to guard
        self.assertEqual(len(os.listdir(self.config["cache"])), 623)

    def test_probes_are_kept(self):
        self.sync.once()
        os.environ["FAKE_FFPROBE_FAIL"] = "dm-nfl-network"  # would fail if it were asked again
        self.addCleanup(os.environ.pop, "FAKE_FFPROBE_FAIL")
        self.calls.clear()
        self.sync.once()
        self.assertEqual(sum(len(part["items"]) for part in self.snapshots()), 624)

    def test_a_list_under_half_the_last_is_not_forced(self):
        self.sync.once()
        self.calls.clear()
        self.catalog["metas"][:] = [meta for meta in CATALOG["metas"] if meta["genre"].startswith("Live:")]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.sync.once()
        self.assertEqual(caught.exception.code, 409)
        parts = self.snapshots()
        self.assertLess(sum(len(part["items"]) for part in parts) * 2, 624)
        self.assertNotIn("force", parts[-1])
        self.sync.short_since -= 1801  # still short half an hour on: the events ended
        self.calls.clear()
        self.sync.once()
        self.assertTrue(self.snapshots()[-1]["force"])

    def test_churn_of_the_same_size_is_forced(self):
        self.sync.once()
        self.calls.clear()
        self.catalog["metas"][:] = [dict(meta, id=meta["id"] + "-next") for meta in CATALOG["metas"]]
        self.sync.once()
        self.assertTrue(self.snapshots()[-1]["force"])

    def test_nothing_listed_pushes_nothing(self):
        self.catalog["metas"][:] = []
        self.sync.once()
        self.assertEqual(self.calls, [])


class Relay(unittest.TestCase):
    def test_redirects_to_live_sports(self):
        cfg = dict(rex_live_sports.DEFAULTS, live_sports_url="http://zen:8765")
        server = ThreadingHTTPServer(("127.0.0.1", 0), rex_live_sports.handler(cfg))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(base + "/live-sports/dm-sky-sports-racing.m3u8")
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(caught.exception.headers["Location"], "http://zen:8765/stream/dm-sky-sports-racing")
        for path in ("/live-sports/a%2Fb.m3u8", "/other/x.m3u8", "/live-sports/x.ts"):
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
        live, base = serve({"/catalog/tv/all.json": {"metas": [meta for meta in CATALOG["metas"] if meta["genre"].startswith("Live:")]}}, calls)
        self.addCleanup(live.server_close)
        self.addCleanup(live.shutdown)
        sync = rex_live_sports.Sync(config(work, base, rex_url="http://127.0.0.1:%d" % self.port))
        sync.once()
        sections = self.get("/library/sections")["MediaContainer"]["Directory"]
        section = next(section for section in sections if section["title"] == "Live Sports")
        rows = self.get("/library/sections/%s/all" % section["key"])["MediaContainer"].get("Metadata") or []
        self.assertEqual(len(rows), len(rex_live_sports.channels(sync.config, {"metas": [m for m in CATALOG["metas"] if m["genre"].startswith("Live:")]})))
        self.assertIn("Sky Sports Racing", {row["title"] for row in rows})
        collections = self.get("/library/sections/%s/collections" % section["key"])["MediaContainer"].get("Metadata") or []
        self.assertIn("Live: Football", {row["title"] for row in collections})


if __name__ == "__main__":
    unittest.main()
