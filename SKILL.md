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

## Discovery (no login)

```bash
deezer search "daft punk"                      # tracks (id, artist — title, dur, [album])
deezer search "random access" --type album     # or artist / playlist
deezer track  <track_id>                        # full track details
deezer album  <album_id>                         # album header + track list
deezer artist <artist_id>                        # artist profile
deezer artist-top     <artist_id>                # most popular tracks
deezer artist-related <artist_id>                # similar artists — discovery jump-off
deezer artist-radio   <artist_id>                # radio-style mix seeded from the artist
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
# Likes
deezer likes                       # most-recently-liked first
deezer likes --oldest --limit 100  # oldest first
deezer like   <track_id> [<id>...] # like one or more (batched into one call)
deezer unlike <track_id> [<id>...] # remove from likes

# Playlists
deezer playlists                   # owned (★) + followed; --owned for just yours
deezer playlist <playlist_id>      # a playlist's tracks
deezer playlist-create "Title" --description "..."   # prints the new playlist id
deezer playlist-add    <playlist_id> <track_id> [<id>...]
deezer playlist-remove <playlist_id> <track_id> [<id>...]
deezer playlist-delete <playlist_id>

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
`deezer playlist-add <playlist_id> <id> <id>` to act on them.

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
  same-name cover/karaoke — check the artist/album columns before acting.

## Tests

```bash
uv run --with pytest --with click --with requests --with pycryptodome -- pytest tests/ -q
```

Unit tests cover the network-free helpers (duration/track formatting, Chromium
cookie AES-CBC decryption). Account-level behaviour is verified interactively
against a live account, not mocked.
