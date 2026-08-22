#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "requests", "pycryptodome"]
# ///
"""deezer-cli — read and edit your Deezer account from the terminal.

Deezer exposes two APIs and this CLI uses both, picking the cheapest one that
can answer:

* The **public REST API** (`api.deezer.com`) needs no auth and powers all
  discovery — search, tracks/albums/artists, an artist's top tracks / related
  artists / radio, and the global charts. Great for finding music.

* The **private web API** (`www.deezer.com/ajax/gw-light.php`, the same one the
  web player calls) is the only way to touch *your* account — favourites
  (likes), playlists, Flow recommendations, listening history. It authenticates
  with the browser `arl` cookie: a long-lived (~1 year) session token. Every
  write also needs a per-session CSRF `api_token`, obtained by first calling
  `deezer.getUserData` — this client fetches and caches it automatically.

The `arl` cookie is read straight from a locally logged-in Chromium browser
(Chrome/Arc/Brave/Edge) by decrypting its cookie store with the app's macOS
keychain key — the same trick notion-cli / rentalready-cli use — so there is
nothing to paste. `DEEZER_ARL` overrides.
"""

from __future__ import annotations

import csv as csvlib
import hashlib
import hmac
import io
import json as jsonlib
import logging
import os
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import click
import requests
from Crypto.Cipher import AES, Blowfish
from Crypto.Protocol.KDF import PBKDF2

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
log = logging.getLogger("dz")

PUBLIC_API = "https://api.deezer.com"
GW_LIGHT = "https://www.deezer.com/ajax/gw-light.php"
CONFIG_PATH = Path.home() / ".config" / "deezer-cli" / "config.json"
# Album genre/release/label are immutable, so enrichment lookups are cached
# forever on disk — a re-run of `export-likes --enrich` only fetches new albums.
ALBUM_CACHE = Path.home() / ".cache" / "deezer-cli" / "albums.json"
# Every id the CLI resolves (track/artist/album/playlist -> name) is recorded
# here as a side effect of reads. Deezer ids are immutable, so there is no TTL;
# `resolve` reads this first and hits the API only for ids never seen.
IDS_CACHE = Path.home() / ".cache" / "deezer-cli" / "ids.json"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Chromium cookie stores this CLI knows how to open, in preference order:
# (label, cookie sqlite path, keychain service holding the AES key)
COOKIE_SOURCES = [
    ("chrome", "~/Library/Application Support/Google/Chrome/Default/Cookies", "Chrome Safe Storage"),
    ("chrome", "~/Library/Application Support/Google/Chrome/Profile 1/Cookies", "Chrome Safe Storage"),
    ("chrome", "~/Library/Application Support/Google/Chrome/Profile 2/Cookies", "Chrome Safe Storage"),
    ("arc", "~/Library/Application Support/Arc/User Data/Default/Cookies", "Arc Safe Storage"),
    ("brave", "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies", "Brave Safe Storage"),
    ("edge", "~/Library/Application Support/Microsoft Edge/Default/Cookies", "Microsoft Edge Safe Storage"),
]


