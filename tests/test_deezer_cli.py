"""Unit tests for the pure, network-free helpers in deezer_cli.

Only functions that don't touch the network or the keychain are covered here:
formatting and the Chromium cookie AES-CBC decryption (exercised with a
locally-constructed ciphertext). API-level behaviour is verified interactively
against a live account, not mocked here.
"""

import base64
import importlib.util
import os
import struct
from pathlib import Path

import pytest
from Crypto.Cipher import AES, Blowfish

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


def test_harvest_ids_pulls_nested_public_track():
    payload = [{
        "id": 3135553, "title": "One More Time", "type": "track",
        "artist": {"id": 27, "name": "Daft Punk", "type": "artist"},
        "album": {"id": 302127, "title": "Discovery", "type": "album"},
    }]
    got = set(deezer_cli._harvest_ids(payload))
    assert ("track", "3135553", "One More Time") in got
    assert ("artist", "27", "Daft Punk") in got
    assert ("album", "302127", "Discovery") in got


def test_harvest_ids_pulls_gw_upper_snake_fields():
    payload = [{"SNG_ID": "9054516", "SNG_TITLE": "Foni",
                "ART_ID": "70224", "ART_NAME": "Orfeas Peridis",
                "ALB_ID": "830506", "ALB_TITLE": "Ap' To Parathyro Koito"}]
    got = set(deezer_cli._harvest_ids(payload))
    assert ("track", "9054516", "Foni") in got
    assert ("artist", "70224", "Orfeas Peridis") in got
    assert ("album", "830506", "Ap' To Parathyro Koito") in got


# --- DRM (BF_CBC_STRIPE) ---------------------------------------------------- #
# Vectors captured from a real Deezer stream: track 3135553 (MP3_128), the
# first 6144 bytes of the signed-CDN ciphertext, and the key derived from the
# worker's MD5-fold. The Node harness (player worker) and this implementation
# produce byte-identical output on the full 5 MB file.

