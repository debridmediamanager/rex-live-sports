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
logo for gets that logo; any other its name.

Each channel is also a channel of rex's Live TV (`live_tv`), with a number that stays its own and
a guide: Pluto TV's own schedule for a Pluto TV channel, epgshare01's XMLTV for a channel it finds
there by name and country, and the channel's name in blocks of six hours for the rest.

With a lineup (`lineups/us.json`), Live TV is a short list of networks instead: the channels it
names, at the numbers it gives them, with ESPN's schedule of the games on them, and slots that
carry each game of a sport from before kickoff until it ends.

Standard library only; ffprobe and ffmpeg are separate executables.
"""
import argparse
import calendar
import datetime
import email.utils
import gzip
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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
    "live_tv": True,
    "lineup": "lineup.json",
    "lineup_only": True,
    "slots": {"NFL": 10, "NBA": 6, "NHL": 6, "MLB": 4, "Soccer": 8, "F1": 1},
}

CHANNEL = re.compile(r"[A-Za-z0-9_.-]+")

AGENT = "rex-live-sports (https://github.com/debridmediamanager/rex-live-sports)"

IPTV_ORG = "https://iptv-org.github.io/api"

# Guides: free, no key. epgshare01 rebuilds its per-country XMLTV files a few times a day (about
# 100 MB of them for live-sports' sports channels, 2026-11-01); Pluto TV's own API answers the
# schedule of the country it is asked from, which is live-sports' when both run at home.
EPGSHARE01 = "https://epgshare01.online/epgshare01/"
PLUTO_TV = "https://api.pluto.tv/v2/channels"
GUIDE_HOURS = 12  # how often guides are fetched again
PLACEHOLDER_HOURS = 6

# ESPN's scoreboards, free and without a key: one day a request (a range answers 400, 2026-11-01).
# Each league: its slots' sport, its category in the guide and how long a game is given there.
ESPN = "https://site.api.espn.com/apis/site/v2/sports/"
LEAGUES = dict({"football/nfl": ("NFL", "Football", 195), "football/college-football": ("College Football", "Football", 195),
                "basketball/nba": ("NBA", "Basketball", 150), "hockey/nhl": ("NHL", "Hockey", 165),
                "baseball/mlb": ("MLB", "Baseball", 180), "racing/f1": ("F1", "Motorsport", 120)},
               **{"soccer/" + league: ("Soccer", "Soccer", 120) for league in
                  ("eng.1", "ger.1", "esp.1", "ita.1", "fra.1", "uefa.champions", "uefa.europa", "usa.1")})
SLOTS_FROM = 700  # the first sport's slots are 700.1, 700.2, ..., the next one's 701.1

# live-sports' own poster colour, behind a channel's name and behind a logo drawn in light colours.
DARK, LIGHT = "0x1a1a2e", "0xf2f2f2"

# (width, height, logo box): a film's 2:3 poster and a 16:9 background.
SHAPES = {"poster": (600, 900, 480), "art": (1280, 720, 400)}


def get_json(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": "rex-live-sports", "Accept": "application/json",
                                                  "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        return json.loads((gzip.decompress(data) if response.headers.get("Content-Encoding") == "gzip" else data) or b"{}")


def download(url, path):
    """`url` into `path`, unless it has not changed since `path` was written."""
    headers = {"User-Agent": AGENT}
    if os.path.exists(path):
        headers["If-Modified-Since"] = email.utils.formatdate(os.path.getmtime(path), usegmt=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as response:
            with open(path + ".tmp", "wb") as handle:
                shutil.copyfileobj(response, handle)
        os.replace(path + ".tmp", path)
    except urllib.error.HTTPError as error:
        if error.code != 304:
            raise


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
    lineup = os.path.join(os.path.dirname(os.path.abspath(path)), config["lineup"] or "")
    config["lineup"] = None
    if os.path.isfile(lineup):
        with open(lineup) as handle:
            config["lineup"] = json.load(handle)
    return config


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def listed_metas(catalog):
    return [meta for meta in catalog.get("metas") or [] if isinstance(meta, dict) and str(meta.get("id", "")).startswith("lstv-")
            and CHANNEL.fullmatch(meta["id"][len("lstv-"):])]


def channels(config, catalog, lineup=None):
    """The catalog's channels in the chosen groups, as (key, channel id, meta). live-sports lists
    only channels whose stream passed its last check, one copy of each channel it has several
    of, and which copy can change from one check to the next (measured while it starts). So rex
    knows a channel by its name (`key`), and the relay plays whichever copy is listed now.

    A lineup's channel, found by live-sports' own key for a channel's copies (`espn/united
    states`), or the first of its keys the catalog lists (live-sports keeps `SKY BUNDESLIGA 1`
    and `Sky Sport Bundesliga 1` apart), is taken whatever its group, under its first key, with
    the lineup's name and number; with `lineup_only`, nothing else is."""
    out, keys = [], set()
    metas = listed_metas(catalog)
    listed, named = {str(meta.get("key")) for meta in metas}, {}
    for entry in lineup or []:
        ways = [str(key) for key in (entry["key"] if isinstance(entry["key"], list) else [entry["key"]])]
        found = next((key for key in ways if key in listed), None)
        if found:
            named[found] = dict(entry, key=ways[0])
    others = lineup is None or not config["lineup_only"]
    # The lineup's channels first, so that no other channel takes their key.
    for meta in sorted(metas, key=lambda meta: str(meta.get("key")) not in named):
        channel, group = meta["id"][len("lstv-"):], str(meta.get("genre") or "")
        entry = named.get(str(meta.get("key")))
        if entry or others and group.startswith(tuple(config["groups"])):
            key = slug(entry["key"] if entry else meta.get("name") or "") or channel
            while key in keys:
                key += "-2"
            keys.add(key)
            if entry:
                meta = dict(meta, title=entry["name"], number=str(entry["number"]), espn=entry.get("espn") or [])
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


def country_names(index):
    """iptv-org's countries by name, and by code where a name writes it in capitals (`Sky Sports
    F1 UK`, `Fox Sports 1 USA`): not `TV` (Tuvalu's), which names a channel, not a country."""
    names = {name: code for name, code in index["countries"].items() if name}
    names.update({code: code for code in names.values() if code != "TV"}, USA="US", GB="UK")
    return names


def country_tail(names, name):
    """The name without the country it ends in, and that country's code (`ESPN Argentina`,
    `Sky Sport 1 NZ`)."""
    bare = re.sub(r"\([^)]*\)\s*$", "", name).strip()
    for country, code in sorted(names.items(), key=lambda pair: -len(pair[0])):
        written = bare if country.isupper() else bare.lower()
        if written.endswith(" " + country):
            return bare[:-len(country)].strip(), code
    return bare, None


def countries(index, meta):
    """The countries a channel is of: those its name or its group names (`ESPN (Argentina)`,
    `ESPN (United States) HD (720p)`, `Sports: Spain`, `Sky Sports F1 UK`), else those of
    iptv-org's channels of its name."""
    names = country_names(index)
    name, group = str(meta.get("name") or ""), str(meta.get("genre") or "")
    def code(text):
        return names.get(text.strip().lower(), names.get(text.strip().upper()))
    named = {code(text) for text in re.findall(r"\(([^)]+)\)", name) + [group.partition(":")[2]]}
    named = (named | {country_tail(names, name)[1]}) - {None}
    return named or {country for country, _ in index["names"].get(plain(name)) or []}


def variants(index, name):
    """The plain names a channel's guide can go by: its own, without live-sports' mirror mark
    (`SPORT 1 .c`) and without the country it ends in (`Sky Sport 1 NZ`)."""
    whole = re.sub(r"\s\.\w+$", "", name)
    return {plain(name), plain(whole), plain(country_tail(country_names(index), whole)[0])} - {""}


def epoch(text):
    """An XMLTV time (`20261101101903 +0000`) or an ISO 8601 one (Pluto TV's) as epoch seconds."""
    text = text.strip()
    if text[:14].isdigit():
        zone = re.search(r"([+-])(\d\d)(\d\d)$", text)
        offset = (int(zone[2]) * 3600 + int(zone[3]) * 60) * (1 if zone[1] == "+" else -1) if zone else 0
        return calendar.timegm(time.strptime(text[:14], "%Y%m%d%H%M%S")) - offset
    return int(datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def espn_games(league, answer):
    """A league's day of ESPN's scoreboard as games: each competition (an F1 weekend's sessions
    are competitions of one event) as a guide programme, with its state, what a channel's name
    calls each side (a nickname, `Chiefs`; a club by its name or its last word, `Newcastle United`
    or `United`; an F1 session by `F1`) and the networks ESPN lists it on, in lower case."""
    sport, category, minutes = LEAGUES[league]
    name = str((answer.get("leagues") or [{}])[0].get("abbreviation") or sport)
    def called(team):
        whole = str(team.get("displayName") or "")
        return [whole, whole.split()[-1]] if sport == "Soccer" and whole else [str(team.get("name") or whole)]
    out = []
    for event in answer.get("events") or []:
        competitions = event.get("competitions") or []
        for competition in competitions:
            try:
                start = epoch(competition.get("date") or event["date"])
            except (KeyError, TypeError, ValueError):
                continue
            teams = sorted((side for side in competition.get("competitors") or [] if isinstance(side.get("team"), dict)),
                           key=lambda side: side.get("homeAway") != "home")
            kind = (competition.get("type") or {}).get("abbreviation") if len(competitions) > 1 else None
            week = (event.get("week") or {}).get("number")
            programme = {"start": start, "end": start + minutes * 60,
                         "title": " ".join(str(part) for part in (event.get("name") or name, kind) if part),
                         "summary": name + (", Week %s" % week if week else ""), "categories": ["Sports", category, name]}
            logo = str(teams[0]["team"].get("logo") or "") if teams else ""
            if logo.startswith("https://"):
                programme["thumb"] = logo
            out.append({"id": "%s/%s" % (event.get("id"), competition.get("id")), "sport": sport,
                        "programme": programme,
                        "state": ((competition.get("status") or event.get("status") or {}).get("type") or {}).get("state"),
                        "teams": [[part for part in called(side["team"]) if part] for side in teams] or [[sport]],
                        "networks": {str(network).lower() for broadcast in competition.get("broadcasts") or []
                                     for network in broadcast.get("names") or []}})
    return out


def on(game, now):
    """A game is on from 15 minutes before kickoff until ESPN has it over."""
    return game["state"] != "post" and now >= game["programme"]["start"] - 900


def shows(meta, game):
    """A channel of a game: its name calls both sides (`Chiefs vs Broncos`, `Denver Broncos vs
    Kansas City Chiefs (LOW SERVER)`), each from the start of a word (the Nets, not the Hornets)."""
    name = str(meta.get("name") or "")
    return all(any(re.search(r"(?i)\b" + re.escape(part), name) for part in side) for side in game["teams"])


def height(probe):
    return max([stream.get("height") or 0 for stream in probe.get("streams") or [] if stream.get("codec_type") == "video"] or [0])


def deal(games, slots):
    """Each sport's games, by kickoff, to the lowest-numbered of its `slots` free by then (its last
    game over by the sport's length), else to the one whose game ends soonest: {"NFL 1": [game]}."""
    lanes, ends = {}, {}
    for game in games:
        count = int(slots.get(game["sport"]) or 0)
        if count:
            free = ends.setdefault(game["sport"], [0] * count)
            at = next((at for at, end in enumerate(free) if end <= game["programme"]["start"]), free.index(min(free)))
            free[at] = game["programme"]["end"]
            lanes.setdefault("%s %d" % (game["sport"], at + 1), []).append(game)
    return lanes


def one_at_a_time(programmes):
    """Programmes by their start, each cut at the next one's: a slot takes a game before its last
    one ends when all are taken, and ESPN lists the regional games of one network (CBS's at 1 pm)
    at once."""
    programmes = sorted(programmes, key=lambda programme: programme["start"])
    cut = [dict(programme, end=min(programme["end"], after["start"])) for programme, after in zip(programmes, programmes[1:])]
    return [programme for programme in cut + programmes[-1:] if programme["end"] > programme["start"]]


def around(guide, games):
    """A channel's guide with its games in it, what else it had at their times cut out."""
    if not games:
        return guide
    out = list(games)
    for programme in guide:
        pieces = [(programme["start"], programme["end"])]
        for game in games:
            pieces = [(start, end) for low, high in pieces
                      for start, end in ((low, min(high, game["start"])), (max(low, game["end"]), high)) if end > start]
        out += [dict(programme, start=start, end=end) for start, end in pieces]
    return sorted(out, key=lambda programme: programme["start"])


def xmltv_programme(element):
    """An XMLTV `<programme>` as a guide programme: an episode when it has its season and episode
    (`xmltv_ns` counts both from 0, `onscreen` writes S02E04), else a programme of its own."""
    title = (element.findtext("title") or "").strip()
    if not title:
        return None
    out = {"start": epoch(element.get("start")), "end": epoch(element.get("stop")), "title": title}
    numbers = None
    for number in element.findall("episode-num"):
        text = (number.text or "").strip()
        parts = [part.split("/")[0].strip() for part in text.split(".")]
        if number.get("system") == "xmltv_ns" and len(parts) > 1 and parts[0].isdigit() and parts[1].isdigit():
            numbers = (int(parts[0]) + 1, int(parts[1]) + 1)
        elif number.get("system") == "onscreen" and numbers is None and re.fullmatch(r"S\d+\s*E\d+", text, re.I):
            numbers = tuple(int(part) for part in re.findall(r"\d+", text))
    if numbers:
        out.update(type="episode", show=title, season=numbers[0], episode=numbers[1],
                   title=(element.findtext("sub-title") or "").strip() or "Episode %d" % numbers[1])
    summary = (element.findtext("desc") or "").strip()
    categories = [text.strip() for text in (category.text for category in element.findall("category")) if text and text.strip()]
    thumb = element.find("icon")
    for field, value in (("summary", summary), ("categories", categories),
                         ("thumb", thumb is not None and thumb.get("src", "").startswith(("http://", "https://")) and thumb.get("src")),
                         ("previously_shown", element.find("previously-shown") is not None)):
        if value:
            out[field] = value
    return out


def xmltv(stream, code, wanted, since, until):
    """One XMLTV guide's channels by a name in `wanted` (plain names): ({plain name: [(country,
    channel id)]}, {channel id: [programme]}), with the programmes between `since` and `until`."""
    names, programmes = {}, {}
    for _, element in ET.iterparse(stream):
        if element.tag == "channel":
            for name in {plain(name.text or "") for name in element.findall("display-name")} & wanted:
                names.setdefault(name, []).append((code, element.get("id")))
                programmes.setdefault(element.get("id"), [])
            element.clear()
        elif element.tag == "programme":
            if element.get("channel") in programmes:
                try:
                    programme = xmltv_programme(element)
                except (TypeError, ValueError):
                    programme = None
                if programme and programme["end"] > since and programme["start"] < until and programme["end"] > programme["start"]:
                    programmes[element.get("channel")].append(programme)
            element.clear()
    return names, programmes


def pluto_programme(entry):
    """A Pluto TV timeline entry as a guide programme: an episode of a series under its own title,
    anything else (a film, a race) under the slot's. Pluto TV airs only repeats, but live."""
    episode = entry.get("episode") or {}
    series = episode.get("series") or {}
    def known(text):
        return text.strip() if isinstance(text, str) and text.strip() and text != "no info available" else None
    out = {"start": epoch(entry["start"]), "end": epoch(entry["stop"]),
           "title": known(entry.get("title")) or known(episode.get("name")) or known(series.get("name"))}
    if not out["title"] or out["end"] <= out["start"]:
        return None
    season, number = episode.get("season"), episode.get("number")
    if series.get("type") == "tv" and isinstance(season, int) and isinstance(number, int) and season >= 0 and number > 0:
        out.update(type="episode", show=known(series.get("name")) or out["title"], season=season, episode=number,
                   title=known(episode.get("name")) or out["title"])
    for field, value in (("summary", known(episode.get("description"))),
                         ("categories", list(dict.fromkeys(genre for genre in (known(episode.get("genre")), known(episode.get("subGenre"))) if genre))),
                         ("thumb", str((episode.get("thumbnail") or {}).get("path") or "").startswith("https://")
                          and episode["thumbnail"]["path"]),
                         ("previously_shown", not episode.get("liveBroadcast"))):
        if value:
            out[field] = value
    return out


def categories_of(group):
    """A group's categories in the guide: `Live: Football` is ["Sports", "Football"], any
    `Sports...` group ["Sports"], `Pluto: Crime` ["Crime"]; general TV none."""
    head, _, tail = group.partition(":")
    return (["Sports"] if head in ("Live", "Sports") else []) + ([tail.strip()] if head in ("Live", "Pluto") and tail.strip() else [])


def placeholder(meta, now):
    """A guide for a channel no guide lists, and for a single match: its name, in blocks of
    PLACEHOLDER_HOURS (UTC) from the block before now to a day after the next, so that every
    channel has a row in Plex's guide. They change only when a block passes."""
    block = PLACEHOLDER_HOURS * 3600
    first = int(now) - int(now) % block - block
    categories = categories_of(str(meta.get("genre") or ""))
    return [dict({"start": start, "end": start + block, "title": str(meta.get("title") or meta.get("name") or "")},
                 **({"categories": categories} if categories else {}))
            for start in range(first, first + 6 * block, block)]


def live(probe):
    """A probe with no duration: what rex takes a Live TV channel for."""
    try:
        return not float((probe.get("format") or {}).get("duration") or 0) > 0
    except ValueError:
        return True


def item(config, key, meta, probe, art=None):
    """One feed item: a live channel nothing identifies, named by `display`, in its group's
    collection. Its own release, or rex would group every channel of the folder as one."""
    path = "%s/%s.m3u8" % (config["directory"], key)
    group = str(meta.get("genre") or "")
    art = art or {}
    name = meta.get("title") or meta.get("name") or key
    display = {"title": name, "summary": group, "thumb": art.get("poster"), "art": art.get("art")}
    return {
        "id": "ls-" + key,
        "path": path,
        "file": "%s/%s" % (config["mount"].rstrip("/"), path),
        "name": name,
        "release": "live-sports:" + key,
        "media_info": probe,
        "display": {key: value for key, value in display.items() if value},
        "collections": [group] if group else [],
    }


def playable(probe):
    """An HLS probe with a moving picture: what rex plays as a live channel. live-sports shows a
    placeholder while a channel warms up, a 0x0 png with no sound, which rex cannot convert."""
    return (isinstance(probe, dict) and (probe.get("format") or {}).get("format_name") == "hls"
            and any(stream.get("codec_type") == "video" and (stream.get("width") or 0) > 0
                    and stream.get("codec_name") not in ("png", "mjpeg", "bmp", "gif")
                    for stream in probe.get("streams") or []))


class Sync:
    def __init__(self, config):
        self.config = config
        self.short_since = None
        self.picky = None
        self.logos, self.logos_at = None, 0
        self.routes = {}  # key -> the channel id the relay plays it from
        self.guides, self.guides_at, self.guides_for = None, 0, 0
        self.numbered = None
        self.espn = {}  # (league, day) -> (when it was fetched, its games)
        self.slotted = None  # slot key -> the probe of its last game
        self.pushed = {}  # item id -> its channel as last pushed, which rex keeps while an item has none
        self.now = time.time

    def rex(self, method, path, body=None):
        request = urllib.request.Request(self.config["rex_url"].rstrip("/") + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read() or b"{}")

    def register(self):
        answer = self.rex("PUT", "/rex/sources/" + self.config["source"], {
            "url": self.config["relay_url"], "mount": self.config["mount"],
            "directories": [{"name": self.config["directory"], "movies": self.config["library"]}],
            # A channel that ends never comes back as the same file: rex deletes what a snapshot
            # leaves out instead of trashing it (2,115 trashed channels on rex-fun, 2026-11-01).
            "trash": False})
        for ignored in answer.get("ignored") or []:
            LOG.warning("rex ignored %s: %s", ignored.get("entry"), ignored.get("reason"))

    def ffprobe(self, url):
        probe = self.run_ffprobe(["-show_format", "-show_streams"], url)
        video = next((stream for stream in probe.get("streams") or [] if stream.get("codec_type") == "video"), None)
        if video is not None and video.get("field_order") in (None, "unknown"):
            # A live stream joined mid-GOP is described before a frame is decoded, so ffprobe
            # leaves field_order unknown; rex reads interlacing from it, and browsers cannot play
            # field-coded H.264 that rex passes through (Chrome, fun, 2026-10-27).
            frames = self.run_ffprobe(["-select_streams", "v:0", "-read_intervals", "%+4",
                                       "-show_entries", "frame=interlaced_frame,top_field_first"], url).get("frames") or []
            if frames:
                interlaced = [frame for frame in frames if frame.get("interlaced_frame")]
                if len(interlaced) * 2 <= len(frames):
                    video["field_order"] = "progressive"
                else:
                    top = sum(1 for frame in interlaced if frame.get("top_field_first"))
                    video["field_order"] = "tt" if top * 2 >= len(interlaced) else "bb"
        return probe

    def run_ffprobe(self, args, url):
        command = [self.config["ffprobe"], "-v", "error", "-print_format", "json"] + args
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
                    kept = json.load(handle)
                # Probes kept before field_order was read are probed again once.
                if playable(kept) and all(stream.get("field_order") not in (None, "unknown") for stream in kept["streams"]
                                          if stream.get("codec_type") == "video"):
                    return kept
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

    def numbers(self, metas, now):
        """Each channel's number in the guide, `<group>.<n>`, kept in `channels.json`: a group gets
        the lowest number from 100 no group holds (rex's guide is one for every source, and
        another's, or a tuner's, are the low ones: `2.1`), a channel the lowest one free in its
        group, both in the
        order of their names the first time they are numbered. A channel keeps its number while
        live-sports lists it at least once a week, wherever its group goes (a week this does not
        run does not count); after that its number is free again, and its group's once none of its
        channels is left. A lineup's channel and a slot have the number they are given."""
        path = os.path.join(self.config["cache"], "channels.json")
        if self.numbered is None:
            try:
                with open(path) as handle:
                    self.numbered = json.load(handle)
            except (OSError, ValueError):
                self.numbered = {"groups": {}, "channels": {}}
        groups, channels = self.numbered["groups"], self.numbered["channels"]
        fixed = {key: meta["number"] for key, meta in metas.items() if meta.get("number")}
        last = max((seen for _, seen in channels.values()), default=now)
        for key in [key for key, (_, seen) in channels.items() if last - seen > 7 * 86400]:
            del channels[key]
        held = {number.split(".")[0] for number, _ in channels.values()}
        for group in [group for group, number in groups.items() if str(number) not in held]:
            del groups[group]
        for key, meta in sorted(metas.items(), key=lambda pair: (str(pair[1].get("genre") or ""), str(pair[1].get("name") or ""))):
            if key in fixed:
                continue
            if key not in channels:
                group = str(meta.get("genre") or "")
                if group not in groups:
                    groups[group] = next(number for number in itertools.count(100) if number not in groups.values())
                taken = {number for number, _ in channels.values()}
                channels[key] = [next(number for number in ("%d.%d" % (groups[group], at) for at in itertools.count(1))
                                      if number not in taken), now]
            channels[key][1] = now
        os.makedirs(self.config["cache"], exist_ok=True)
        with open(path + ".tmp", "w") as handle:
            json.dump(self.numbered, handle)
        os.replace(path + ".tmp", path)
        return dict({key: channels[key][0] for key in metas if key not in fixed}, **fixed)

    def guide_index(self, metas):
        """The guides of the listed channels' sources, fetched every GUIDE_HOURS, programmes from
        three hours back (rex lists an airing until three hours after it ends) to two days ahead:
        Pluto TV's schedule when a Pluto TV channel is listed, and epgshare01's XMLTV files of
        the countries the other channels are of, each downloaded again only when it changed.
        Fetched again sooner for twice as many channels as it was fetched for: live-sports lists
        a few while it starts."""
        now = self.now()
        if self.guides is not None and now - self.guides_at < GUIDE_HOURS * 3600 and len(metas) < 2 * self.guides_for:
            return self.guides
        since, until = now - 3 * 3600, now + 48 * 3600
        guides = {"names": {}, "programmes": {}}
        if any(str(meta.get("id")).startswith("lstv-pl-") for meta in metas):
            stamp = lambda at: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
            try:
                for channel in get_json("%s?start=%s&stop=%s" % (PLUTO_TV, stamp(since), stamp(until)), 120):
                    guides["programmes"]["pluto:" + str(channel.get("_id"))] = [
                        programme for programme in map(pluto_programme, channel.get("timelines") or []) if programme]
            except Exception as error:
                LOG.warning("Pluto TV guide: %s", error)
        index, wanted, codes = self.logo_index(), set(), set()
        for meta in metas:
            if not str(meta.get("id")).startswith("lstv-pl-"):
                wanted |= variants(index, str(meta.get("name") or ""))
                codes |= countries(index, meta)
        folder = os.path.join(self.config["cache"], "guides")
        os.makedirs(folder, exist_ok=True)
        try:
            with urllib.request.urlopen(urllib.request.Request(EPGSHARE01, headers={"User-Agent": AGENT}), timeout=60) as response:
                listing = response.read().decode(errors="replace")
        except Exception as error:
            LOG.warning("epgshare01: %s", error)
            listing = " ".join('"%s"' % name for name in os.listdir(folder))
        # Per country (`epg_ripper_UK1`, `epg_ripper_US_SPORTS1`); not US_LOCALS1, 57 MB of local stations.
        for name, code in sorted(set(re.findall(r'"(epg_ripper_([A-Z]{2})(?:_SPORTS)?\d+\.xml\.gz)"', listing))):
            if code not in codes:
                continue
            path = os.path.join(folder, name)
            try:
                download(EPGSHARE01 + name, path)
            except Exception as error:
                LOG.warning("epgshare01 %s: %s", name, error)
                if not os.path.exists(path):
                    continue
            try:
                with gzip.open(path) as stream:
                    names, programmes = xmltv(stream, code, wanted, since, until)
            except (OSError, EOFError, ET.ParseError) as error:
                LOG.warning("epgshare01 %s: %s", name, error)
                continue
            for plain_name, found in names.items():
                guides["names"].setdefault(plain_name, []).extend(found)
            guides["programmes"].update(programmes)
        LOG.info("guides: %d channels", sum(1 for programmes in guides["programmes"].values() if programmes))
        self.guides, self.guides_at, self.guides_for = guides, now, len(metas)
        return guides

    def guide(self, guides, meta, now):
        """A channel's guide: Pluto TV's for its Pluto TV channel; else epgshare01's for a channel
        of its name of one of its countries, or, when it is of no country known, of the only
        channel of its name; else its name."""
        channel = str(meta.get("id"))[len("lstv-"):]
        if channel.startswith("pl-"):
            return guides["programmes"].get("pluto:" + channel[3:]) or placeholder(meta, now)
        index = self.logo_index()
        codes = countries(index, meta)
        for name in sorted(variants(index, str(meta.get("name") or "")), key=len, reverse=True):
            found = guides["names"].get(name) or []
            for at in [at for code, at in found if code in codes] or ([found[0][1]] if not codes and len({at for _, at in found}) == 1 else []):
                if guides["programmes"].get(at):
                    return guides["programmes"][at]
        return placeholder(meta, now)

    def live_tv(self, items, metas):
        """Gives each item its Live TV channel, when it changed since it was last pushed: rex
        keeps the channel of an item pushed without one, and a guide moves only every few hours.
        `"channel": null` when `live_tv` is off, which ends a channel rex holds. Answers each
        item's channel as pushed."""
        now = self.now()
        numbers = self.numbers(metas, now) if self.config["live_tv"] else {}
        guides = self.guide_index([meta for meta in metas.values() if not meta.get("slot")]) if self.config["live_tv"] else None
        sent = {}
        for entry in items:
            meta, channel = metas[entry["id"]], None
            if self.config["live_tv"] and live(entry["media_info"]):
                name = str(entry["name"])
                games = meta.get("games") or []
                channel = {"number": numbers[entry["id"]], "title": name,
                           "call_sign": re.sub(r"\s*\([^)]*\)", "", name).strip() or name,
                           "guide": games if meta.get("slot") else around(self.guide(guides, meta, now), games)}
            sent[entry["id"]] = json.dumps(channel, sort_keys=True)
            if self.pushed.get(entry["id"]) != sent[entry["id"]]:
                entry["channel"] = channel
        return sent

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

    def draw(self, source, background, shape, path):
        width, height, box = SHAPES[shape]
        out = subprocess.run([self.config["ffmpeg"], "-v", "error", "-y", "-i", source, "-filter_complex",
                              "color=%s:s=%dx%d[bg];[0]scale=%d:%d:force_original_aspect_ratio=decrease,format=rgba[fg];"
                              "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=rgb24" % (background, width, height, box, box),
                              "-frames:v", "1", path + ".tmp.png"], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[-300:] or "ffmpeg failed")
        os.replace(path + ".tmp.png", path)

    def title(self, name, shape, path):
        """The channel's name in white on live-sports' dark colour, for a channel with no logo,
        wrapped to lines drawn one by one (ffmpeg 4.4's drawtext cannot centre lines). Drawn, not
        read from live-sports' SVG card: not every ffmpeg reads SVG (gyan.dev's Windows builds do
        not), and fontconfig there has no setup, so Windows names its font."""
        width, height, _ = SHAPES[shape]
        lines = textwrap.wrap(name, 16 if width < height else 24, break_long_words=True)[:4] or [name]
        size = max(16, min(72, int(width * 0.8 / max(len(line) for line in lines) * 1.8)))
        font = "fontfile='C\\:/Windows/Fonts/arialbd.ttf'" if os.name == "nt" else "font=Sans"
        files = ["%s.%d.txt" % (path, at) for at in range(len(lines))]
        for line, file in zip(lines, files):
            with open(file, "w", encoding="utf-8") as handle:
                handle.write(line)
        top = (height - len(lines) * size * 1.3) / 2
        graph = ",".join("drawtext=textfile='%s':expansion=none:%s:fontcolor=white:fontsize=%d:x=(w-tw)/2:y=%d"
                         % (os.path.basename(file), font, size, top + at * size * 1.3) for at, file in enumerate(files))
        try:
            out = subprocess.run([self.config["ffmpeg"], "-v", "error", "-y", "-f", "lavfi", "-i", "color=%s:s=%dx%d" % (DARK, width, height),
                                  "-vf", graph, "-frames:v", "1", os.path.basename(path) + ".tmp.png"],
                                 capture_output=True, text=True, timeout=60, cwd=os.path.dirname(path))
        finally:
            for file in files:
                os.remove(file)
        if out.returncode != 0 or not os.path.exists(path + ".tmp.png"):
            raise RuntimeError(out.stderr.strip()[-300:] or "ffmpeg failed")
        os.replace(path + ".tmp.png", path)

    def artwork(self, key, meta):
        """The channel's poster and background, as rex reads them from the relay: its logo when
        iptv-org has one, else its name. A picture is drawn once; its name carries what it was
        drawn from, so a new logo is a new path, which clients do not hold cached."""
        name = str(meta.get("name") or key)
        shown = str(meta.get("title") or name)
        folder = os.path.join(self.config["cache"], "art")
        os.makedirs(folder, exist_ok=True)
        for url in [url for url in [logo_for(self.logo_index(), name)] if url] + [None]:
            stem = "%s-%s" % (key, hashlib.sha1((url or "name:" + shown).encode()).hexdigest()[:8])
            out = {shape: os.path.join(folder, "%s-%s.png" % (stem, shape)) for shape in SHAPES}
            try:
                if not all(os.path.exists(path) for path in out.values()):
                    if url is None:
                        for shape, path in out.items():
                            self.title(shown, shape, path)
                    else:
                        self.logo(url, stem, folder, out)
                return {shape: "%s/%s/art/%s" % (self.config["relay_url"], self.config["directory"], os.path.basename(path))
                        for shape, path in out.items()}
            except Exception as error:
                LOG.info("%s: picture from %s: %s", key, (url or "its name").split("?")[0][:80], error)
        return {}

    def logo(self, url, stem, folder, out):
        data = fetch(url)
        # ffmpeg tells an SVG only by its name; other pictures by their bytes.
        source = os.path.join(folder, stem + (".svg" if data.lstrip()[:5] in (b"<?xml", b"<svg ") else ".source"))
        with open(source, "wb") as handle:
            handle.write(data)
        try:
            # How much the logo changes the two backgrounds says how much of it is drawn and how
            # bright that is; a mostly light logo goes on the dark one.
            on_black, on_white = self.luma(source, "black"), self.luma(source, "white")
            drawn = 1 - (on_white - on_black) / 255
            background = DARK if drawn > 0.05 and on_black / drawn > 150 else LIGHT
            for shape, path in out.items():
                self.draw(source, background, shape, path)
        finally:
            os.remove(source)

    def schedule(self):
        """ESPN's games from yesterday to two days ahead (its days are America's), by kickoff:
        each league's day asked again every GUIDE_HOURS, and every sync while one of its games is
        on, for its state. A day ESPN does not answer keeps what it answered last."""
        now = self.now()
        days = [time.strftime("%Y%m%d", time.gmtime(now + 86400 * at)) for at in (-1, 0, 1, 2)]
        self.espn = {at: kept for at, kept in self.espn.items() if at[1] in days}
        def stale(at):
            fetched, games = self.espn.get(at, (0, []))
            return now - fetched >= GUIDE_HOURS * 3600 or any(on(game, now) for game in games)
        def fetch_day(at):
            try:
                return at, (now, espn_games(at[0], get_json("%s%s/scoreboard?dates=%s" % (ESPN, at[0], at[1]), 20)))
            except Exception as error:
                LOG.warning("ESPN %s %s: %s", at[0], at[1], error)
                return at, self.espn.get(at, (0, []))
        with ThreadPoolExecutor(8) as pool:
            self.espn.update(pool.map(fetch_day, [at for at in ((league, day) for league in LEAGUES for day in days) if stale(at)]))
        games = {game["id"]: game for _, kept in self.espn.values() for game in kept}
        return sorted(games.values(), key=lambda game: game["programme"]["start"])

    def slots(self, games, catalog, metas):
        """The slots' items, adding their metas to `metas`. Each game goes to the first slot of its
        sport free at its kickoff (`deal`), and a slot plays the channel that shows its game
        while it is on: of those whose name calls both sides, in any group, the one of the
        tallest picture, then of the shortest name. With no game on, or no channel of it, its
        relay answers 503, and it keeps the probe of the last game it carried; before its first
        it is left out."""
        path = os.path.join(self.config["cache"], "slots.json")
        if self.slotted is None:
            try:
                with open(path) as handle:
                    self.slotted = json.load(handle)
            except (OSError, ValueError):
                self.slotted = {}
        now, lanes = self.now(), deal(games, self.config["slots"])
        playing = {}  # slot title -> the channels of the game on in it
        for title, lane in lanes.items():
            game = next((game for game in lane if on(game, now)), None)
            if game:
                playing[title] = {meta["id"][len("lstv-"):]: meta for meta in listed_metas(catalog) if shows(meta, game)}
        wanted = sorted({channel for found in playing.values() for channel in found})
        with ThreadPoolExecutor(8) as pool:
            probes = dict(zip(wanted, pool.map(self.probe, wanted)))
        items, before = [], json.dumps(self.slotted, sort_keys=True)
        for base, (sport, count) in enumerate(self.config["slots"].items(), SLOTS_FROM):
            for at in range(1, int(count) + 1):
                key, title = "slot-%s-%d" % (slug(sport), at), "%s %d" % (sport, at)
                found = sorted(((height(probes[channel]), -len(str(meta.get("name"))), channel)
                                for channel, meta in playing.get(title, {}).items() if probes[channel]), reverse=True)
                self.routes[key] = found[0][2] if found else None
                if found:
                    self.slotted[key] = probes[found[0][2]]
                if self.slotted.get(key):
                    meta = {"name": title, "genre": sport, "number": "%d.%d" % (base, at), "slot": True,
                            "games": one_at_a_time([game["programme"] for game in lanes.get(title, [])])}
                    metas["ls-" + key] = meta
                    items.append(item(self.config, key, meta, self.slotted[key], self.artwork(key, meta)))
        if json.dumps(self.slotted, sort_keys=True) != before:
            os.makedirs(self.config["cache"], exist_ok=True)
            with open(path + ".tmp", "w") as handle:
                json.dump(self.slotted, handle)
            os.replace(path + ".tmp", path)
        return items

    def once(self):
        catalog = get_json(self.config["live_sports_url"] + "/catalog/tv/all.json")
        listed = channels(self.config, catalog, self.config["lineup"])
        if not listed_metas(catalog):
            # live-sports lists nothing until its first checks finish after a start.
            LOG.info("live-sports lists no channel that plays yet")
            return
        self.register()
        self.routes.update({key: channel for key, channel, _ in listed})
        self.logo_index()
        with ThreadPoolExecutor(8) as pool:
            probes = list(pool.map(lambda row: self.probe(row[1]), listed))
            art = list(pool.map(lambda row, probe: self.artwork(row[0], row[2]) if probe else None, listed, probes))
        # One row a channel name, its first copy that plays: live-sports lists several copies
        # under one name at once, each was a row of its own, and rex-fun's Live Sports showed
        # ESPN+ USA 21 times (2026-11-01). Names are compared whole: different channels can share
        # a key's spelling (`espn-usa`), and keep their `-2`.
        items, named = [], set()
        for (key, _, meta), probe, pictures in zip(listed, probes, art):
            name = str(meta.get("name") or key).strip().lower()
            if probe and name not in named:
                named.add(name)
                items.append(item(self.config, key, meta, probe, pictures))
        metas = {"ls-" + key: meta for key, _, meta in listed}
        if self.config["lineup"] is not None:
            games = self.schedule()
            for _, _, meta in listed:
                if meta.get("espn"):
                    networks = {name.lower() for name in meta["espn"]}
                    meta["games"] = one_at_a_time([game["programme"] for game in games if game["networks"] & networks])
            items += self.slots(games, catalog, metas)
        if not items:
            return

        sent = self.live_tv(items, metas)
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
            # With only a lineup, its channels and the slots are what is expected, not what rex
            # holds from before it (807 channels on fun, 2026-11-02, where 28 were listed).
            if self.config["lineup"] is not None and self.config["lineup_only"]:
                held = len(self.config["lineup"]) + sum(1 for meta in metas.values() if meta.get("slot"))
            if len(items) * 2 < held:
                self.short_since = self.short_since or time.time()
                if time.time() - self.short_since < 1800:
                    LOG.info("live-sports lists %d channels where %d are expected: waiting", len(items), held)
                    return
            answer = self.snapshot(items, force=True)
        self.short_since = None
        self.pushed = {key: channel for key, channel in sent.items() if key not in self.rejected}
        LOG.info("pushed %d of %d channels, %d with their Live TV channel: %s", len(items), len(listed),
                 sum(1 for entry in items if "channel" in entry), {key: answer.get(key) for key in ("created", "updated", "removed")})

    def snapshot(self, items, force):
        session = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        chunks = [items[at:at + 200] for at in range(0, len(items), 200)]
        self.rejected = set()
        for part, chunk in enumerate(chunks):
            last = part == len(chunks) - 1
            body = {"session": session, "part": part, "complete": last, "items": chunk}
            if last and force:
                body["force"] = True
            answer = self.rex("PUT", "/rex/feed/%s/snapshot" % self.config["source"], body)
            for rejected in answer.get("rejected") or []:
                LOG.warning("rex rejected %s: %s", rejected.get("id"), rejected.get("reason"))
                self.rejected.add(rejected.get("id"))
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
            if routes[match[1]] is None:  # a slot with no game on
                self.send_error(503)
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
