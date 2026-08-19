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

```bash
# Discovery (no login)
deezer search "daft punk"                 # tracks; --type album|artist|playlist
deezer track  <id> ; deezer album <id> ; deezer artist <id>
deezer artist-top <id> ; deezer artist-related <id> ; deezer artist-radio <id>
deezer chart

# Account (needs login)
deezer whoami
deezer likes ; deezer like <id>... ; deezer unlike <id>...
deezer playlists [--owned] ; deezer playlist <id>
deezer playlist-create "Title" ; deezer playlist-add <pid> <id>...
deezer playlist-remove <pid> <id>... ; deezer playlist-delete <pid>
deezer flow ; deezer history
```

The first column of every list is the track/album/artist/playlist **id** — feed
it into `like`, `playlist-add`, etc. Add `--limit N` / `--json` to any read.

See [SKILL.md](SKILL.md) for the full command reference, the API method map, and
gotchas.

## How it works

- **Discovery** → Deezer's public REST API (`api.deezer.com`), no auth.
- **Account** → Deezer's private web API (`www.deezer.com/ajax/gw-light.php`,
  the web player's own), authenticated with the `arl` cookie plus a per-session
  CSRF token fetched via `deezer.getUserData`. Every private method was captured
  from the live web player and verified end-to-end.

## Tests

```bash
uv run --with pytest --with click --with requests --with pycryptodome -- pytest tests/ -q
```