_FIXTURE_B64 = """
DgHDLiyDdzVFZa2EDxnf8j6QvLolBBT2yWD0rDmL3qL2ferEmk+WubRQztXpb6NW3VCkhH3R\nJaoD woimYOvrtqgxWjGVwNgrL6X+ILwEqA+9Fk1Ah7JIRAOaTI85vAvrZo00mv6eY7LUPP1\n/slx+vlh2 m0/6VnOm6wZ5lk8J6q182K08KGYLoVNuF2RDqE4+7O6m8Wdrw0MI7OKdSzNXAB\nnre/x6lfUHr/nB MzFQcRGkxKpXFFVrqEECeHWxtfzN9okiuAwqEy1E9M/GtnVwcPCb5GjAJ\ne5Yw0npP/PzblSfPz/Y +oRG/kd88++YG/LuM7rMEgYNQdN9FMvVCSCXwIGsN8u2I80ff5u6\nlMOENUojaNOS/3UWOEd9yfcc ajfs7o5MQ84zPQiI2T3PugKCMRoPJlWPMl6iEx2NFPI5Osq\nVlo5xTt3hX5/krt4b2nmwHPgWKD8u shWv88hF8QYNFCjeQHQtJP4rB9sLZORUcrhsHLy8Ng\n7E8h/t1WHBXPi1rpHVElcRbG9dHnSDYevq sJ/BS6XIQvtR8UNgBzbAsNpjpyaDVfC8w+WcR\nRDUuCv96tVzFElT+WS8Vw401vrtKNKCUURlQEf+ 0gJcKrvhR5ODw1ZbueFbgUz5i59GUlOE\nAQVI8K2cinHjiJ+LaYm/rscJyCWCC+TEApa6jk3RXZ3q ePBukF8XP2+WYxzYztBHdguejF3\nVrL7Vfmsm47KDKqny1wEm0fVVG3W4KPoXmSXmUQZitQNoUi3/ +xbznQFcDqxPUGRth/Rnzu\nAubAVuWE4dUaH0Z8u4zaeVCP0m+F+bAz75v1cmyiR31DaN22sUKsVf s49pUZ+a1ZcSyNg7O\nuD+46fyjWy22TxZyugfQ8ItrGMB0kQFmEoue5TaXuEY0MLfWZaVFVwb6+Hr 1o3D5TxiALPH\n79RlFdgFFMuZGXvDxiRp9DoYSyzi2vKNwFeSJsCDFkQbpPPPPRVOpz7PapRNq8XE TWGZ/ge\nEIAr0Cx/xpf5kB6PBspW7MHD2ZU6cN8XFnkMEYTPckZOPH4jV3DoJ3BqAvH1v6ZYh2arj Yk\nyMKQQCXFL8gXhQpUyhLkT27l7MJK0nMRby1pxmuzbnA/ekq5Jeu1RkdE+lIAiAlqG/ZOKiny\nU4 IuAhkUKFCouMRduDZANHM+RPD5d4aKQdHTfSjWXzg88kTta6KDrJl2NKWfqTRVcLgs3+p\nqqHQiyC b4xnllVbSluwvUpf+n0PaD6xZMh77z7hcR+YXdcmb9w+QNrztbkBrtjgbtI9QE0t\nCQCt2Tcfjpba iPCqzQiNssOXe5WAUgl3voJG/hhmJ0rA7z761kGkjOT10EWwywQ61PIErOP\nYsbG5SDylbPodAHah yeBB/qz6n560fwEPxCG3Y9eh818Z7HXIXLRUJQybH/3imOCuk5m1PG\n2GeiCwYT037ljPDvi63INC QTTgswjEGOoqhekPxxydGjhEsy6kbcvnMsiLdusn5Fg8kzWDb\n+cUlfsxHK5/QjGZz0y22yJBTmyc 481TQhmt9loa/8C29YoSHsOVpcrpKIUIFfEinSo2vU6h\nPuv95H1N2CntiYKEiGzv1jbBeY7UjWBN hc+t3gAKvDR+0wd6aazBh0WWfQD2TtpuUedalmV\ngVX3R95KdsyKpUe/rRF7NK9iq98uJ1QmTUHWu vq1YnahoANRptGmeQF2xHuj7NmUgWWauA+\nEw3GVL8ujwVGfltda6M4G11w+7/r9PnR42s2okiPKc 91m3GLKNLDgyuwytF4/7c1vZ9NzYP\nQ39MYAyY2nL5N41mL1ikUn5utRLo/yfomxN0Ktpl30Lt5ew 7ANP+hTTzLP3J41i0Zg3lmfL\n04GsYXomvaAC4PwciKurbTAvUFOF9tjWTasezgPYW0WCz8LQs0Lc qMtG772MAO71TppEyY/\nr50PE7g/brsZpiTR4g9cYa1s+GvMpu3hKWa82WBQyoJJmXKV9eLr94brM RgPKFD4qrgCOaz\nLh1TmbP1NtITDzuumJA2FdnfwFrUJ8gvPnqc9QVc0dKuAR4Y6IybNgpaj3KNrj AsBJ8A561\nNtQv5VTzwlAfGSMLCHOs7PDTGbwo7KGX2AsKU0rUGDuCDiaOkF3CC1w5YgWErWxLqw6 UgA0\ntG5foN4g94i/SFf5TaW9UKQZKWKfCfzfYztT7jmLcZsz3/VO17bPTCtXXfIaYHuYVvg3g/l3\nKO6u4zUzYiNuHbNCWgVPoQzDM+iKb6BI+f+p2o2Xs/y/vDRxZt8GlR8awXx4LMl2CQ2CjG5Y\nOMh0 V+DcK3W92fIyb+mfWwwF+KSNF7km0SnhGfapEwkrTj857jvH4v33ABfMepDgIWrKMo5\nUhn3tJKO7 w7QAsVwZbEmA4+fWkRL8toEeyQDYnCInQwTGQup3gX2S/4AgVBDVZ+dj2Lf7w6\n6YXmxSGwCOxQv5 DOefFnCYMp/FbhyCHuK/bwRpbwrKujR1gAoTaNbf63A/g2o5cNyJ1pK/u\nZTajOQom139HxNaoG78 CApBCua43l0iI07RXZj6/ax4zXcW0p11KEaR/48QsTFyHxbYp0oD\nPHdkL+wKD5lpX5slWUQpNLEZ 6JHF5dBUdwPiH1v3ZSB4yGk/cpDwq+xDHqD5ozuC70FkUKq\nNMCbeeh6w+js2k8kx0OrpKbCTlpz2 89/8/Mc2Rd7jrSuVc0UHI6qpe+jR0OexeRKCRlpauV\ntChDd1sqVTsqa7cm8zX2lACvEMtaZiVyqb yAKK2Ib2NVIrvaoCKOlmtP1y5QLPHm63XNbrC\nt08RQNPUo46//uSZM0DA+NqWaHmHSIgIAspBCIA EDGtZQeYewivhCvkEIwAtN3aWj7LJaIJ\nkg+LWzBp63mdWWmb7tG+dqmssjVc+miIEZqNNqyrxxgi kiTWnScKI4KXFNqK6C18ObkY34x\nuBrwkw5VLPFtk3rbPi1icUTex++TdPoK149bGAAAANG4SRB5V UhhhMlVOtFqVfnPd9qHEdP\npSj//K/1a+vZ6d3f4dZaFBXpVtT8hL1Sr8JZmIS/dCnFjtDE7OGZ4V mo166EejkyfrAgWgB\n5dmElt54SMdfQkOVxrPKrAjq5EsYlHTw3BKo6yhZDzzaqsnh8HtsHttFslu PqWrep7HvCkm\nkESwbHIwn1g94KGF0KDFqbgkmMS2Ml6ds9hkrWqD7GbPF0Pgd2c3FSo8XZ7OvIuc SxyJNBg\nyygBCCokWzRjjTusE9blatFnOd2n36xjf5q7//ff/i9vq/0f3qrW0poEEDnLaO4W+YWIp 3F\nnNFHKuK8LYa8RUlW46glJQfvUqYGIbOtHivsVKWcVL3T9v8euTvozL4E59Tes+U07v30d2L/\n/7 kmTnAwVMbNdB7E5AJ4EK6QwjIhJZp2EHsREIlwPsKBCIIBKzRV60a+sxaF0e+y1Trz58/\n3urdUP9 b0wPW66Tbm/GZoFur1XLU2ZjwEzc5M9ZXfTuijOPRF4d307fu5OoCE9ciRSiJvUg\ngAIM61WXFPC+ B0Dqygahma05bu/1W1fcvR+/17/X/d+jR2eAtGAAcoSsCuOcu4vzcMY9S/n\nGTZTG8EuBKo+AFBNW 7OO2A7Rj6oWlgSwsoUFDfAVAqqq0XHUUU+hElrC6uIyRtUdYVXgY6b\nmpVFNWSH34tIlGiWTyyNYl IYHQidbw+iaScxWNMQ1WSnQ1B0Z5Kb5QbQ0gSKjjlS12urE8h\nKJ7qOGRgvlOSRrbc9Iya2NyMitN pSWSjS07azIS6e/4rOTc5epukNbKJdlLFIAAAABAAdKr\ntzx8a77rUKbC2nEUnDQJnhBO5LRfcS3X 2lfcqNWNv//63KtQr/rfr//9VaqkEAMAGClFwlS\nZk3FhN2GjX6AemNkhAyMeZIh/BtuMD2JaRSck wzEgFAVgNlvJyoE6h49LTBX/+5Jk4QNET2\nJYIew0QiPBOtkMIigVTa1ah7EyCMyF6njzFChAPCyR Fg8sLx2tbTDEyLnPxyqc7X1veJMp5\nybQRvLYT2ii7bFi8cXZJHkC10/62ws7n/Ys0he/8/ntVjbf 9CBg/eIoNsZuHnLXIIyxG3Fx\n+6+1aA99xctrTxInkN+ZfYlYMZr7a91+B6zKiM+cW/KItv8/7u4x FW6uM+UR7HMmAAAAAEN\ndFnrNUsKFbBbRU+4aEDr4jpWfQu/Z193u1VK//5y7du/yn3f9/6KwSggA ulvWHrQUdk691z\nRB25p+4AmSFBJy9p4QcWt6xmwidHWomw1zQFhHg/V+g1fiRWSXbLQ8RsOsgalD PmfOUy0+A\n9Fh7Z5xgwXZBNYF1WIO+Kg5Huu47VdEvsnI10jAwgqv/6Wy0crrZWvTrV4KMP2NWy8Y uQnh\nQviZvF+wmis1gbfy2TC5P9XWczKlqOcv+a7Ma9ib+9HlPljpr3N244cXx+9tutJfyNYKt9Zk\nkIAIIAAISTwGZtPdISd40X5dzzQSy/pZT5blOeVs++7/9Ct3//uSZNoDFaVr1iH4YqIsITqo\nICUy lfWvWow9jMiqB+swMozgQj/p///11YiSwAAziBmkOBBrOn8bC8DKZG88XhxBgi3XhlJ\nVZ1PUehtZ uWT8dtUyo2aC1I9TrzAp38E0311Q5xX2XM8LQliSC/Y9Ls83CeWPa/U8Km3dYc\nZxkm3GiQ37pzp4 VYCvdNaZhJJ0uB4aavD1O7zB877WdZn2+ZZvEbo0VFPWMj1tdrhTyYVku\nK3qz3PoOiVJGZyst2V1 ijswuWKa261sriEeVixinr6vzb5XOQ0ykOLPvNbHHPKXUa8VvwHu\nwdCACAAAjQ5K3YufhA3Bj6N+ PTXP9Hp6+j/WpTv/1X///d///RXGgVZZE87jKUyBrLSMGXN\nNhx0Y0gmDXROQiDv4CwFp6uOGAwp1 KFsPnKYZ1O0x0IdrLBPWAcb3b/Z/y3S2bMp2w1OOVj\n7Nqz9wQiq7AylbMXHrxx9Qk3r71LKUhunL JYJp7AGDDW9fTxu7Np1//v6p719JomGtAE8uR\nGMj8WX03+fu2QSkU36uI5WncFpd6lF1qNuCM//7 kmS7h0XrbNYjD2VwJMH6ywyiYBdFsVaM\nPY+AfAQrsBCYKE4jrFWyGxEJLD92kOlMtPPxLphy2nyN NNm+5Tc5K1l2HiNCQY/kIKYGGmT\nZzK1OIdbajFVbnU/29n/5hyP/7dP2/9/lVaiQQAAS0viaH2h4 iBlDlhkbWDdKpVgLANJfYi\nAjPaJz2OZVLZ3rxiMQabMrTBjcwSQp90+SrAn8QG1jjzEmhzM08WC0 bbSERlUrIu4kQpIGY\nNY0JlesOazlOFo7Q1x1BNoKQplrDJdK7D6LT33cb1RSde9joqVOro8cZWEs WrnWy32J0ThY\nYL9lwBDKRF21S826RLGLEV8gNgwRsjE1nnUCw2FiqDVDKOSFWEcZWVmhgiARZNuC yhTwynt\nsgsqotbdIAIAAACQRlOmc6pCCpOKWuPri/7/Od31xf2V//6P//u9v/VSCCQABbCwnkZZU gr\nEMGAkSBIFmV7OWM4lRHKECMn1IhxZJaMZTYpdAFTmkIKlWlSLIpHHZ7qRbZCCtUOMwlZM8cm\nR2 onVHgEETak8guqCdD/yS883/+5JknIMGB2vVoexPoiOBCswEIhQYHatUh7E7SJsH6zAwi\nKChL7LK HzpIVJT05StK4y0yXhmftnBX595tq6vOUznz/3pUtZjsEwFfkR6cMFNIjTnrqJpD\niRuDWy+1yeFx 6KjB/cyfaZiqJRhq5u+nTMZhzTrEjsUyF6S0hKltujIPoW0SrMEDU5AflqW\n9ISQ6ABIACANAKUzo iUl29RXTfilXstnaPV1/2ep9v/frQpbPX////+ukAkAAHaH+GiWIIW\nOldkjIGYZSq19WElVgZd1I +pQ3jXXgliYQpG0T5rDvA8MtjuI94UAF8p3KovkJbFwnpWFWL\ngH7BbcaoeqPmWC/o1tMZwU2kSYy +7VkFmczDSc0HFHlCVO2+NtZUyngMMVXOSGPyiOZpZ3K\nytetzhe8J3CpPjOIKGaZbvrPlAqSZInc JSm1Abm4+nDEVSKmqQgQZEjCSJdnCO4oWrlQ0yO\n6Mz0gjDDiQINNvohdUsxqBk27iJdedR3FcLtj Ymtgw+V0dWMWot38V9Voe1mJnJtinEVAEg\nAAfx6p0u2rUEfv//uSZHWHBu1r1CH4epIhAPrbDCJA G32vUQfl6kiQhCrsEKQoVyVKj9/R6\n76e3s/V//q3Xfs+r1oSMrScZ+FwDlEmIQkT6VpTPxMraD05 FQJwy1tDinbFo11ekS7nqAVv\nNkjQ5Ci/B2Eufpk0UAfqjJK9ZXBTEWu2Vne4X0m8fEzVcUnDpbgr pCFNmCxMUc/zkUrc7Vj\nJGTuXOFCXUZvvDbojgb86SLxEZGOG5RUrEjPu3Q548R715cvG9jV7yCW9 xbwdeGzURD8PnD\nbeu1ZuqjVENWrpwhIclYFosKGzuUJdLDmdTg4yRK2a6RzvVDgq3GCpGBtbn0DZ 3K99HXUGK\n/PCA1ObQ6irl1pykg7jDcxQ+qEIQSACAGY2yQRavIfkb02n10P0b/b3dn/aj/+1P/3f +36u\nj+xCgQAAAAAAAAAvAhS01S122rNQc6SLCSZijcoZYORr2oamIHnHRZTSyuAjIXZylSCkYNGi\n49EgMzasREatp8vhOHNzUBRiNTvqWjJm2ZG5XQES4QYDYvrqaPAYnrCj3KHaMyQzfa4K3I8k\njv/7 kmQ1Bwa1bNTzL2TwI4EK3iQmGBGxrV/HsQ9IxIgquMMMMJWM/i1REKcbTxmiXi2dy1n\nwz6gx9QIt C02mbbQGBJuJIINpixxUbdbwsQkAjIwM1YhLA8uCGWWFpdQj5K6y+vMxARQIm3\nFiJD8uj+eHl455 cqPUtzoP7k1PW/Dy2Xzah38VYkCh3ZwZMsMuMOWYAAAAiCDIiBRmLNJwV\nsPrbrOYm+t+37e/X/R/ /q/u///79coSkIuoCSPoemQlA4yQnYceULU8EWcSznZ4f3XAntUx\nM2ixUGrfYWOQxzLF2rafHcWR Wsev3cyCb8qlatTznp6HoNIS4XMDly+S7sxRsQTeDBkJtCc\n9P1f/SRLzIkyAIjrpy3I1tGjFotj3 YuNqsdBr4+kNSZiqSSZkrcm1jbQ+ESIzumdlz0eZhQ\n5fHtQYAAAAAACACLzk43xrtfu3Fjt5mRB/ V4WJtwndSS3bbP19f7Uf/2I7P1/o9v79P86mb\niQCIEjOyIULMuTmJ0ToujtCFQvhLzMY0WmTWZog /FEsSvF04QibDNnCVDAGARb/+5JkF4NF\nEWrWoe9Jcibg+twMIggRvadgh7EQyKAJ6pAyiNCgMhs8 RQkYkyOSKi0KutpWpqxu4mSvejj\nBX3qS+TES04RUfh9XIYa1McwunHbQ3Gv26nBPwR5OK+0I8eDa LI0awPHoKyrMKlvsi3sv4y\niaV2sjFE6mpVmrdc/GO78RMSxRGbg3fp5798nL2bndXbQ0omJGZEgA ABHkJHeykKVYNi3lE\naWoup6LtdtDf2fTd//v/Wyxe//f//fq7qaAgBjF2QohRNjRJKfL0vS2hb6C OU1LxBdT4QhT\nA8mdWPQTwsOIViSNoOhZdflrrRG5tCEcdCmB5ahlnSFnF9FgvIEyI6uQhFDoKhR0 DIsVobd\nVMznRt/Jc9SnesDK7FhvSsNQcOI+oGN1UQMD+7s+qS74odXM2nU361NTe0CGP3nhi67ie Hh\nL3QsmCSUAACobHibdz4RGsqYJGR+IRaYYQuh/dcv+j0ehf//Peyrkf3f+T/9euFAMIK1OE5O\nYe BPBa1Ie6JdtRv7DbVauUi+sOcqPTjxVbdsauQ+mG6jJkmZ9N//uSZBcDBRRrV6HpNmIqI\nmrMDMJQ EZGpZceYeUiMBCwwEIggiseteVFNsj0gYzyabUhKMm9dkL3X6wQDjOi/X+1yhUV2\nu1BGK5IIIhAx QTYXN0UZqTCTK66+/ekhfW9hMVzw9ew11fKt8GIEcITtjjBNJNeaZu8qSjm\nFFEqmhm0d9ZPptONZ Y8xTNEpW9plM2vswxBVnpC56QAAQABAAqiwsXLTl8W+rb/Ux/aDGMv\nq7LEeQv5PL7E//2o/t1f9H p79MMxqYKAghiBF3Okvh0m8cahPhVGSsWLMlB5KFT3d5RLq7z\nFdNaFQaRlXZdr7W9zusVsnjkoa2\nb5s0ccSbKNQUNzHAiRW8yJdUBZmzvznUzEQljA+kDrvwheSjO1v7Tjdq/dgA
"""


