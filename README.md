# deezer-cli

Read and manage your Deezer account from the terminal: search & discover music
(public API, no login) and manage your likes, playlists, Flow, and history (your
account, via the browser `arl` cookie).

Single-file Python CLI (`deezer_cli.py`, PEP 723 — run with `uv`, deps resolve
inline on first run). Modelled on the session-cookie CLI pattern
(notion-cli / rentalready-cli): the `arl` cookie is read straight from a locally
logged-in Chromium browser by decrypting its cookie store with the macOS
keychain key, so there's nothing to paste.

## Install

```bash
ln -sfn "$PWD/bin/deezer" ~/.local/bin/deezer   # put `deezer` on $PATH
deezer login                                     # import + validate the arl cookie
```

`login` needs you to be logged into deezer.com in Chrome/Arc/Brave/Edge first.
`$DEEZER_ARL` overrides the saved cookie.

## Usage

Every command that takes a track/album/artist/playlist accepts **either the
numeric id or a name** — names are resolved via search under the hood and the
pick is logged to stderr (e.g. `"daft punk" → artist 27 (Daft Punk)`).

```bash
# Discovery (no login)
deezer search "daft punk"                 # tracks; --type album|artist|playlist
deezer track "one more time" ; deezer album "discovery" ; deezer artist "daft punk"
deezer artist-top "daft punk" ; deezer artist-related 27 ; deezer artist-radio 27
deezer chart ; deezer genres
deezer resolve track:<id> artist:<id>     # id -> name, cache-first (ids auto-cached on every read)

# Account (needs login)
deezer whoami
deezer likes ; deezer like "veridis quo daft punk" ; deezer unlike "veridis quo"
deezer playlists [--owned] ; deezer playlist "Running"
deezer playlist-create "Title" ; deezer playlist-add "Running" "get lucky" <id>...
deezer playlist-remove "Running" "get lucky" ; deezer playlist-delete "Running"
deezer flow ; deezer history

# Download tracks as local files — DRM-decrypted + tagged (needs login)
deezer download <track_id_or_name> [...]          # mp3_128 by default
deezer download "one more time daft punk" --quality mp3_320   # or flac
deezer download 3135553 3135563 -o ~/Music/Deezer # batch + output dir
deezer download 3135553 --overwrite               # replace existing files

# Bulk export of all your likes (JSON or CSV); --enrich joins album genre/label
deezer export-likes --enrich --csv -o likes.csv
```

The first column of every list is the track/album/artist/playlist **id** —
still the unambiguous way to feed `like`, `playlist-add`, etc. Name resolution
rules: track/album/artist names take the top public-search hit; playlist names
match your own + followed playlists by title (exact, then unique substring —
ambiguity errors out listing candidates); `unlike` / `playlist-remove` match
names against your likes / the playlist's own contents, never a catalogue
search. Add `--limit N` / `--json` to any read.

See [SKILL.md](SKILL.md) for the full command reference, the API method map, and
gotchas.

## How it works

- **Discovery** → Deezer's public REST API (`api.deezer.com`), no auth.
- **Account** → Deezer's private web API (`www.deezer.com/ajax/gw-light.php`,
  the web player's own), authenticated with the `arl` cookie plus a per-session
  CSRF token fetched via `deezer.getUserData`. Every private method was captured
  from the live web player and verified end-to-end.
- **Download** → reverse-engineered from the web player's audio pipeline:
  `song.getData` yields a per-track token, `{URL_MEDIA}/v1/get_url` exchanges it
  (plus the account's `license_token`) for a signed CDN URL, and the stream is
  decrypted with a Blowfish-CBC "stripe" cipher keyed by an MD5 fold of the
  track id. MP3s get a minimal ID3v2.4 tag; FLACs get their Vorbis comment
  filled in place (Deezer stores it in a type-4 metadata block).

## Tests

```bash
uv run --with pytest --with click --with requests --with pycryptodome -- pytest tests/ -q
```
