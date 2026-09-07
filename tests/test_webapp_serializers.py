# tests/test_webapp_serializers.py
"""Template filters — the small formatting helpers every page leans on."""
from src.webapp.serializers import format_cs_date


class TestCsDate:
    def test_iso_date_becomes_czech_day_month_year(self):
        assert format_cs_date("2026-09-04") == "4. 9. 2026"

    def test_missing_value_is_a_dash(self):
        assert format_cs_date(None) == "–"
        assert format_cs_date("") == "–"

    def test_an_unparseable_value_passes_through(self):
        """A stray label must not blank the card — show what we were given."""
        assert format_cs_date("n/a") == "n/a"


class TestAssetUrl:
    """Static assets are linked with the file's modification time, so a
    browser that cached style.css picks the new one up after a git pull —
    the templates reload live, and a stale stylesheet under fresh markup
    breaks the layout without restarting anything."""

    def test_the_url_carries_the_files_mtime(self, tmp_path):
        from src.webapp.serializers import asset_url
        import os
        css = tmp_path / "style.css"
        css.write_text("a{}")
        os.utime(css, (1_700_000_000, 1_700_000_000))
        assert asset_url(tmp_path, "style.css") == "/static/style.css?v=1700000000"

    def test_a_changed_file_gets_a_new_url(self, tmp_path):
        from src.webapp.serializers import asset_url
        import os
        css = tmp_path / "style.css"
        css.write_text("a{}")
        os.utime(css, (1_700_000_000, 1_700_000_000))
        before = asset_url(tmp_path, "style.css")
        css.write_text("a{color:red}")
        os.utime(css, (1_700_000_500, 1_700_000_500))
        assert asset_url(tmp_path, "style.css") != before

    def test_a_missing_file_still_links(self, tmp_path):
        """A typo in a name must not take the whole page down."""
        from src.webapp.serializers import asset_url
        assert asset_url(tmp_path, "nope.js") == "/static/nope.js?v=0"