def test_drm_key_matches_worker_vector():
    # MD5("3135553") = 400a3784ab56654e66d49059640f82bf, folded with the
    # worker's EVEN/ODD constants.
    assert deezer_cli.drm_key("3135553").hex() == "653231393f3f7a6e672c733637693732"


def test_drm_key_accepts_int_and_str_equally():
    assert deezer_cli.drm_key(3135553) == deezer_cli.drm_key("3135553")


def test_drm_decrypt_real_ciphertext():
    enc = base64.b64decode(_FIXTURE_B64)
    assert len(enc) == 6144
    dec = deezer_cli.drm_decrypt(enc, "3135553")
    assert len(dec) == 6144
    # first 2048 bytes were encrypted -> now a valid MPEG-1 Layer III header
    # (128 kbps, 44.1 kHz)
    assert dec[:4] == b"\xff\xfb\x90\x00"
    # the other 4096 bytes of the group pass through untouched
    assert dec[2048:] == enc[2048:]


def test_drm_decrypt_roundtrip():
    key = deezer_cli.drm_key("12345")
    plain = os.urandom(6144 * 3 + 100)
    enc = bytearray(plain)
    # mirror the worker's strict `i + block < len` stripe exactly
    for i in range(0, len(enc) - 2048, 6144):
        c = Blowfish.new(key, Blowfish.MODE_CBC, iv=bytes(range(8)))
        enc[i:i + 2048] = c.encrypt(bytes(enc[i:i + 2048]))
    assert deezer_cli.drm_decrypt(bytes(enc), "12345") == plain


