---
name: deezer-cli
description: >
  Read and manage your Deezer account from the terminal via deezer_cli.py.
  Use for music discovery (search tracks/albums/artists/playlists, an artist's
  top tracks / related artists / radio, global charts — all no-login) and for
  your account: list/add/remove likes, list playlists, create playlists and
  add/remove/delete their tracks, Deezer Flow recommendations, and listening
  history. Discovery uses Deezer's public REST API; account actions use the
  private web API authenticated with the browser `arl` cookie (auto-extracted,
  like notion-cli / rentalready-cli). Every read supports --json. Triggers:
  "search deezer", "like this song on deezer", "add to my deezer playlist",
  "what should I listen to", "deezer flow", "my deezer likes", "deezer cli".
---

# Deezer CLI

A terminal client for Deezer at `~/.claude/skills/deezer-cli/deezer_cli.py`. It
uses **two** Deezer APIs, picking the cheapest that can answer:

- **Public REST** (`api.deezer.com`) — no auth, powers all discovery: search,
  track/album/artist details, an artist's top tracks / related artists / radio,
  and the global charts.
- **Private web API** (`www.deezer.com/ajax/gw-light.php`, the web player's own)
  — the only way to touch *your* account: likes, playlists, Flow, history. It
  authenticates with the browser `arl` cookie (a ~1-year session token) and a
  per-session CSRF `api_token` that the client fetches automatically.

Every private method name and payload in this tool was captured from the live
web player's network traffic and verified end-to-end against a real account —
not guessed.

## How to invoke

Invoke it as **`deezer`** if it's on `$PATH` (symlink `bin/deezer` into
`~/.local/bin`), otherwise run the bundled launcher `bin/deezer` (PEP 723 — `uv`
resolves deps inline on first run). Examples below use `deezer`.

```bash
ln -sfn ~/.claude/skills/deezer-cli/bin/deezer ~/.local/bin/deezer   # once
```

## Authentication

Discovery commands need **no** login. Account commands need the `arl` cookie:

```bash
deezer login              # extract arl from a logged-in Chromium browser + validate
deezer login --source arc # only try one browser (chrome/arc/brave/edge)
deezer auth               # manual fallback: paste the arl value (hidden input)
deezer whoami             # verify the saved session
```

`login` decrypts the browser cookie store with the macOS keychain key (may pop
one "Allow" dialog), finds the first `arl` that validates, and saves it chmod-600
to `~/.config/deezer-cli/config.json`. `$DEEZER_ARL` overrides. The `arl`
survives ~1 year unless you log out of deezer.com; if account commands start
failing with "not logged in", re-run `deezer login` after visiting deezer.com in
your browser.

To grab `arl` manually: deezer.com → devtools → Application → Cookies →
`https://www.deezer.com` → `arl`.

## Names or ids — everywhere

Every command that takes a track/album/artist/playlist accepts **either the
numeric id or a name**. Numeric args pass through untouched; anything else is
resolved under the hood and the pick is logged to stderr so a wrong guess is
visible:

- **track / album / artist** names → top hit of the public search
  (`"one more time daft punk" → track 3135553 (Daft Punk — One More Time)`).
  Include the artist in a track name to disambiguate covers.
- **playlist** names → matched against *your* owned + followed playlists by
  title: exact case-insensitive match first, then a unique substring; several
  hits error out listing the candidates (use the id). `playlist <name>` falls
  back to public playlist search when nothing of yours matches; the destructive
  `playlist-add/-remove/-delete` never do.
- **`unlike` / `playlist-remove`** track names are matched against your likes /
  the playlist's own contents (exact title, then unique `artist — title`
  substring) — never a catalogue search, so the removed track is always one
  that is actually there.

Ids remain the unambiguous fast path — prefer them in scripts and when acting
on rows you just listed.

## Discovery (no login)

```bash
deezer search "daft punk"                      # tracks (id, artist — title, dur, [album])
deezer search "random access" --type album     # or artist / playlist
deezer track  "one more time daft punk"         # full track details (id or name)
deezer album  "random access memories"          # album header + track list
deezer artist "daft punk"                       # artist profile
deezer artist-top     "daft punk"               # most popular tracks
deezer artist-related 27                        # similar artists — discovery jump-off
deezer artist-radio   27                        # radio-style mix seeded from the artist
deezer chart                                     # global top-tracks chart
deezer genres                                    # Deezer's 22 top-level genres (id + name)
deezer resolve track:<id> artist:<id> ...        # id -> name, cache-first (see below)
```

