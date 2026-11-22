"""redact_text's literal prefilter must be byte-identical to a full scan of every pattern."""
from __future__ import annotations

import random

import pytest

from herdr_engineering.control_plane import redact as R
from herdr_engineering.control_plane.redact import redact_text


def reference(text: str) -> str:
    """Full scan: private-key pass, then every pattern sequentially (the pre-prefilter implementation)."""
    if not text:
        return text
    out = R._redact_private_keys(text)
    for pattern in R._PATTERNS:
        out = pattern.sub(R._REDACTED, out)
    return out


EXISTING_FIXTURES = [
    "api_key=sk-abcdef123456 and password='hunter2' and token abc.def.ghi",
    "dsn=postgresql://u:hunter2@db:5432/herdr client_secret=abc123def456 "
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    "task-s6lease", "disk-image-1 and risk-profile-high", "sk-abcDEF123456", "key=sk-abcDEF123456",
    "OPENAI_API_KEY_sk-abcDEF123456", '"sk-proj-abcDEF123456"', "(sk-abc123)",
    "\x1b[32m" + "sk-" + "A" * 40, "api_key%3D" + "sk-" + "A" * 40, "task-sk-ab", "\x1b[32mtask-sk-ab\x1b[0m",
    "a -----BEGIN PRIVATE KEY-----\nxx\n-----END PRIVATE KEY----- b", "-----BEGIN RSA PRIVATE KEY-----" * 5,
    "", "plain prose with nothing secret", "author=alice",
]

SHAPES = [
    "sk-abcdefghijklmnopqrstuvwxyz0123", "sk-ab12",
    "AKIAABCDEFGHIJKLMNOP", "ASIAABCDEFGHIJKLMNOP",
    "aws_secret_access_key = " + "A" * 40, "AWS SECRET KEY: " + "b" * 35,
    "ghp_" + "a" * 30, "gho_" + "a" * 30, "ghu_" + "a" * 30, "ghs_" + "a" * 30, "ghr_" + "a" * 30,
    "github_pat_" + "a" * 30, "xoxb-1234567890-abcdef", "xoxp-1234567890-abcdef", "Bearer abc.def-ghi",
    "bearer   TOKEN123==", "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM", "password=hunter2", "passwd: 'x'", "secret=abcdefgh1",
    "token abc.def.ghi", "apikey=abcdefghij", "api-key: abcdefghij", "private_key=abcdefghij", "pwd=abcdefghi",
    "pass=abcdefghi", "x-api-key: zzz", "authorization: Basic abc", "auth_token=abc", "access-token abcdefgh12",
    "refresh_token=abc", "client_secret=abc", "postgres://u:p@h/db", "REDIS://u:p@h", "s3://k:s@bucket",
    "mongodb+srv://u:p@h", "amqps://u:p@h",
]
PROSE = ["", "see the log: ", "\x1b[32m", "x=", "%3D", "(", '"', "id-"]
SUFFIX = ["", " end", "\x1b[0m", "\n", ",;"]


def _corpus() -> list[str]:
    out = []
    for s in SHAPES:
        for variant in (s, s.upper(), s.lower(), s.title(), s.swapcase()):
            for pre in PROSE:
                for suf in SUFFIX:
                    out.append(pre + variant + suf)
    return out


@pytest.mark.parametrize("text", EXISTING_FIXTURES)
def test_existing_fixtures_match_reference(text):
    assert redact_text(text) == reference(text)


def test_each_secret_shape_in_prose_and_case_variants_matches_reference():
    corpus = _corpus()
    redacted = 0
    for text in corpus:
        assert redact_text(text) == reference(text), text
        redacted += reference(text) != text
    assert redacted > 500  # the corpus really exercises the patterns


@pytest.mark.parametrize("text", [
    "ſecret=abcdefgh123", "api_Key=abcdefghij123", "aKey=abcdefghij", "toKen abcdefghij",
    "paſſword=hunter22", "naïve password=hunter2 ok", "日本語 token abcdefgh12", "AWS_SECRET_ACCESS_KEY=" + "A" * 40,
    "Key: x", "ſk-abcdefghijklmnopqrstuvwxyz", "ghp_" + "é" + "a" * 30,
])
def test_non_ascii_matches_reference(text):
    assert redact_text(text) == reference(text)


def test_kelvin_and_long_s_actually_redact():
    assert "abcdefgh123" not in redact_text("ſecret=abcdefgh123")
    assert "abcdefghij" not in redact_text("api_Key=abcdefghij")


FRAGMENTS = [
    "sk-", "AKIA", "asia", "aws", "_secret", "_access", "_key", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "xox", "xoxb-", "Bearer ", "bearer", "eyJ", "pass", "password", "passwd", "secret", "token", "api", "api_key",
    "api-key", "apikey", "private", "private_key", "pwd", "auth", "authorization", "x-api-key", "auth_token",
    "access_token", "://", "postgres", "redis", "s3", "=", ":", " ", "\"", "'", ".", "-", "_", "abcdefgh", "AB12CD34EF56GH78IJ90",
    "[REDACTED]", "\x1b[32m", "%3D", "\n", "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----", "@", "u:p", "é", "K", "ſ",
]


def test_seeded_fuzz_matches_reference():
    rng = random.Random(20261121)
    hits = 0
    for _ in range(6000):
        text = "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 14)))
        if rng.random() < 0.3:
            text = text.upper() if rng.random() < 0.5 else text.swapcase()
        expected = reference(text)
        assert redact_text(text) == expected, repr(text)
        hits += expected != text
    assert hits > 500


def test_literal_table_lines_up_with_patterns():
    assert len(R._LITERALS) == len(R._PATTERNS)


def test_inserted_redaction_marker_cannot_create_a_later_match():
    # the prefilter looks at the pre-substitution text; "[REDACTED]" holds none of the literals
    marker = R._REDACTED.lower()
    for table in R._LITERALS:
        assert table
        for literal in table:
            assert literal not in marker