def test_drm_decrypt_short_buffer_is_noop():
    # the worker's loop condition (i + 2048 < len) leaves short buffers alone
    small = b"\x01" * 2048
    assert deezer_cli.drm_decrypt(small, "1") == small


# --- audio tagging ----------------------------------------------------------- #
def _parse_id3(tag):
    assert tag[:6] == b"ID3\x04\x00\x00"
    size = ((tag[6] << 21) | (tag[7] << 14) | (tag[8] << 7) | tag[9])
    assert size == len(tag) - 10
    frames, off = {}, 0
    body = tag[10:]
    while off < len(body):
        fid = body[off:off + 4].decode("ascii")
        flen = ((body[off + 4] << 21) | (body[off + 5] << 14)
                | (body[off + 6] << 7) | body[off + 7])
        frames[fid] = body[off + 10:off + 10 + flen]
        off += 10 + flen
    return frames


def test_id3v2_tag_structure_and_text():
    tag = deezer_cli._id3v2_tag("One More Time", "Daft Punk", "Discovery")
    frames = _parse_id3(tag)
    assert set(frames) == {"TIT2", "TPE1", "TALB"}
    for fid, expected in (("TIT2", "One More Time"), ("TPE1", "Daft Punk"),
                          ("TALB", "Discovery")):
        assert frames[fid][0] == 1  # 0x01 = UTF-16 with BOM (ID3v2 spec)
        assert frames[fid][1:].decode("utf-16") == expected


