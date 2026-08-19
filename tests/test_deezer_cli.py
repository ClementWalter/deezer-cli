"""Unit tests for the pure, network-free helpers in deezer_cli.

Only functions that don't touch the network or the keychain are covered here:
formatting and the Chromium cookie AES-CBC decryption (exercised with a
locally-constructed ciphertext). API-level behaviour is verified interactively
against a live account, not mocked here.
"""

import importlib.util
from pathlib import Path

import pytest
from Crypto.Cipher import AES

# Load the single-file CLI as a module (it lives at the repo root, not a package).
_spec = importlib.util.spec_from_file_location(
    "deezer_cli", Path(__file__).resolve().parent.parent / "deezer_cli.py"
)
deezer_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deezer_cli)


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0:00"), (5, "0:05"), (65, "1:05"), (350, "5:50"), (3599, "59:59")],
)
def test_dur_formats_seconds(seconds, expected):
    assert deezer_cli._dur(seconds) == expected


def test_dur_handles_non_numeric():
    assert deezer_cli._dur(None) == "?:??"


def test_fmt_track_public_renders_artist_title_duration_album():
    track = {
        "id": 123,
        "title": "Get Lucky",
        "duration": 248,
        "artist": {"name": "Daft Punk"},
        "album": {"title": "Random Access Memories"},
    }
    assert deezer_cli._fmt_track_public(track) == (
        "123\tDaft Punk — Get Lucky\t4:08\t[Random Access Memories]"
    )


def test_fmt_track_gw_renders_upper_snake_keys():
    track = {
        "SNG_ID": "3135553",
        "SNG_TITLE": "One More Time",
        "ART_NAME": "Daft Punk",
        "DURATION": "320",
        "ALB_TITLE": "Discovery",
    }
    assert deezer_cli._fmt_track_gw(track) == (
        "3135553\tDaft Punk — One More Time\t5:20\t[Discovery]"
    )


@pytest.fixture
def chromium_cookie():
    """A `v10` Chromium cookie: AES-CBC, fixed IV of 16 spaces, PKCS7 padding."""
    key = b"0123456789abcdef"
    plaintext = b"my-secret-arl-token"
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    cipher = AES.new(key, AES.MODE_CBC, iv=b" " * 16)
    return key, b"v10" + cipher.encrypt(padded), plaintext.decode()


def test_decrypt_cookie_roundtrips(chromium_cookie):
    key, encrypted, expected = chromium_cookie
    assert deezer_cli._decrypt_cookie(encrypted, key) == expected


def test_decrypt_cookie_rejects_unversioned_blob():
    assert deezer_cli._decrypt_cookie(b"plaintext-no-prefix", b"0" * 16) is None


@pytest.fixture
def liked_track():
    return {
        "SNG_ID": "9054516", "SNG_TITLE": "Foni",
        "ART_ID": "70224", "ART_NAME": "Orfeas Peridis",
        "ALB_ID": "830506", "ALB_TITLE": "Ap' To Parathyro Koito",
        "DURATION": "254", "RANK_SNG": "1995", "DATE_START": "2000-01-01",
        "EXPLICIT_TRACK_CONTENT": {"EXPLICIT_LYRICS_STATUS": 0},
        "DATE_ADD": 1431079416,  # 2015-05-08 UTC
    }


def test_export_row_flattens_track_and_converts_date(liked_track):
    row = deezer_cli._like_export_row(liked_track, album_meta=None)
    assert row["date_added"] == "2015-05-08"
    assert row["artist"] == "Orfeas Peridis"
    assert "genres" not in row  # no album_meta => no enrichment columns


def test_export_row_joins_album_genre_when_enriched(liked_track):
    meta = {"830506": {"genre_id": 466, "genres": ["Folk", "World"],
                       "album_release": "2004-06-12", "label": "WM Italy"}}
    row = deezer_cli._like_export_row(liked_track, album_meta=meta)
    assert row["genre_id"] == 466
    assert row["genres"] == "Folk; World"
    assert row["label"] == "WM Italy"