# --------------------------------------------------------------------------- #
# Cookie extraction (decrypt a Chromium `v10` cookie with the keychain key)
# --------------------------------------------------------------------------- #
def _keychain_key(service: str) -> bytes | None:
    """Derive the AES key Chromium uses on macOS: PBKDF2(keychain secret)."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    secret = proc.stdout.strip().encode()
    return PBKDF2(secret, b"saltysalt", 16, count=1003,
                  prf=lambda p, s: hmac.new(p, s, hashlib.sha1).digest())


def _decrypt_cookie(encrypted: bytes, key: bytes) -> str | None:
    """Decrypt a Chromium `v10`/`v11` AES-CBC cookie value."""
    if not encrypted or encrypted[:3] not in (b"v10", b"v11"):
        return None
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv=b" " * 16)
        dec = cipher.decrypt(encrypted[3:])
        dec = dec[: -dec[-1]]  # strip PKCS7 padding
        try:
            return dec.decode()
        except UnicodeDecodeError:
            # Chrome >= M118 prefixes a 32-byte sha256(domain); skip it.
            return dec[32:].decode(errors="replace")
    except (ValueError, IndexError):
        return None


def iter_arls():
    """Yield (source_label, arl) from every local Chromium cookie store holding
    a `.deezer.com` `arl` cookie."""
    for label, path, service in COOKIE_SOURCES:
        store = Path(path).expanduser()
        if not store.exists():
            continue
        key = _keychain_key(service)
        if not key:
            continue
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "Cookies"
            try:
                shutil.copy2(store, tmp)  # copy: the live DB is locked by the browser
                con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
                rows = con.execute(
                    "SELECT encrypted_value FROM cookies "
                    "WHERE host_key LIKE '%deezer.com' AND name='arl'"
                ).fetchall()
                con.close()
            except (OSError, sqlite3.Error):
                continue
        for (enc,) in rows:
            val = _decrypt_cookie(bytes(enc), key)
            if val:
                yield label, val


# --------------------------------------------------------------------------- #
# DRM decryption (Deezer "BF_CBC_STRIPE", reverse-engineered from the web
# player's decrypt worker)
#
# The signed CDN stream is a "stripe": Blowfish-CBC (8-byte blocks, fixed IV
# 0..7 re-armed per chunk) is applied to the first 2048 bytes of every
# 6144-byte group; all other bytes are already plaintext. The 16-byte key is
# MD5-hex of the *track id string* folded with two interleaved 8-byte
# constants. Verified byte-for-byte against the player's own worker.
# --------------------------------------------------------------------------- #
_DRM_EVEN = (0x61, 0x39, 0x76, 0x30, 0x77, 0x35, 0x65, 0x67)
_DRM_ODD = (0x31, 0x6E, 0x66, 0x7A, 0x63, 0x38, 0x6C, 0x34)
_DRM_IV = bytes(range(8))
_DRM_BLOCK = 2048
_DRM_CHUNK = 6144


def drm_key(track_id: str | int) -> bytes:
    """The 16-byte Blowfish key for a track (keyed by the track id string)."""
    h = hashlib.md5(str(track_id).encode()).hexdigest()
    return bytes(
        ord(h[i]) ^ ord(h[i + 16]) ^ (_DRM_ODD if i % 2 else _DRM_EVEN)[7 - i // 2]
        for i in range(16)
    )


def drm_decrypt(data: bytes, track_id: str | int) -> bytes:
    """Decrypt a BF_CBC_STRIPE stream (the inverse of the player worker)."""
    key = drm_key(track_id)
    out = bytearray(data)
    n = len(out)
    # The worker's loop is `i + block < len` (strict): a group whose 2048-byte
    # stripe would run exactly to the end is left untouched. Verified against
    # the real worker (a 2048-byte buffer is a no-op; 6144 decrypts blk0 only).
    for i in range(0, n - _DRM_BLOCK, _DRM_CHUNK):
        cipher = Blowfish.new(key, Blowfish.MODE_CBC, iv=_DRM_IV)
        out[i:i + _DRM_BLOCK] = cipher.decrypt(bytes(out[i:i + _DRM_BLOCK]))
    return bytes(out)


# --------------------------------------------------------------------------- #
# Audio tagging (minimal ID3v2.4 for MP3, Vorbis comments for FLAC)
# --------------------------------------------------------------------------- #
def _syncsafe(n: int) -> bytes:
    """ID3v2.4 syncsafe integer (7 bits per byte)."""
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _id3v2_tag(title: str, artist: str, album: str) -> bytes:
    """A minimal ID3v2.4 header with TIT2/TPE1/TALB (UTF-16, BOM)."""
    frames = b""
    for fid, text in (("TIT2", title), ("TPE1", artist), ("TALB", album)):
        if not text:
            continue
        payload = b"\x01" + text.encode("utf-16")  # 0x01 = UTF-16 with BOM (ID3v2 spec)
        frames += fid.encode("ascii") + _syncsafe(len(payload)) + b"\x00\x00" + payload
    return b"ID3\x04\x00\x00" + _syncsafe(len(frames)) + frames


def _flac_tag(data: bytes, title: str, artist: str, album: str) -> bytes:
    """Fill (or insert) the VORBIS_COMMENT block with TITLE/ARTIST/ALBUM.

    Deezer's FLAC files carry the Vorbis comment in a type-4 metadata block
    (the one holding the 'reference libFLAC' vendor string); some encoders use
    type 3. We fill whichever exists (type 4 preferred) in place, preserving
    the block's 'last' flag; if neither is present we insert a type-4 block
    after STREAMINFO."""
    comments = [f"{k}={v}" for k, v in (("TITLE", title), ("ARTIST", artist),
                                        ("ALBUM", album)) if v]
    payload = struct.pack("<I", 0) + struct.pack("<I", len(comments))
    for c in comments:
        cb = c.encode("utf-8")
        payload += struct.pack("<I", len(cb)) + cb

    off, n = 4, len(data)
    found4 = found3 = None
    while off < n:
        hdr = data[off]
        ln = int.from_bytes(data[off + 1:off + 4], "big")
        t = hdr & 0x7F
        if t == 4 and found4 is None:
            found4 = (off, ln)
        elif t == 3 and found3 is None:
            found3 = (off, ln)
        off += 4 + ln
        if hdr & 0x80:
            break

    target = found4 or found3
    if target is not None:
        off, ln = target
        hdr = data[off]
        size = max(ln, len(payload))
        block = bytes([hdr]) + size.to_bytes(3, "big") \
            + payload.ljust(size, b"\x00")
        return data[:off] + block + data[off + 4 + ln:]

    # No Vorbis comment block — insert a type-4 one after STREAMINFO.
    hdr0 = data[4]
    ln0 = int.from_bytes(data[5:8], "big")
    insert_at = 8 + ln0
    if hdr0 & 0x80:  # STREAMINFO was the last block; it stops being last
        return data[:4] + bytes([hdr0 & 0x7F]) + data[5:insert_at] \
            + bytes([0x84]) + len(payload).to_bytes(3, "big") + payload
    return data[:insert_at] \
        + bytes([0x04]) + len(payload).to_bytes(3, "big") + payload \
        + data[insert_at:]


def _safe_filename(name: str) -> str:
    """Strip characters that are awkward in macOS/Windows filenames."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', " ", name)
    return re.sub(r"\s+", " ", name).strip(" .") or "untitled"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return jsonlib.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(jsonlib.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # chmod 600 — it holds a session


# --------------------------------------------------------------------------- #
# Deezer client
# --------------------------------------------------------------------------- #
class Deezer:
    """Deezer client: public REST for discovery, gw-light (arl cookie) for the
    account. The CSRF `api_token` is fetched lazily on the first write."""

    def __init__(self, arl: str | None = None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.arl = arl
        if arl:
            self.s.cookies.set("arl", arl, domain=".deezer.com")
        self._api_token: str | None = None
        self._user: dict | None = None
        self._user_data: dict | None = None  # full getUserData response

    # -- public REST ------------------------------------------------------- #
    def public(self, path: str, params: dict | None = None) -> dict:
        r = self.s.get(f"{PUBLIC_API}/{path.lstrip('/')}", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise click.ClickException(
                f"Deezer public API error: {err.get('type')}: {err.get('message')}"
            )
        return data

    # -- private gw-light -------------------------------------------------- #
    def gw(self, method: str, payload: dict | None = None,
           need_token: bool = True) -> dict:
        """Call a gw-light method. `deezer.getUserData` bootstraps the session
        and needs no prior token; everything else reuses the cached one."""
        if not self.arl:
            raise click.ClickException(
                "This needs your account. Run `deezer login` (or set DEEZER_ARL)."
            )
        token = ""
        if need_token:
            token = self._ensure_token()
        params = {
            "method": method,
            "input": "3",
            "api_version": "1.0",
            "api_token": token,
        }
        r = self.s.post(GW_LIGHT, params=params, json=payload or {}, timeout=30)
        r.raise_for_status()
        data = r.json()
        errors = data.get("error")
        # gw-light returns errors as a dict {CODE: message} or [] when none.
        if errors:
            # A stale/invalid CSRF token surfaces as VALID_TOKEN_REQUIRED; refetch once.
            if isinstance(errors, dict) and "VALID_TOKEN_REQUIRED" in errors and need_token:
                self._api_token = None
                token = self._ensure_token()
                params["api_token"] = token
                r = self.s.post(GW_LIGHT, params=params, json=payload or {}, timeout=30)
                r.raise_for_status()
                data = r.json()
                errors = data.get("error")
            if errors:
                raise click.ClickException(f"Deezer gw error on {method}: {errors}")
        return data.get("results", {})

    def _ensure_token(self) -> str:
        if self._api_token:
            return self._api_token
        res = self.gw("deezer.getUserData", need_token=False)
        self._user_data = res
        self._user = res.get("USER", {})
        token = res.get("checkForm")
        uid = self._user.get("USER_ID")
        if not token or not uid or uid == 0:
            raise click.ClickException(
                "Not logged in — the arl cookie is missing or expired. "
                "Log into deezer.com in your browser, then re-run `deezer login`."
            )
        self._api_token = token
        return token

    def user_data(self) -> dict:
        self._ensure_token()
        return self._user or {}

    # -- DRM download (the web player's own flow) -------------------------- #
    def song_data(self, track_id: str | int) -> dict:
        """`song.getData` — the web player's per-track fetch. Returns
        TRACK_TOKEN, MD5_ORIGIN and display metadata (SNG_TITLE / ART_NAME /
        ALB_TITLE). Needs the arl session + CSRF token."""
        return self.gw("song.getData", {"SNG_ID": int(track_id)})

    def media_urls(self, track_tokens: list[str], fmt: str) -> list[dict]:
        """`{URL_MEDIA}/v1/get_url` — exchange track tokens for signed CDN
        URLs (~20 h). One call per format; returns one entry (with `sources`)
        per token, in request order."""
        ud = self.user_data()
        url_media = (self._user_data or {}).get("URL_MEDIA") \
            or "https://media.deezer.com"
        lic = ((ud.get("OPTIONS") or {}).get("license_token"))
        if not lic:
            raise click.ClickException(
                "No license_token in getUserData — the account may not be "
                "entitled to stream (check your subscription)."
            )
        r = self.s.post(
            f"{url_media}/v1/get_url",
            json={
                "license_token": lic,
                "media": [{"type": "FULL",
                           "formats": [{"cipher": "BF_CBC_STRIPE", "format": fmt}]}],
                "track_tokens": track_tokens,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or "data" not in data:
            raise click.ClickException(f"get_url failed: {str(data)[:200]}")
        return data["data"]


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _dur(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?:??"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _harvest_ids(data) -> list[tuple]:
    """Pull (type, id, name) tuples out of any track/artist/album/playlist
    objects in an API payload (public lowercase or gw UPPER_SNAKE), so a read
    populates the id cache. Descends one level into obvious nested objects
    (a track's artist/album, an album's track list)."""
    out: list[tuple] = []

    def visit(obj):
        if isinstance(obj, list):
            for x in obj:
                visit(x)
            return
        if not isinstance(obj, dict):
            return
        # public shapes
        if "id" in obj and ("title" in obj or "name" in obj):
            t = obj.get("type")
            kind = {"artist": "artist", "album": "album", "playlist": "playlist",
                    "track": "track"}.get(t) or ("artist" if "name" in obj and "title" not in obj else "track")
            out.append((kind, str(obj["id"]), obj.get("title") or obj.get("name")))
            visit(obj.get("artist"))
            visit(obj.get("album"))
            visit((obj.get("tracks") or {}).get("data") if isinstance(obj.get("tracks"), dict) else None)
        # gw shapes
        if obj.get("SNG_ID"):
            out.append(("track", str(obj["SNG_ID"]), obj.get("SNG_TITLE")))
        if obj.get("ART_ID"):
            out.append(("artist", str(obj["ART_ID"]), obj.get("ART_NAME")))
        if obj.get("ALB_ID"):
            out.append(("album", str(obj["ALB_ID"]), obj.get("ALB_TITLE")))
        if obj.get("PLAYLIST_ID"):
            out.append(("playlist", str(obj["PLAYLIST_ID"]), obj.get("TITLE")))

    visit(data)
    return out


def _emit(rows: list[str], as_json: bool, data) -> None:
    _cache_ids(_harvest_ids(data))  # every read records the ids it resolved
    if as_json:
        click.echo(jsonlib.dumps(data, ensure_ascii=False, indent=2))
    else:
        click.echo("\n".join(rows))


def _fmt_track_public(t: dict) -> str:
    """One line for a public-API track object."""
    artist = (t.get("artist") or {}).get("name", "?")
    album = (t.get("album") or {}).get("title", "")
    tail = f"\t[{album}]" if album else ""
    return f"{t.get('id')}\t{artist} — {t.get('title')}\t{_dur(t.get('duration'))}{tail}"


def _fmt_track_gw(t: dict) -> str:
    """One line for a gw-light SNG object (UPPER_SNAKE keys)."""
    return (
        f"{t.get('SNG_ID')}\t{t.get('ART_NAME','?')} — {t.get('SNG_TITLE','?')}"
        f"\t{_dur(t.get('DURATION'))}\t[{t.get('ALB_TITLE','')}]"
    )


def get_client(require_account: bool = False) -> Deezer:
    """Build a client. Uses $DEEZER_ARL, else the saved config."""
    arl = os.environ.get("DEEZER_ARL") or load_config().get("arl")
    if require_account and not arl:
        raise click.ClickException(
            "No account session. Run `deezer login` to import the arl cookie "
            "from your browser (or set DEEZER_ARL)."
        )
    return Deezer(arl)


# --------------------------------------------------------------------------- #
# Likes export helpers
# --------------------------------------------------------------------------- #
def _all_favorites(dz: Deezer) -> list[dict]:
    """Every liked track, paginated (the API caps a page at ~2000)."""
    uid = dz.user_data().get("USER_ID")
    out, start, page = [], 0, 2000
    while True:
        res = dz.gw("favorite_song.getList", {"user_id": uid, "start": start, "nb": page})
        batch = res.get("data", [])
        out.extend(batch)
        total = res.get("total", len(out))
        start += len(batch)
        if not batch or start >= total:
            break
    return out


def _load_ids_cache() -> dict:
    if IDS_CACHE.exists():
        try:
            return jsonlib.loads(IDS_CACHE.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def _cache_ids(entries: list[tuple]) -> None:
    """Record resolved ids as {"<type>:<id>": name}. `entries` are (type, id,
    name) tuples; ids are namespaced by type since a track and an artist can
    share a numeric id. Best-effort — never fails a read."""
    if not entries:
        return
    try:
        cache = _load_ids_cache()
        for kind, _id, name in entries:
            if _id is None or name is None:
                continue
            cache[f"{kind}:{_id}"] = name
        IDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        IDS_CACHE.write_text(jsonlib.dumps(cache, ensure_ascii=False))
    except OSError:
        pass


def _load_album_cache() -> dict:
    if ALBUM_CACHE.exists():
        try:
            return jsonlib.loads(ALBUM_CACHE.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def _save_album_cache(cache: dict) -> None:
    try:
        ALBUM_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ALBUM_CACHE.write_text(jsonlib.dumps(cache))
    except OSError:
        pass  # cache is best-effort


def _enrich_albums(dz: Deezer, album_ids: list[str]) -> dict:
    """Map album_id -> {genre_id, genres, album_release, label}, hitting the
    public album API only for ids not already on disk. Honours 429 backoff."""
    cache = _load_album_cache()
    todo = [a for a in album_ids if str(a) not in cache]
    with click.progressbar(todo, label=f"Enriching {len(todo)} new albums") as bar:
        for i, aid in enumerate(bar):
            try:
                a = dz.public(f"album/{aid}")
                cache[str(aid)] = {
                    "genre_id": a.get("genre_id"),
                    "genres": [g["name"] for g in (a.get("genres") or {}).get("data", [])],
                    "album_release": a.get("release_date"),
                    "label": a.get("label"),
                }
            except (click.ClickException, requests.RequestException):
                cache[str(aid)] = {}  # negative-cache so a re-run skips it
            if i % 50 == 49:
                _save_album_cache(cache)  # checkpoint against interruption
                time.sleep(1)  # stay under the public API's ~50 req / 5 s ceiling
    _save_album_cache(cache)
    return cache


def _like_export_row(t: dict, album_meta: dict | None) -> dict:
    """Flatten a liked-track record (+ optional album enrichment) for export."""
    explicit = (t.get("EXPLICIT_TRACK_CONTENT") or {}).get("EXPLICIT_LYRICS_STATUS")
    ts = t.get("DATE_ADD")
    row = {
        "track_id": t.get("SNG_ID"),
        "title": t.get("SNG_TITLE"),
        "artist_id": t.get("ART_ID"),
        "artist": t.get("ART_NAME"),
        "album_id": t.get("ALB_ID"),
        "album": t.get("ALB_TITLE"),
        "duration_s": t.get("DURATION"),
        "rank": t.get("RANK_SNG"),
        "explicit": explicit,
        "track_release": t.get("DATE_START"),
        "date_added": (time.strftime("%Y-%m-%d", time.gmtime(int(ts))) if ts else None),
        "date_added_ts": ts,
    }
    if album_meta is not None:
        m = album_meta.get(str(t.get("ALB_ID"))) or {}
        row["genre_id"] = m.get("genre_id")
        row["genres"] = "; ".join(m.get("genres", [])) or None
        row["album_release"] = m.get("album_release")
        row["label"] = m.get("label")
    return row


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """Deezer from the terminal: search & discover (public API), manage likes
    and playlists, and download DRM-free MP3/FLAC files (your account via the
    browser arl cookie)."""


# -- auth -------------------------------------------------------------------- #
@cli.command()
@click.option("--source", help="Only try this browser (chrome/arc/brave/edge).")
def login(source):
    """Import the `arl` cookie from a locally logged-in browser and validate it."""
    tried = 0
    for label, arl in iter_arls():
        if source and label != source:
            continue
        tried += 1
        dz = Deezer(arl)
        try:
            u = dz.user_data()
        except click.ClickException:
            continue
        if u.get("USER_ID"):
            save_config({"arl": arl, "source": label})
            name = u.get("BLOG_NAME") or u.get("FIRSTNAME") or u.get("USER_ID")
            click.echo(f"Logged in as {name} (id {u['USER_ID']}) via {label}. "
                       f"Saved to {CONFIG_PATH}.")
            return
    if tried == 0:
        raise click.ClickException(
            "No `arl` cookie found in any local browser. Log into deezer.com "
            "in Chrome/Arc/Brave/Edge first, then re-run `deezer login`."
        )
    raise click.ClickException(
        "Found an `arl` cookie but it did not validate (expired?). Log into "
        "deezer.com again in your browser, then re-run `deezer login`."
    )


@cli.command()
@click.option("--arl", prompt=True, hide_input=True,
              help="Paste the arl cookie value (deezer.com → devtools → cookies).")
def auth(arl):
    """Manually store an `arl` cookie value (fallback when `login` can't read it)."""
    dz = Deezer(arl.strip())
    u = dz.user_data()
    save_config({"arl": arl.strip(), "source": "manual"})
    name = u.get("BLOG_NAME") or u.get("FIRSTNAME") or u.get("USER_ID")
    click.echo(f"Logged in as {name} (id {u['USER_ID']}). Saved to {CONFIG_PATH}.")


@cli.command()
@click.option("--json", "as_json", is_flag=True)
def whoami(as_json):
    """Show the logged-in account."""
    dz = get_client(require_account=True)
    u = dz.user_data()
    data = {
        "user_id": u.get("USER_ID"),
        "name": u.get("BLOG_NAME"),
        "first_name": u.get("FIRSTNAME"),
        "email": u.get("EMAIL"),
        "country": u.get("COUNTRY"),
        "offer": (u.get("OFFER_NAME") or (u.get("OPTIONS") or {}).get("license_country")),
    }
    rows = [f"{k}: {v}" for k, v in data.items() if v is not None]
    _emit(rows, as_json, data)


# -- discovery (public API) -------------------------------------------------- #
@cli.command()
@click.argument("query")
@click.option("--type", "kind", type=click.Choice(["track", "album", "artist", "playlist"]),
              default="track", help="What to search for (default: track).")
@click.option("--limit", default=25, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def search(query, kind, limit, as_json):
    """Search Deezer's catalogue (no login needed)."""
    dz = get_client()
    path = "search" if kind == "track" else f"search/{kind}"
    data = dz.public(path, {"q": query, "limit": limit}).get("data", [])
    rows = []
    for item in data:
        if kind == "track":
            rows.append(_fmt_track_public(item))
        elif kind == "album":
            rows.append(f"{item['id']}\t{(item.get('artist') or {}).get('name','?')} — "
                        f"{item.get('title')}\t{item.get('nb_tracks','?')} tracks")
        elif kind == "artist":
            rows.append(f"{item['id']}\t{item.get('name')}\t{item.get('nb_fan','?')} fans")
        else:  # playlist
            rows.append(f"{item['id']}\t{item.get('title')}\t{item.get('nb_tracks','?')} tracks"
                        f"\tby {(item.get('user') or {}).get('name','?')}")
    _emit(rows or ["(no results)"], as_json, data)


@cli.command()
@click.argument("track_id")
@click.option("--json", "as_json", is_flag=True)
def track(track_id, as_json):
    """Show a track's details."""
    dz = get_client()
    t = dz.public(f"track/{track_id}")
    rows = [
        f"{t.get('id')}\t{(t.get('artist') or {}).get('name')} — {t.get('title')}",
        f"album: {(t.get('album') or {}).get('title')}",
        f"duration: {_dur(t.get('duration'))}",
        f"release: {t.get('release_date')}",
        f"bpm: {t.get('bpm')}  rank: {t.get('rank')}",
        f"isrc: {t.get('isrc')}",
        f"link: {t.get('link')}",
    ]
    _emit(rows, as_json, t)


@cli.command()
@click.argument("album_id")
@click.option("--json", "as_json", is_flag=True)
def album(album_id, as_json):
    """Show an album and its track list."""
    dz = get_client()
    a = dz.public(f"album/{album_id}")
    rows = [
        f"{a.get('id')}\t{(a.get('artist') or {}).get('name')} — {a.get('title')}",
        f"released {a.get('release_date')}  ·  {a.get('nb_tracks')} tracks  ·  "
        f"{a.get('fans')} fans",
        "",
    ]
    for t in (a.get("tracks") or {}).get("data", []):
        rows.append(_fmt_track_public(t))
    _emit(rows, as_json, a)


@cli.command()
@click.argument("artist_id")
@click.option("--json", "as_json", is_flag=True)
def artist(artist_id, as_json):
    """Show an artist's profile."""
    dz = get_client()
    a = dz.public(f"artist/{artist_id}")
    rows = [
        f"{a.get('id')}\t{a.get('name')}",
        f"{a.get('nb_fan')} fans  ·  {a.get('nb_album')} albums",
        f"link: {a.get('link')}",
    ]
    _emit(rows, as_json, a)


@cli.command(name="artist-top")
@click.argument("artist_id")
@click.option("--limit", default=25, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def artist_top(artist_id, limit, as_json):
    """An artist's most popular tracks."""
    dz = get_client()
    data = dz.public(f"artist/{artist_id}/top", {"limit": limit}).get("data", [])
    _emit([_fmt_track_public(t) for t in data] or ["(none)"], as_json, data)


@cli.command(name="artist-related")
@click.argument("artist_id")
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def artist_related(artist_id, limit, as_json):
    """Artists similar to this one — a discovery jump-off."""
    dz = get_client()
    data = dz.public(f"artist/{artist_id}/related", {"limit": limit}).get("data", [])
    rows = [f"{a['id']}\t{a.get('name')}\t{a.get('nb_fan','?')} fans" for a in data]
    _emit(rows or ["(none)"], as_json, data)


@cli.command(name="artist-radio")
@click.argument("artist_id")
@click.option("--limit", default=25, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def artist_radio(artist_id, limit, as_json):
    """A radio-style track mix seeded from an artist (great for discovery)."""
    dz = get_client()
    data = dz.public(f"artist/{artist_id}/radio", {"limit": limit}).get("data", [])
    _emit([_fmt_track_public(t) for t in data] or ["(none)"], as_json, data)


@cli.command()
@click.option("--limit", default=25, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def chart(limit, as_json):
    """The global top-tracks chart."""
    dz = get_client()
    data = dz.public("chart", {"limit": limit}).get("tracks", {}).get("data", [])
    _emit([_fmt_track_public(t) for t in data] or ["(none)"], as_json, data)


# -- account: likes ---------------------------------------------------------- #
@cli.command()
@click.option("--limit", default=50, show_default=True)
@click.option("--oldest", is_flag=True,
              help="Oldest likes first (default is most-recently-liked first).")
@click.option("--json", "as_json", is_flag=True)
def likes(limit, oldest, as_json):
    """List your liked (favourite) tracks, most-recently-liked first."""
    dz = get_client(require_account=True)
    uid = dz.user_data().get("USER_ID")
    # The API returns favourites oldest-first, so to show the newest we fetch the
    # tail (start = total - limit) and reverse it. `total` comes back on any page.
    first = dz.gw("favorite_song.getList", {"user_id": uid, "start": 0, "nb": limit})
    total = first.get("total", 0)
    if oldest or total <= limit:
        data = first.get("data", [])[:limit]
        if not oldest:
            data = sorted(data, key=lambda t: t.get("DATE_ADD", 0), reverse=True)
    else:
        tail = dz.gw("favorite_song.getList",
                     {"user_id": uid, "start": max(0, total - limit), "nb": limit})
        data = sorted(tail.get("data", []),
                      key=lambda t: t.get("DATE_ADD", 0), reverse=True)
    _emit([_fmt_track_gw(t) for t in data] or ["(no likes)"], as_json, data)


@cli.command()
@click.argument("track_ids", nargs=-1, required=True)
def like(track_ids):
    """Like one or more tracks (by track id)."""
    dz = get_client(require_account=True)
    ids = [str(t) for t in track_ids]
    dz.gw("song.addFavorites", {"IDS": ids})  # batched: one call for all ids
    click.echo(f"liked {len(ids)} track(s): {' '.join(ids)}")


@cli.command()
@click.argument("track_ids", nargs=-1, required=True)
def unlike(track_ids):
    """Remove one or more tracks from your likes."""
    dz = get_client(require_account=True)
    ids = [str(t) for t in track_ids]
    dz.gw("song.removeFavorites", {"IDS": ids})
    click.echo(f"unliked {len(ids)} track(s): {' '.join(ids)}")


# -- account: playlists ------------------------------------------------------ #
@cli.command()
@click.option("--limit", default=200, show_default=True)
@click.option("--owned", is_flag=True, help="Only playlists you own (hide followed ones).")
@click.option("--json", "as_json", is_flag=True)
def playlists(limit, owned, as_json):
    """List your playlists (owned + followed). Use --owned for just yours."""
    dz = get_client(require_account=True)
    uid = dz.user_data().get("USER_ID")
    res = dz.gw("deezer.pageProfile",
                {"user_id": uid, "tab": "playlists", "nb": limit})
    data = ((res.get("TAB") or {}).get("playlists") or {}).get("data", [])
    if owned:
        data = [p for p in data if str(p.get("PARENT_USER_ID")) == str(uid)]
    rows = []
    for p in data:
        is_mine = str(p.get("PARENT_USER_ID")) == str(uid)
        mine = "★" if is_mine else " "
        who = "you" if is_mine else (p.get("PARENT_USERNAME") or "?")
        rows.append(f"{mine} {p.get('PLAYLIST_ID')}\t{p.get('TITLE')}"
                    f"\t{p.get('NB_SONG','?')} tracks\tby {who}")
    _emit(rows or ["(no playlists)"], as_json, data)


@cli.command(name="playlist")
@click.argument("playlist_id")
@click.option("--limit", default=200, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def playlist_show(playlist_id, limit, as_json):
    """Show a playlist's tracks."""
    dz = get_client(require_account=True)
    res = dz.gw("playlist.getSongs", {"playlist_id": str(playlist_id),
                                      "start": 0, "nb": limit})
    data = res.get("data", [])
    _emit([_fmt_track_gw(t) for t in data] or ["(empty)"], as_json, data)


@cli.command(name="playlist-create")
@click.argument("title")
@click.option("--description", default="")
@click.option("--json", "as_json", is_flag=True)
def playlist_create(title, description, as_json):
    """Create a new (empty) playlist and print its id."""
    dz = get_client(require_account=True)
    res = dz.gw("playlist.create", {
        "title": title, "description": description,
        "songs": [], "status": 0,
    })
    pid = res if isinstance(res, (str, int)) else (res or {}).get("PLAYLIST_ID", res)
    click.echo(jsonlib.dumps(res) if as_json else f"created playlist {pid}: {title}")


@cli.command(name="playlist-add")
@click.argument("playlist_id")
@click.argument("track_ids", nargs=-1, required=True)
def playlist_add(playlist_id, track_ids):
    """Add tracks to a playlist."""
    dz = get_client(require_account=True)
    songs = [[str(t), 0] for t in track_ids]
    dz.gw("playlist.addSongs", {"playlist_id": str(playlist_id), "songs": songs})
    click.echo(f"added {len(track_ids)} track(s) to playlist {playlist_id}")


@cli.command(name="playlist-remove")
@click.argument("playlist_id")
@click.argument("track_ids", nargs=-1, required=True)
def playlist_remove(playlist_id, track_ids):
    """Remove tracks from a playlist."""
    dz = get_client(require_account=True)
    songs = [[str(t), 0] for t in track_ids]
    dz.gw("playlist.deleteSongs", {"playlist_id": str(playlist_id), "songs": songs})
    click.echo(f"removed {len(track_ids)} track(s) from playlist {playlist_id}")


@cli.command(name="playlist-delete")
@click.argument("playlist_id")
def playlist_delete(playlist_id):
    """Delete a playlist."""
    dz = get_client(require_account=True)
    dz.gw("playlist.delete", {"playlist_id": str(playlist_id)})
    click.echo(f"deleted playlist {playlist_id}")


# -- account: discovery ------------------------------------------------------ #
@cli.command()
@click.option("--limit", default=25, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def flow(limit, as_json):
    """Deezer Flow — personalised track recommendations for your account."""
    dz = get_client(require_account=True)
    res = dz.gw("radio.getUserRadio", {"user_id": dz.user_data().get("USER_ID")})
    data = res.get("data", [])[:limit]
    _emit([_fmt_track_gw(t) for t in data] or ["(none)"], as_json, data)


@cli.command()
@click.option("--limit", default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def history(limit, as_json):
    """Your recent listening history."""
    dz = get_client(require_account=True)
    uid = dz.user_data().get("USER_ID")
    res = dz.gw("deezer.pageProfile", {"user_id": uid, "tab": "history", "nb": limit})
    data = ((res.get("TAB") or {}).get("history") or {}).get("data", [])
    _emit([_fmt_track_gw(t) for t in data] or ["(none)"], as_json, data)


@cli.command(name="export-likes")
@click.option("--enrich", is_flag=True,
              help="Join album genre / release / label from the public API "
                   "(fetches each unique album once, cached on disk).")
@click.option("--csv", "as_csv", is_flag=True, help="Output CSV instead of JSON.")
@click.option("-o", "--out", type=click.Path(dir_okay=False, writable=True),
              help="Write to this file instead of stdout.")
def export_likes(enrich, as_csv, out):
    """Dump ALL your liked tracks with their metadata (JSON, or --csv).

    Base fields come from the favourites API (artist, album, popularity, dates,
    duration, explicit). --enrich adds genre/label by joining each album via the
    public API — genre is stored on the album, not the track."""
    dz = get_client(require_account=True)
    likes_data = _all_favorites(dz)
    log.info(f"Fetched {len(likes_data)} liked tracks.")
    album_meta = None
    if enrich:
        album_ids = list({str(t.get("ALB_ID")) for t in likes_data if t.get("ALB_ID")})
        album_meta = _enrich_albums(dz, album_ids)
    rows = [_like_export_row(t, album_meta) for t in likes_data]

    if as_csv:
        buf = io.StringIO()
        writer = csvlib.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
        text = buf.getvalue()
    else:
        text = jsonlib.dumps(rows, ensure_ascii=False, indent=2)

    if out:
        Path(out).write_text(text)
        log.info(f"Wrote {len(rows)} rows to {out}")
    else:
        click.echo(text)


@cli.command()
@click.argument("track_ids", nargs=-1, required=True)
@click.option("--quality",
              type=click.Choice(["mp3_128", "mp3_320", "flac"]),
              default="mp3_128", show_default=True,
              help="Audio format (all three use the same DRM cipher).")
@click.option("-o", "--out-dir",
              type=click.Path(file_okay=False, writable=True),
              default=".", show_default=True)
@click.option("--overwrite", is_flag=True, help="Replace files that already exist.")
def download(track_ids, quality, out_dir, overwrite):
    """Download tracks as local MP3/FLAC files — DRM-decrypted and tagged.

    Reverse-engineered from the web player: song.getData -> track token,
    {URL_MEDIA}/v1/get_url -> signed CDN URL, then Blowfish-CBC "stripe"
    decryption keyed by the track id. Files are written as
    '<artist> - <title>.mp3' (or .flac) with ID3 / Vorbis tags.

    Examples:
      deezer download 3135553
      deezer download 3135553 3135554 --quality mp3_320 -o ~/Music/Deezer
    """
    dz = get_client(require_account=True)
    out = Path(out_dir).expanduser()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise click.ClickException(f"Cannot write to {out}: {e}")

    # 1) track tokens + metadata (one gw call per track; keep order, skip dead ids)
    songs: list[tuple[str, dict]] = []
    for tid in track_ids:
        if not str(tid).isdigit():
            log.warning(f"track {tid}: not a numeric track id, skipping")
            continue
        try:
            songs.append((str(tid), dz.song_data(tid)))
        except (click.ClickException, requests.RequestException) as e:
            log.warning(f"track {tid}: {getattr(e, 'message', None) or e}")

    if not songs:
        raise click.ClickException("No tracks could be resolved.")

    # 2) one get_url call for every token (single format per call)
    fmt = quality.upper()  # MP3_128 / MP3_320 / FLAC
    try:
        media = dz.media_urls([sd["TRACK_TOKEN"] for _, sd in songs], fmt)
    except (click.ClickException, requests.RequestException) as e:
        raise click.ClickException(f"get_url: {getattr(e, 'message', None) or e}")

    ext = "flac" if quality == "flac" else "mp3"
    used: set[str] = set()
    failures = 0
    for (tid, sd), entry in zip(songs, media):
        title = sd.get("SNG_TITLE") or f"track {tid}"
        artist = (sd.get("ART_NAME") or "Unknown Artist").strip()
        album = sd.get("ALB_TITLE") or ""

        # unique, filesystem-safe name (append the track id on collisions)
        base = _safe_filename(f"{artist} - {title}")
        name = f"{base}.{ext}"
        if name in used:
            name = f"{base} ({tid}).{ext}"
        used.add(name)
        target = out / name

        if target.exists() and not overwrite:
            click.echo(f"skip   {artist} — {title}\t{target} (exists)")
            continue

        # 3) download the encrypted stream (try each CDN source in turn)
        sources = ((entry.get("media") or [{}])[0].get("sources")) or []
        if not sources:
            log.error(f"track {tid}: no media source (unavailable?)")
            failures += 1
            continue
        blob = None
        for src in sources:
            try:
                r = dz.s.get(src["url"], timeout=180)
                if r.status_code == 200:
                    blob = r.content
                    break
            except requests.RequestException:
                continue
        if blob is None:
            log.error(f"track {tid}: CDN download failed")
            failures += 1
            continue

        # 4) decrypt + tag + write
        dec = drm_decrypt(blob, tid)
        if quality == "flac":
            dec = _flac_tag(dec, title, artist, album)
        else:
            dec = _id3v2_tag(title, artist, album) + dec
        target.write_bytes(dec)
        click.echo(f"saved  {artist} — {title}\t{target} ({len(dec) / 1e6:.1f} MB)")

    if failures:
        raise click.ClickException(f"{failures}/{len(songs)} track(s) failed")


@cli.command()
@click.argument("ids", nargs=-1, required=True)
@click.option("--type", "kind", type=click.Choice(["track", "artist", "album", "playlist"]),
              help="Restrict the API fallback to this type (default: try all).")
def resolve(ids, kind):
    """Resolve ids to names, local cache first, API only for unseen ids.

    Ids are cached (namespaced by type) as a side effect of every other read, so
    an id you've already encountered resolves for free. Pass `type:id` to scope
    one lookup, or a bare id to search all types."""
    dz = get_client()
    cache = _load_ids_cache()
    kinds = [kind] if kind else ["track", "artist", "album", "playlist"]
    for raw in ids:
        want_kind, _id = (raw.split(":", 1) if ":" in raw else (None, raw))
        search_kinds = [want_kind] if want_kind else kinds
        hit = next((cache.get(f"{k}:{_id}") for k in search_kinds
                    if cache.get(f"{k}:{_id}")), None)
        if hit:
            click.echo(f"{raw}\t{hit}\t(cached)")
            continue
        found = None
        for k in search_kinds:
            try:
                obj = dz.public(f"{k}/{_id}")
                name = obj.get("title") or obj.get("name")
                if name:
                    _cache_ids([(k, _id, name)])
                    found = (k, name)
                    break
            except (click.ClickException, requests.RequestException):
                continue
        click.echo(f"{raw}\t{found[1]}\t({found[0]})" if found else f"{raw}\t(not found)")


@cli.command()
@click.option("--json", "as_json", is_flag=True)
def genres(as_json):
    """List Deezer's top-level genres (id + name)."""
    dz = get_client()
    data = dz.public("genre").get("data", [])
    rows = [f"{g['id']}\t{g['name']}" for g in data]
    _emit(rows or ["(none)"], as_json, data)


if __name__ == "__main__":
    cli()