def test_id3v2_tag_handles_unicode_and_empty_fields():
    tag = deezer_cli._id3v2_tag("Foni — Α'", "Orfeas", "")
    frames = _parse_id3(tag)
    assert set(frames) == {"TIT2", "TPE1"}  # empty album omitted
    assert frames["TIT2"][1:].decode("utf-16") == "Foni — Α'"


def _parse_flac_blocks(data):
    assert data[:4] == b"fLaC"
    off, blocks = 4, []
    while True:
        hdr = data[off]
        ln = int.from_bytes(data[off + 1:off + 4], "big")
        blocks.append((hdr, data[off + 4:off + 4 + ln]))
        off += 4 + ln
        if hdr & 0x80:
            break
    return blocks


def _vorbis_comments(vc_data):
    vlen = struct.unpack("<I", vc_data[0:4])[0]
    n = struct.unpack("<I", vc_data[4 + vlen:8 + vlen])[0]
    p, out = 8 + vlen, []
    for _ in range(n):
        l = struct.unpack("<I", vc_data[p:p + 4])[0]
        p += 4
        out.append(vc_data[p:p + l].decode("utf-8"))
        p += l
    return out


def test_flac_tag_fills_existing_empty_comment_block():
    # Type-3 fallback: STREAMINFO (not last) + empty 594-byte type-3 block and
    # no type-4 block, so _flac_tag fills the type-3 one in place.
    si = b"\x00" * 34
    vc = b"\x00" * 594
    data = (b"fLaC" + bytes([0x00]) + len(si).to_bytes(3, "big") + si
            + bytes([0x83]) + len(vc).to_bytes(3, "big") + vc)
    out = deezer_cli._flac_tag(data, "One More Time", "Daft Punk", "Discovery")
    blocks = _parse_flac_blocks(out)
    assert [b[0] & 0x7F for b in blocks] == [0, 3]
    assert _vorbis_comments(blocks[1][1]) == [
        "TITLE=One More Time", "ARTIST=Daft Punk", "ALBUM=Discovery"]
    # audio data (after the metadata) is untouched
    assert out[len(data):] == b"" and len(out) == len(data)