### Resolved-id cache

Every read records the ids it touches (track/artist/album/playlist → name) to
`~/.cache/deezer-cli/ids.json`, namespaced by type since a track and an artist
can share a numeric id. Deezer ids are immutable, so there is no TTL. `resolve`
checks this cache first and only calls the API for ids never seen — pass
`type:id` to scope a lookup, or a bare id to search all types (and `--type` to
restrict the API fallback). So an id surfaced by an earlier `search` / `likes` /
`playlist` resolves for free later.

### How Deezer classifies music

Genre is stored on the **album** (`genre_id` + a `genres[]` list that can name
sub-genres like "Indie Pop/Folk"); tracks inherit it and **artists carry no
genre field** (it's derived from their releases). `deezer genres` lists the 22
formal top-level genres. Above that sit editorial *channels* (moods/activities/
decades — Chill, Focus, Party, années 80…) and per-genre curated radios; those
are browse-only in the web player and not (yet) exposed as commands.

The first column is always the **id** — feed it into `like`, `playlist-add`,
`track`, etc. Add `--limit N` and `--json` to any of these.

## Your account (needs `login`)

```bash
# Likes (tracks by id or name)
deezer likes                       # most-recently-liked first
deezer likes --oldest --limit 100  # oldest first
deezer like   "veridis quo daft punk" [<id>...]  # like one or more (batched)
deezer unlike "veridis quo" [<id>...]  # names matched against your likes

# Playlists (playlist + tracks by id or name)
deezer playlists                   # owned (★) + followed; --owned for just yours
deezer playlist "Running"          # a playlist's tracks (yours first, then public)
deezer playlist-create "Title" --description "..."   # prints the new playlist id
deezer playlist-add    "Running" "get lucky" [<id>...]
deezer playlist-remove "Running" "get lucky"   # names matched against its contents
deezer playlist-delete "Running"   # name resolved only against YOUR playlists

# Discovery tied to your account
deezer flow            # Deezer Flow — personalised recommendations
deezer history         # recent listening history

# Bulk export
deezer export-likes                    # ALL likes as JSON (artist, album, rank, dates…)
deezer export-likes --csv -o likes.csv # …as CSV to a file
deezer export-likes --enrich --csv -o likes.csv  # + genre/label/release joined
                                       # from the public album API (each unique
                                       # album fetched once, cached forever on disk)
```

`export-likes` paginates the whole favourites list. Base fields come from the
favourites API; `--enrich` joins the album API for `genre_id` / `genres` /
`album_release` / `label` (genre isn't on the track). The album cache lives at
`~/.cache/deezer-cli/albums.json`, so a second `--enrich` run only fetches
newly-liked albums. A full enrich of ~1200 unique albums takes ~3 minutes
(paced under the public API's ~50 req / 5 s limit).

A discovery→action flow looks like: `deezer search "…"` (or `artist-radio`,
`flow`, `chart`) to get track ids, then `deezer like <id>` or
`deezer playlist-add <playlist> <id> <id>` to act on them — or skip the search
step entirely and pass names directly (`deezer like "instant crush"`), checking
the logged resolution line.

## Download (needs `login`)

Download tracks as local, DRM-free audio files — the web player's own pipeline,
reverse-engineered end-to-end:

```bash
deezer download <track_id_or_name> [...]          # mp3_128 by default
deezer download "one more time daft punk" --quality mp3_320   # or flac
deezer download 3135553 3135563 -o ~/Music/Deezer # batch + output dir
deezer download 3135553 --overwrite               # replace existing files
```

- `--quality mp3_128|mp3_320|flac` (default `mp3_128`) — all three use the same
  DRM cipher; only the requested format differs.
- `-o/--out-dir` (default `.`) — created if missing. Files are named
  `<artist> - <title>.mp3` / `.flac`; a name collision within one run appends
  ` (<id>)`.
- Existing files are **skipped** by default (`skip … (exists)`); `--overwrite`
  replaces them.
- MP3s get a minimal ID3v2.4 tag (TIT2/TPE1/TALB, UTF-16+BOM); FLACs get their
  Vorbis comment filled in place with TITLE/ARTIST/ALBUM. No new dependencies —
  tags are written by hand and the Blowfish stripe uses `pycryptodome`.

Per-track pipeline: `song.getData` → `TRACK_TOKEN`, then **one batched**
`{URL_MEDIA}/v1/get_url` call (all tokens, single format) → signed CDN URLs
(~20 h), then the encrypted stream is fetched (each `sources[]` entry tried in
turn) and decrypted with a Blowfish-CBC "stripe" keyed by an MD5 fold of the
track id.

## Output conventions

- Default output is compact, one line per row, tab-separated
  (`id  artist — title  m:ss  [album]`) — cheap to read in an agent context.
- `--json` prints the raw API objects (public commands) or gw `results.data`
  (account commands) for piping to `jq`/python.
- Big pulls: redirect to a file and filter rather than reading it all back.

## Method map (verified against the live web player)

| Command | API call |
| --- | --- |
| search / track / album / artist / artist-* / chart / genres / resolve | `api.deezer.com` public REST |
| export-likes | `favorite_song.getList` (paginated) + `album/{id}` public REST for `--enrich` |
| login / whoami | `deezer.getUserData` (→ `checkForm` CSRF token) |
| likes | `favorite_song.getList` (oldest-first; CLI fetches the tail for recency) |
| like / unlike | `song.addFavorites` / `song.removeFavorites`, payload `{"IDS":[...]}` |
| playlists | `deezer.pageProfile` tab `playlists` → `TAB.playlists.data` |
| playlist | `playlist.getSongs` |
| playlist-create/-add/-remove/-delete | `playlist.create` / `.addSongs` / `.deleteSongs` / `.delete` |
| flow | `radio.getUserRadio` |
| history | `deezer.pageProfile` tab `history` → `TAB.history.data` |
| download | `song.getData` (per track) + `{URL_MEDIA}/v1/get_url` (batched, one format per call) |

## Gotchas

- **Unofficial API** (`gw-light.php`): method names drift over time. Likes use
  the *plural* `song.addFavorites`/`removeFavorites` with an `IDS` array — the
  singular forms return `GATEWAY_ERROR: Undefined or invalid output`. If a call
  starts failing that way, re-capture the real method from the web player's
  network tab and update the mapping above.
- **CSRF token** (`api_token`) is per-session and fetched lazily on the first
  account call; a `VALID_TOKEN_REQUIRED` error triggers one automatic refetch.
- `playlists` lists owned **and** followed playlists (★ marks yours); pass
  `--owned` to filter to only yours. Only your own playlists are editable.
- `favorite_song.getList` returns favourites **oldest-first**; `likes` reverses
  the newest page so the default view is most-recently-liked.
- Public search matches Deezer's catalogue, which occasionally surfaces a
  same-name cover/karaoke — check the artist/album columns before acting. The
  same applies to name refs: resolution takes the **top** search hit, so put
  the artist in the query (`"get lucky daft punk"`) and check the logged
  `"…" → track <id> (…)` line; use the id when precision matters.
- **One format per `get_url` call** — the endpoint takes a single
  `formats[]`; to get both MP3 and FLAC for a track, run `download` twice with
  different `--quality`.
- **Signed URLs + track tokens expire in ~20 h** — they're fetched fresh on
  every `download` run, so this only matters if you reuse a captured URL.
- **DRM key = the track id string** — the Blowfish key is an MD5 fold of
  `str(track_id)`. The cipher is symmetric with no auth tag, so a wrong id
  silently yields garbage audio rather than an error.
- **FLAC Vorbis comment lives in a type-4 metadata block** — Deezer's FLACs
  carry the comment (the `reference libFLAC …` vendor string) in a type-4 block,
  not the spec's type-3. `_flac_tag` fills an existing type-4 block (type-3
  fallback, insert type-4 if neither) in place, preserving the layout; mutagen
  reads it back correctly.

## Tests

```bash
uv run --with pytest --with click --with requests --with pycryptodome -- pytest tests/ -q
```

Unit tests cover the network-free helpers (duration/track formatting, Chromium
cookie AES-CBC decryption). Account-level behaviour is verified interactively
against a live account, not mocked.
