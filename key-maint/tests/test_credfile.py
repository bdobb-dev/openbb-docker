# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Parser must replicate docker compose v2.29 dotenv behavior as observed on the NAS
(2026-08-04 incident): leading whitespace after '=' is trimmed; an inline
' # comment' after a NON-empty value is stripped; a line whose value is only
whitespace followed by '#...' yields the '#...' text AS the value (the hazard
the repo convention now forbids, but the parser must still mirror compose)."""
import os
import stat

import pytest

from app.credfile import load, load_with_warnings, parse_text, set_value


class TestParseText:
    def test_plain_pair(self):
        assert parse_text("FMP_API_KEY=abc123\n") == {"FMP_API_KEY": "abc123"}

    def test_empty_value(self):
        assert parse_text("FMP_API_KEY=\n") == {"FMP_API_KEY": ""}

    def test_full_line_comment_and_blank_skipped(self):
        assert parse_text("# a comment\n\nA=1\n") == {"A": "1"}

    def test_inline_comment_after_value_stripped(self):
        assert parse_text("EODHD_API_KEY=tok123  # eodhd.com token\n") == {
            "EODHD_API_KEY": "tok123"
        }

    def test_empty_value_with_inline_comment_becomes_value(self):
        # The compose hazard: comment text leaks in as the value.
        out = parse_text("EIA_API_KEY=                 # eia.gov/opendata\n")
        assert out == {"EIA_API_KEY": "# eia.gov/opendata"}

    def test_leading_whitespace_trimmed_trailing_too(self):
        assert parse_text("A=  v1  \n") == {"A": "v1"}

    def test_malformed_line_skipped(self):
        assert parse_text("NOEQUALSSIGN\nA=1\n") == {"A": "1"}

    def test_last_duplicate_wins(self):
        assert parse_text("A=1\nA=2\n") == {"A": "2"}


class TestLoad:
    def test_missing_file_returns_none(self, tmp_path):
        assert load(str(tmp_path / "nope.env")) is None

    def test_reads_file(self, tmp_path):
        p = tmp_path / "c.env"
        p.write_text("A=1\n")
        assert load(str(p)) == {"A": "1"}


class TestLoadWithWarnings:
    def test_malformed_line_reported_and_rest_parsed(self, tmp_path):
        p = tmp_path / "c.env"
        p.write_text("A=1\nNOEQUALSSIGN\nB=2\n")
        values, warnings = load_with_warnings(str(p))
        assert values == {"A": "1", "B": "2"}
        assert warnings == ["line 2"]


class TestSetValue:
    def test_updates_in_place_preserving_everything_else(self, tmp_path):
        p = tmp_path / "credentials.env"
        p.write_text(
            "# leading comment\n"
            "FMP_API_KEY=old\n"
            "\n"
            "# another comment\n"
            "EODHD_API_KEY=keepme\n"
        )
        set_value(str(p), "FMP_API_KEY", "new")
        text = p.read_text()
        assert "FMP_API_KEY=new" in text
        assert "# leading comment" in text
        assert "# another comment" in text
        assert "EODHD_API_KEY=keepme" in text
        # order preserved: FMP still before EODHD
        assert text.index("FMP_API_KEY") < text.index("EODHD_API_KEY")

    def test_appends_when_absent(self, tmp_path):
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        set_value(str(p), "EODHD_API_KEY", "fresh")
        assert "EODHD_API_KEY=fresh" in p.read_text()
        assert "FMP_API_KEY=old" in p.read_text()

    def test_setting_empty_clears_the_value(self, tmp_path):
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        set_value(str(p), "FMP_API_KEY", "")
        assert parse_text(p.read_text())["FMP_API_KEY"] == ""

    def test_round_trips_through_the_parser(self, tmp_path):
        # The writer must agree with the reader, or the widget shows one
        # thing and the next container restart loads another.
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        set_value(str(p), "FMP_API_KEY", "abc123")
        assert parse_text(p.read_text())["FMP_API_KEY"] == "abc123"

    def test_no_temp_file_left_behind(self, tmp_path):
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        set_value(str(p), "FMP_API_KEY", "new")
        assert [f.name for f in tmp_path.iterdir()] == ["credentials.env"]

    def test_rejects_value_containing_newline(self, tmp_path):
        # A newline in the value would inject an extra line into the file,
        # letting a caller define an arbitrary second variable via a single
        # set_value call. The dotenv format has no escape for this, so the
        # writer refuses rather than silently truncating or corrupting the
        # file.
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        with pytest.raises(ValueError):
            set_value(str(p), "FMP_API_KEY", "abc\ndef")
        # Rejected write must not touch the file or leave a temp file.
        assert p.read_text() == "FMP_API_KEY=old\n"
        assert [f.name for f in tmp_path.iterdir()] == ["credentials.env"]

    @pytest.mark.parametrize(
        "char",
        [
            "\x0b",  # vertical tab
            "\x1c",  # file separator
            "\x85",  # NEL
        ],
        ids=["vertical-tab", "file-separator", "nel"],
    )
    def test_rejects_value_containing_other_splitlines_breaks(self, tmp_path, char):
        # str.splitlines() -- used by both parse_text and set_value's own
        # re-read -- treats far more than '\n'/'\r' as a line break: \v
        # (\x0b), \f (\x0c), \x1c, \x1d, \x1e, \x85, and the unicode line
        # and paragraph separators \u2028/\u2029. Checking only for
        # '\n'/'\r' left every one of those characters as a working
        # line-injection vector: a value like "abc\x0bINJECTED=pwned"
        # would sail through the old check and parse_text would then see
        # two variables.
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        with pytest.raises(ValueError):
            set_value(str(p), "FMP_API_KEY", f"abc{char}INJECTED=pwned")
        assert p.read_text() == "FMP_API_KEY=old\n"
        assert [f.name for f in tmp_path.iterdir()] == ["credentials.env"]

    def test_does_not_over_reject_value_that_merely_contains_key_equals(self, tmp_path):
        # The check must key off actual line breaks, not off the shape of
        # the value -- a value that happens to contain something that looks
        # like another KEY=value assignment, with no break character, is
        # legitimate and must round-trip untouched.
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        value = "prefix INJECTED=pwned suffix"
        set_value(str(p), "FMP_API_KEY", value)
        assert parse_text(p.read_text())["FMP_API_KEY"] == value

    def test_empty_value_still_accepted(self, tmp_path):
        # ''.splitlines() == [], so the "no line break" check must not
        # mistake an empty value for a rejected one.
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        set_value(str(p), "FMP_API_KEY", "")
        assert parse_text(p.read_text())["FMP_API_KEY"] == ""

    def test_preserves_existing_file_mode(self, tmp_path):
        # tempfile.mkstemp creates the temp file at 0600, and os.replace is
        # a rename -- without carrying the original mode across, a 0644
        # credentials.env silently narrows to 0600 on the very first write.
        # If the API reads the file as a different user/group than the
        # writer, that breaks credential loading with no error anywhere.
        p = tmp_path / "credentials.env"
        p.write_text("FMP_API_KEY=old\n")
        os.chmod(p, 0o644)
        set_value(str(p), "FMP_API_KEY", "new")
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o644

    def test_creates_with_restrictive_default_mode_when_file_absent(self, tmp_path):
        # First write, nothing to inherit from: mkstemp's restrictive 0600
        # default is the right call for a brand-new credentials file.
        p = tmp_path / "credentials.env"
        set_value(str(p), "FMP_API_KEY", "new")
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