def test_flac_tag_inserts_comment_block_when_missing():
    si = b"\x00" * 34
    data = b"fLaC" + bytes([0x80]) + len(si).to_bytes(3, "big") + si
    out = deezer_cli._flac_tag(data, "T", "A", "")
    blocks = _parse_flac_blocks(out)
    assert len(blocks) == 2 and (blocks[1][0] & 0x7F) == 4
    assert not (blocks[0][0] & 0x80)  # STREAMINFO no longer the last block
    assert _vorbis_comments(blocks[1][1]) == ["TITLE=T", "ARTIST=A"]


def test_flac_tag_fills_type4_block_deezer_layout():
    # Real Deezer layout: STREAMINFO + type-3 (zeros) + type-4 (vendor string,
    # 0 comments) + PADDING(last). The Vorbis comment lives in the type-4 block,
    # so _flac_tag must fill that one (not the all-zero type-3 block).
    si = b"\x00" * 34
    t3 = b"\x00" * 594
    vendor = b"reference libFLAC 1.3.1 20141125"
    t4 = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    pad = b"\x00" * 8192
    audio = b"\xff\xfb" + b"\x00" * 16
    data = (b"fLaC"
            + bytes([0x00]) + len(si).to_bytes(3, "big") + si
            + bytes([0x03]) + len(t3).to_bytes(3, "big") + t3
            + bytes([0x04]) + len(t4).to_bytes(3, "big") + t4
            + bytes([0x81]) + len(pad).to_bytes(3, "big") + pad
            + audio)
    out = deezer_cli._flac_tag(data, "One More Time", "Daft Punk", "Discovery")
    blocks = _parse_flac_blocks(out)
    # block types preserved: STREAMINFO, type-3, type-4, PADDING(last)
    assert [b[0] & 0x7F for b in blocks] == [0, 3, 4, 1]
    assert blocks[3][0] & 0x80  # PADDING still the last block
    # type-3 block untouched (still all zeros)
    assert blocks[1][1] == t3
    # type-4 block now carries our comments (original vendor cleared)
    assert _vorbis_comments(blocks[2][1]) == [
        "TITLE=One More Time", "ARTIST=Daft Punk", "ALBUM=Discovery"]
    # audio data (after the metadata) is preserved at the end, untouched
    assert out.endswith(audio)


def test_safe_filename_strips_unwanted_chars():
    assert deezer_cli._safe_filename('A/B: C *D* "E"') == "A B C D E"
    assert deezer_cli._safe_filename("  spaced   out  ") == "spaced out"
    assert deezer_cli._safe_filename("///") == "untitled"
