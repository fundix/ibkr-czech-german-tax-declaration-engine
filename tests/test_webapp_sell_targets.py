# tests/test_webapp_sell_targets.py
"""Sell-target ladder (prodejní zóny) — the store and its page.

No monkeypatching of paths: the store location derives from the injected
``data_dir``, so the ``service(tmp_path)`` fixture isolates tests for free.
That is deliberate — see ``RunService.sell_targets_path``.
"""
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.webapp.app import create_app
from src.webapp.services import MAX_ZONES_PER_SYMBOL, RunService, _parse_decimal_cz


@pytest.fixture
def service(tmp_path):
    svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    yield svc
    svc.runner.shutdown(wait=False)


@pytest.fixture
def client(service):
    """Function-scoped: the shared client in test_webapp_routes.py is module
    scoped, so POSTs there would leak state between these tests."""
    with TestClient(create_app(services=service)) as c:
        yield c


class TestDecimalParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("70", "70"),
        ("70,5", "70.5"),
        ("1 234,50", "1234.50"),          # plain space
        ("1 234,50", "1234.50"),     # no-break space
        ("1 234,50", "1234.50"),     # narrow no-break space
        ("1 234.50", "1234.50"),
        ("70,00 Kč", "70.00"),
        ("  12,5  ", "12.5"),
    ])
    def test_czech_forms_accepted(self, raw, expected):
        assert _parse_decimal_cz(raw, "cena") == Decimal(expected)

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "1,2,3", "--5"])
    def test_junk_rejected_with_czech_message(self, raw):
        with pytest.raises(ValueError) as exc:
            _parse_decimal_cz(raw, "cílová cena")
        assert "cena" in str(exc.value).lower()

    def test_infinity_rejected(self):
        with pytest.raises(ValueError):
            _parse_decimal_cz("Infinity", "cena")


class TestStore:
    def test_missing_store_is_empty_not_an_error(self, service):
        assert service.load_sell_targets()["targets"] == {}
        assert service.list_sell_targets() == []

    def test_round_trip_and_file_shape(self, service):
        zid = service.save_sell_zone("SOFI", "70", "50", note="první tranše")
        [row] = service.list_sell_targets()
        assert row["symbol"] == "SOFI"
        [zone] = row["zones"]
        assert zone["id"] == zid
        # Money is persisted as a STRING, never a float
        assert zone["price"] == "70" and isinstance(zone["price"], str)
        assert zone["quantity"] == "50"
        assert zone["note"] == "první tranše"
        assert zone["reached_at"] is None and zone["done_at"] is None

        raw = service.sell_targets_path.read_text(encoding="utf-8")
        assert "první tranše" in raw          # ensure_ascii=False
        assert '\n  "version"' in raw          # indent=2
        assert json.loads(raw)["version"] == 1

    def test_ladder_is_kept_price_ascending(self, service):
        service.save_sell_zone("SOFI", "85", "20")
        service.save_sell_zone("SOFI", "70", "50")
        service.save_sell_zone("SOFI", "1 234,50", "5")
        [row] = service.list_sell_targets()
        assert [z["price"] for z in row["zones"]] == ["70", "85", "1234.50"]

    def test_czech_comma_survives_the_form_path(self, service):
        service.save_sell_zone("TUI1", "1 234,50", "12,5")
        [row] = service.list_sell_targets()
        assert row["zones"][0]["price"] == "1234.50"
        assert row["zones"][0]["quantity"] == "12.5"

    @pytest.mark.parametrize("price", ["0", "-5", "abc", ""])
    def test_bad_price_rejected(self, service, price):
        with pytest.raises(ValueError):
            service.save_sell_zone("SOFI", price, "10")

    @pytest.mark.parametrize("qty", ["0", "-1", "abc"])
    def test_bad_quantity_rejected(self, service, qty):
        with pytest.raises(ValueError):
            service.save_sell_zone("SOFI", "70", qty)

    def test_empty_symbol_rejected(self, service):
        with pytest.raises(ValueError, match="symbol"):
            service.save_sell_zone("   ", "70", "10")

    def test_duplicate_price_in_one_ladder_rejected(self, service):
        service.save_sell_zone("SOFI", "70", "50")
        with pytest.raises(ValueError, match="už existuje"):
            service.save_sell_zone("SOFI", "70", "10")

    def test_same_price_on_a_different_symbol_is_fine(self, service):
        service.save_sell_zone("SOFI", "70", "50")
        service.save_sell_zone("PYPL", "70", "10")
        assert len(service.list_sell_targets()) == 2

    def test_symbol_case_and_spaces_preserved(self, service):
        # No .upper(): option keys carry spaces and case.
        service.save_sell_zone("  C TUI  20260619 9 M  ", "70", "1")
        [row] = service.list_sell_targets()
        assert row["symbol"] == "C TUI  20260619 9 M"

    def test_unknown_symbol_allowed(self, service):
        # A target may precede the purchase — never gate on the run.
        service.save_sell_zone("NOTYETBOUGHT", "42", "100")
        assert service.list_sell_targets()[0]["symbol"] == "NOTYETBOUGHT"

    def test_edit_updates_in_place_and_reorders(self, service):
        service.save_sell_zone("SOFI", "70", "50")
        zid = service.save_sell_zone("SOFI", "85", "20")
        service.save_sell_zone("SOFI", "60", "20", zone_id=zid)
        [row] = service.list_sell_targets()
        assert [z["price"] for z in row["zones"]] == ["60", "70"]
        assert len(row["zones"]) == 2          # edited, not appended

    def test_zone_cap_enforced(self, service):
        for i in range(10):
            service.save_sell_zone("SOFI", str(100 + i), "1")
        with pytest.raises(ValueError, match="Maximálně 10"):
            service.save_sell_zone("SOFI", "999", "1")

    def test_delete_zone_and_last_zone_drops_the_symbol(self, service):
        z1 = service.save_sell_zone("SOFI", "70", "50")
        service.save_sell_zone("SOFI", "85", "20")
        service.delete_sell_zone("SOFI", z1)
        assert [z["price"] for z in service.list_sell_targets()[0]["zones"]] == ["85"]
        service.delete_sell_zone("SOFI", service.list_sell_targets()[0]["zones"][0]["id"])
        assert service.list_sell_targets() == []

    def test_delete_whole_symbol(self, service):
        service.save_sell_zone("SOFI", "70", "50")
        service.save_sell_zone("PYPL", "60", "10")
        service.delete_sell_target("SOFI")
        assert [r["symbol"] for r in service.list_sell_targets()] == ["PYPL"]

    def test_corrupt_store_degrades_instead_of_raising(self, service):
        service.sell_targets_path.parent.mkdir(parents=True, exist_ok=True)
        service.sell_targets_path.write_text("{ not json", encoding="utf-8")
        assert service.load_sell_targets()["targets"] == {}
        assert service.list_sell_targets() == []
        # and a save on top of the corrupt file still works
        service.save_sell_zone("SOFI", "70", "50")
        assert len(service.list_sell_targets()) == 1

    def test_store_with_wrong_shape_degrades(self, service):
        service.sell_targets_path.parent.mkdir(parents=True, exist_ok=True)
        service.sell_targets_path.write_text('["nope"]', encoding="utf-8")
        assert service.load_sell_targets()["targets"] == {}


class TestZoneState:
    def test_done_and_undone(self, service):
        zid = service.save_sell_zone("SOFI", "70", "50")
        service.set_zone_state("SOFI", zid, "done")
        [row] = service.list_sell_targets()
        assert row["zones"][0]["done_at"] is not None
        assert row["open_zones"] == []          # done leaves the ladder

        service.set_zone_state("SOFI", zid, "undone")
        [row] = service.list_sell_targets()
        assert row["zones"][0]["done_at"] is None
        assert len(row["open_zones"]) == 1

    def test_rearm_clears_the_latch(self, service):
        zid = service.save_sell_zone("SOFI", "70", "50")
        # Simulate what the live pass will do in a later phase
        service._set_zone_state("SOFI", zid, "acknowledge")
        assert service.list_sell_targets()[0]["zones"][0]["acknowledged_at"]
        service.set_zone_state("SOFI", zid, "rearm")
        zone = service.list_sell_targets()[0]["zones"][0]
        assert zone["acknowledged_at"] is None and zone["reached_at"] is None

    def test_unknown_action_rejected(self, service):
        zid = service.save_sell_zone("SOFI", "70", "50")
        with pytest.raises(ValueError, match="Neznámá akce"):
            service.set_zone_state("SOFI", zid, "explode")

    def test_unknown_zone_rejected(self, service):
        service.save_sell_zone("SOFI", "70", "50")
        with pytest.raises(ValueError, match="neexistuje"):
            service.set_zone_state("SOFI", "nope", "done")


class TestRoutes:
    def test_page_renders_empty_state(self, client):
        r = client.get("/targets")
        assert r.status_code == 200
        assert "Prodejní zóny" in r.text
        # The 3-year correction must be visible, not silent
        assert "3 roky" in r.text

    def test_save_zone_via_form_with_czech_comma(self, client, service):
        r = client.post("/targets/zone",
                        data={"symbol": "SOFI", "price": "1 234,50",
                              "quantity": "50", "note": "první"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert service.list_sell_targets()[0]["zones"][0]["price"] == "1234.50"
        assert "SOFI" in client.get("/targets").text

    def test_bad_input_redirects_with_a_czech_error(self, client, service):
        r = client.post("/targets/zone",
                        data={"symbol": "SOFI", "price": "0", "quantity": "50"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert "error=" in r.headers["location"]
        assert service.list_sell_targets() == []      # nothing written

    def test_delete_zone_via_form(self, client, service):
        zid = service.save_sell_zone("SOFI", "70", "50")
        r = client.post("/targets/zone/delete",
                        data={"symbol": "SOFI", "zone_id": zid},
                        follow_redirects=False)
        assert r.status_code == 303
        assert service.list_sell_targets() == []

    def test_zone_state_via_form(self, client, service):
        zid = service.save_sell_zone("SOFI", "70", "50")
        r = client.post("/targets/zone/state",
                        data={"symbol": "SOFI", "zone_id": zid, "action": "done"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert service.list_sell_targets()[0]["zones"][0]["done_at"]

    def test_delete_symbol_via_form(self, client, service):
        service.save_sell_zone("SOFI", "70", "50")
        service.save_sell_zone("SOFI", "85", "20")
        r = client.post("/targets/symbol/delete", data={"symbol": "SOFI"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert service.list_sell_targets() == []

    def test_ladder_renders_all_zones(self, client, service):
        service.save_sell_zone("SOFI", "70", "50")
        service.save_sell_zone("SOFI", "85", "20")
        text = client.get("/targets").text
        assert "70" in text and "85" in text
        assert 'id="s-SOFI"' in text          # anchor for the flash redirect


def _pos(symbol, description, category="STOCK"):
    return {"symbol": symbol, "description": description, "category": category,
            "quantity_long": "100"}


# The real portfolio shape this feature has to cope with: the user's sheet has
# company NAMES, often truncated by the spreadsheet column width.
REAL_POSITIONS = [
    _pos("PYPL", "PAYPAL HOLDINGS INC"),
    _pos("TUI1", "TUI AG"),
    _pos("BABA", "ALIBABA GROUP HOLDING-SP ADR"),
    _pos("FLXK", "FRK FTSE KOREA UCITS ETF"),
    _pos("KWEB", "KRANESHARES CSI CHINA INTRNT"),
    _pos("NU", "NU HOLDINGS LTD/CAYMAN ISL-A"),
    _pos("EVO", "EVOLUTION AB"),
    _pos("S", "SENTINELONE INC -CLASS A"),
    _pos("BY6", "BYD CO LTD-H"),
    _pos("BYDDY", "BYD CO LTD-UNSPONSORED ADR"),
    _pos("C TUI  20260619 9 M", "TUI1 19JUN26 9 C", "OPTION"),
]


class TestSymbolResolution:
    def test_exact_symbol_wins(self, service):
        r = service.resolve_target_symbol("PYPL", REAL_POSITIONS)
        assert r["symbol"] == "PYPL" and r["how"] == "symbol"

    def test_symbol_is_case_insensitive(self, service):
        assert service.resolve_target_symbol("pypl", REAL_POSITIONS)["symbol"] == "PYPL"

    @pytest.mark.parametrize("name,expected", [
        ("PayPal Holdings Inc", "PYPL"),           # exact description
        ("TUI AG", "TUI1"),
        ("Nu Holdings Ltd.", "NU"),                # trailing punctuation
        ("Evolution AB (publ)", "EVO"),            # sheet has EXTRA tokens
        ("SentinelOne Inc", "S"),                  # single-letter symbol
        ("Alibaba Group Holding Ltd - AD", "BABA"),          # truncated + fuzzy
        ("Franklin FTSE Korea UCITS ET", "FLXK"),            # abbreviation
        ("KraneShares CSI China Interne", "KWEB"),           # truncated
    ])
    def test_company_names_resolve(self, service, name, expected):
        assert service.resolve_target_symbol(name, REAL_POSITIONS)["symbol"] == expected

    def test_dangerous_near_miss_is_refused(self, service):
        # "Duolingo Inc" scores ~0.58 against PAYPAL HOLDINGS INC. A wrong
        # symbol in a sell plan is worse than an unresolved row.
        r = service.resolve_target_symbol("Duolingo Inc", REAL_POSITIONS)
        assert r["symbol"] is None
        assert r["candidates"]                      # offers a pick list

    def test_genuinely_ambiguous_stays_manual(self, service):
        # Two BYD listings — the user must choose.
        r = service.resolve_target_symbol("BYD Company Limited", REAL_POSITIONS)
        assert r["symbol"] is None

    def test_unknown_name_is_unresolved_not_guessed(self, service):
        r = service.resolve_target_symbol("ZLATO - 4GLD", REAL_POSITIONS)
        assert r["symbol"] is None

    def test_options_are_never_a_target(self, service):
        # _sellable_positions filters them out before resolution.
        assert all(p["category"] != "OPTION"
                   for p in service._sellable_positions({"positions": REAL_POSITIONS}))

    def test_empty_value(self, service):
        assert service.resolve_target_symbol("  ", REAL_POSITIONS)["symbol"] is None

    def test_no_positions_does_not_crash(self, service):
        assert service.resolve_target_symbol("PYPL", [])["symbol"] is None


class TestImportParsing:
    HIS_SHEET = "\n".join([
        "k 2Y\tTitul\tDrženo v ks\tPříští sell zóna\t% k prodejní zóně",
        "22%\tPayPal Holdings Inc\t190\t70\t18,76%",
        "68%\tTUI AG\t200\t10\t29,20%",
        "70%\tAlibaba Group Holding Ltd - AD\t10\t130\t2,69%",
    ])

    def test_his_real_layout(self, service):
        out = service.parse_sell_targets(self.HIS_SHEET, REAL_POSITIONS)
        assert out["header_used"] is True
        assert [r["symbol"] for r in out["rows"]] == ["PYPL", "TUI1", "BABA"]
        # "Příští sell zóna" is the price; "% k prodejní zóně" must NOT win it
        assert [r["price"] for r in out["rows"]] == ["70", "10", "130"]
        assert [r["quantity"] for r in out["rows"]] == ["190", "200", "10"]

    def test_percent_column_never_taken_as_price(self, service):
        text = "Titul\t% k prodejní zóně\tPříští sell zóna\tv ks\nTUI AG\t29,20%\t10\t200"
        [row] = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert row["price"] == "10" and row["quantity"] == "200"

    def test_semicolon_delimiter(self, service):
        text = "Titul;Cena;Kusů\nTUI AG;10;200"
        [row] = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert row["symbol"] == "TUI1" and row["price"] == "10"

    def test_comma_is_never_a_delimiter(self, service):
        # A Czech decimal must survive: "1 234,50" is ONE value, not two columns.
        text = "PYPL\t1 234,50\t10"
        [row] = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert row["price"] == "1234.50"

    def test_positional_without_header(self, service):
        text = "PYPL\t70\t190\nTUI1\t10\t200"
        rows = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert [(r["symbol"], r["price"], r["quantity"]) for r in rows] == [
            ("PYPL", "70", "190"), ("TUI1", "10", "200")]

    def test_bad_rows_are_reported_not_dropped(self, service):
        text = ("Titul\tPříští sell zóna\tv ks\n"
                "PayPal Holdings Inc\t70\t190\n"
                "Duolingo Inc\t\t---\n"          # empty price, junk qty
                "TUI AG\tabc\t200\n")            # unparseable price
        out = service.parse_sell_targets(text, REAL_POSITIONS)
        assert len(out["rows"]) == 1
        assert len(out["skipped"]) == 2
        assert {s["line"] for s in out["skipped"]} == {3, 4}
        assert all(s["error"] for s in out["skipped"])

    def test_unresolved_row_is_kept_for_manual_pick(self, service):
        text = "Titul\tCena\tKusů\nZLATO - 4GLD\t160\t100"
        [row] = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert row["symbol"] is None and row["raw_symbol"] == "ZLATO - 4GLD"

    def test_repeated_symbol_becomes_two_zones(self, service):
        text = "PYPL\t70\t40\nPYPL\t85\t30"
        rows = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert [r["price"] for r in rows] == ["70", "85"]

    def test_blank_lines_ignored(self, service):
        text = "\n\nPYPL\t70\t40\n\n"
        assert len(service.parse_sell_targets(text, REAL_POSITIONS)["rows"]) == 1

    def test_too_many_lines_rejected(self, service):
        text = "\n".join(f"PYPL\t{i}\t1" for i in range(1, 700))
        with pytest.raises(ValueError, match="Příliš mnoho řádků"):
            service.parse_sell_targets(text, REAL_POSITIONS)

    def test_header_without_quantity_is_valid(self, service):
        # Only the price matters — his sheet's share counts are unreliable.
        text = "Titul\tPříští sell zóna\nPayPal Holdings Inc\t70"
        [row] = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert row["symbol"] == "PYPL" and row["price"] == "70"
        assert row["quantity"] is None

    def test_header_missing_the_price_reports_it(self, service):
        text = "Titul\tDrženo v ks\nPayPal Holdings Inc\t190"
        out = service.parse_sell_targets(text, REAL_POSITIONS)
        assert out["rows"] == []
        assert "chybí sloupec" in out["skipped"][0]["error"].lower()

    def test_unreadable_quantity_keeps_the_price(self, service):
        # "---" in the quantity column must not cost him the sell price.
        text = "Titul\tPříští sell zóna\tDrženo v ks\nPayPal Holdings Inc\t70\t---"
        out = service.parse_sell_targets(text, REAL_POSITIONS)
        assert out["skipped"] == []
        [row] = out["rows"]
        assert row["price"] == "70" and row["quantity"] is None
        assert "nepřečten" in row["quantity_note"]

    def test_two_column_paste_without_header(self, service):
        text = "PYPL\t70\nTUI1\t10"
        rows = service.parse_sell_targets(text, REAL_POSITIONS)["rows"]
        assert [(r["symbol"], r["price"], r["quantity"]) for r in rows] == [
            ("PYPL", "70", None), ("TUI1", "10", None)]

    def test_single_column_is_still_an_error(self, service):
        out = service.parse_sell_targets("PYPL", REAL_POSITIONS)
        assert out["rows"] == [] and "2 sloupce" in out["skipped"][0]["error"]

    def test_parse_writes_nothing(self, service):
        service.parse_sell_targets(self.HIS_SHEET, REAL_POSITIONS)
        assert service.list_sell_targets() == []


class TestImportApply:
    def test_import_creates_ladders(self, service):
        rows = [{"symbol": "PYPL", "price": "70", "quantity": "40"},
                {"symbol": "PYPL", "price": "85", "quantity": "30"},
                {"symbol": "TUI1", "price": "10", "quantity": "200"}]
        res = service.import_sell_targets(rows)
        assert res["imported"] == 3 and res["symbols"] == ["PYPL", "TUI1"]
        stored = {r["symbol"]: [z["price"] for z in r["zones"]]
                  for r in service.list_sell_targets()}
        assert stored == {"PYPL": ["70", "85"], "TUI1": ["10"]}

    def test_rows_without_a_symbol_are_skipped(self, service):
        rows = [{"symbol": "", "price": "1", "quantity": "1"},
                {"symbol": "PYPL", "price": "70", "quantity": "40"}]
        assert service.import_sell_targets(rows)["imported"] == 1

    def test_nothing_selected_raises(self, service):
        with pytest.raises(ValueError, match="Není co importovat"):
            service.import_sell_targets([{"symbol": "", "price": "1", "quantity": "1"}])

    def test_reimport_does_not_duplicate(self, service):
        rows = [{"symbol": "PYPL", "price": "70", "quantity": "40"}]
        service.import_sell_targets(rows)
        service.import_sell_targets(rows)
        assert [z["price"] for z in service.list_sell_targets()[0]["zones"]] == ["70"]

    def test_reimport_replaces_open_zones_but_keeps_sold_ones(self, service):
        zid = service.save_sell_zone("PYPL", "60", "10")
        service.set_zone_state("PYPL", zid, "done")
        service.save_sell_zone("PYPL", "65", "10")          # open, will be replaced
        service.import_sell_targets([{"symbol": "PYPL", "price": "70", "quantity": "40"}])
        zones = service.list_sell_targets()[0]["zones"]
        assert [z["price"] for z in zones] == ["60", "70"]
        assert [z["price"] for z in zones if z["done_at"]] == ["60"]

    def test_import_leaves_other_symbols_alone(self, service):
        service.save_sell_zone("TUI1", "10", "200")
        service.import_sell_targets([{"symbol": "PYPL", "price": "70", "quantity": "40"}])
        assert {r["symbol"] for r in service.list_sell_targets()} == {"PYPL", "TUI1"}

    def test_replace_wipes_everything_first(self, service):
        service.save_sell_zone("TUI1", "10", "200")
        service.import_sell_targets([{"symbol": "PYPL", "price": "70", "quantity": "40"}],
                                    replace=True)
        assert [r["symbol"] for r in service.list_sell_targets()] == ["PYPL"]

    def test_czech_comma_survives_import(self, service):
        service.import_sell_targets([{"symbol": "TUI1", "price": "9,60", "quantity": "1 000"}])
        [zone] = service.list_sell_targets()[0]["zones"]
        assert zone["price"] == "9.60" and zone["quantity"] == "1000"

    def test_bad_value_aborts_the_whole_import(self, service):
        rows = [{"symbol": "PYPL", "price": "70", "quantity": "40"},
                {"symbol": "TUI1", "price": "abc", "quantity": "1"}]
        with pytest.raises(ValueError):
            service.import_sell_targets(rows)
        assert service.list_sell_targets() == []      # nothing half-written

    def test_zone_cap_respected_on_import(self, service):
        rows = [{"symbol": "PYPL", "price": str(100 + i), "quantity": "1"}
                for i in range(15)]
        service.import_sell_targets(rows)
        assert len(service.list_sell_targets()[0]["zones"]) == MAX_ZONES_PER_SYMBOL


class TestImportRoutes:
    def test_import_page_renders(self, client):
        r = client.get("/targets/import")
        assert r.status_code == 200 and "Vlož tabulku" in r.text

    def test_preview_reads_his_sheet_and_writes_nothing(self, client, service):
        r = client.post("/targets/import/preview",
                        data={"text": TestImportParsing.HIS_SHEET, "run_id": ""})
        assert r.status_code == 200
        assert "Náhled importu" in r.text
        assert service.list_sell_targets() == []

    def test_preview_of_junk_explains_itself(self, client):
        r = client.post("/targets/import/preview", data={"text": "   ", "run_id": ""})
        assert r.status_code == 200
        assert "tabulátor" in r.text

    def test_apply_stores_only_ticked_rows(self, client, service):
        r = client.post("/targets/import/apply", data={
            "symbol": ["PYPL", "TUI1"], "price": ["70", "10"],
            "quantity": ["40", "200"], "include": ["0"],
        }, follow_redirects=False)
        assert r.status_code == 303 and "imported=1" in r.headers["location"]
        assert [x["symbol"] for x in service.list_sell_targets()] == ["PYPL"]

    def test_apply_unticking_does_not_misalign_rows(self, client, service):
        # Only the middle row ticked — it must be the one that lands.
        client.post("/targets/import/apply", data={
            "symbol": ["AAA", "PYPL", "ZZZ"], "price": ["1", "70", "3"],
            "quantity": ["1", "40", "3"], "include": ["1"],
        }, follow_redirects=False)
        [row] = service.list_sell_targets()
        assert row["symbol"] == "PYPL"
        assert row["zones"][0]["price"] == "70" and row["zones"][0]["quantity"] == "40"

    def test_apply_with_nothing_ticked_redirects_with_error(self, client, service):
        r = client.post("/targets/import/apply", data={
            "symbol": ["PYPL"], "price": ["70"], "quantity": ["40"],
        }, follow_redirects=False)
        assert r.status_code == 303 and "error=" in r.headers["location"]
        assert service.list_sell_targets() == []


class TestOptionalQuantity:
    """The price is the point of a sell zone; the share count is advisory.

    The source spreadsheet's quantities are known to be unreliable, so a
    missing or unparseable count must never lose the price.
    """

    def test_zone_without_quantity(self, service):
        service.save_sell_zone("PYPL", "70")
        [zone] = service.list_sell_targets()[0]["zones"]
        assert zone["price"] == "70" and zone["quantity"] is None

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_quantity_stored_as_none(self, service, blank):
        service.save_sell_zone("PYPL", "70", blank)
        assert service.list_sell_targets()[0]["zones"][0]["quantity"] is None

    def test_explicit_quantity_still_works(self, service):
        service.save_sell_zone("PYPL", "70", "40")
        assert service.list_sell_targets()[0]["zones"][0]["quantity"] == "40"

    def test_junk_quantity_on_the_form_path_still_errors(self, service):
        # Typed by hand into the form, "abc" is a mistake worth reporting;
        # only the IMPORT path degrades it to "unspecified".
        with pytest.raises(ValueError):
            service.save_sell_zone("PYPL", "70", "abc")

    def test_negative_quantity_rejected(self, service):
        with pytest.raises(ValueError, match="kladný"):
            service.save_sell_zone("PYPL", "70", "-5")

    def test_import_without_quantities(self, service):
        service.import_sell_targets([
            {"symbol": "PYPL", "price": "70"},
            {"symbol": "TUI1", "price": "10", "quantity": ""},
        ])
        stored = {r["symbol"]: r["zones"][0]["quantity"]
                  for r in service.list_sell_targets()}
        assert stored == {"PYPL": None, "TUI1": None}

    def test_mixed_ladder(self, service):
        service.save_sell_zone("PYPL", "70", "40")
        service.save_sell_zone("PYPL", "85")
        zones = service.list_sell_targets()[0]["zones"]
        assert [z["quantity"] for z in zones] == ["40", None]

    def test_page_renders_a_quantityless_zone(self, client, service):
        service.save_sell_zone("PYPL", "70")
        r = client.get("/targets")
        assert r.status_code == 200 and "PYPL" in r.text

    def test_form_post_without_quantity(self, client, service):
        r = client.post("/targets/zone",
                        data={"symbol": "PYPL", "price": "70", "quantity": ""},
                        follow_redirects=False)
        assert r.status_code == 303
        assert service.list_sell_targets()[0]["zones"][0]["quantity"] is None

    def test_apply_route_accepts_blank_quantity(self, client, service):
        client.post("/targets/import/apply", data={
            "symbol": ["PYPL"], "price": ["70"], "quantity": [""], "include": ["0"],
        }, follow_redirects=False)
        assert service.list_sell_targets()[0]["zones"][0]["quantity"] is None


# --------------------------------------------------------------------------
# Phase 3: live overview — zones joined with quotes, lots and the time test
# --------------------------------------------------------------------------

class StubQuotes:
    def __init__(self, prices):
        self.prices = prices           # symbol -> (Decimal price, currency)

    def get_quote(self, symbol, currency):
        from types import SimpleNamespace
        hit = self.prices.get(symbol)
        if hit is None:
            return None
        return SimpleNamespace(ibkr_symbol=symbol, yahoo_symbol=symbol,
                               price=hit[0], currency=hit[1], fetched_at=0.0)


class StubConverter:
    """Fixed rates so CZK arithmetic is checkable by hand.

    Must return a record with ``converted_amount_czk`` — that is the contract
    ``RunService._to_czk`` relies on (a bare Decimal is swallowed as a failure).
    """
    RATES = {"USD": Decimal("20"), "EUR": Decimal("25"), "CZK": Decimal("1")}

    def convert_to_czk(self, amount, currency, on_date):
        from types import SimpleNamespace
        rate = self.RATES.get(currency)
        if rate is None:
            return None
        return SimpleNamespace(converted_amount_czk=Decimal(str(amount)) * rate)


LADDER_POSITION = {
    "symbol": "PYPL", "description": "PAYPAL HOLDINGS INC", "category": "STOCK",
    "time_test_applicable": True, "quantity_long": "60",
    "eoy_currency": "USD", "eoy_market_price": "50",
    "lots": [
        # Old, expensive, already past the 3-year test
        {"acquisition_date": "2021-03-01", "quantity": "40", "unit_cost_eur": "78",
         "acquisition_estimated": False, "time_test_deadline": "2024-03-01"},
        # Bought on the dip, nowhere near exempt — the lot he wants to sell first
        {"acquisition_date": "2026-02-06", "quantity": "20", "unit_cost_eur": "34",
         "acquisition_estimated": False, "time_test_deadline": "2029-02-06"},
    ],
}


@pytest.fixture
def live_service(tmp_path):
    svc = RunService(
        data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
        quote_service=StubQuotes({"PYPL": (Decimal("60"), "USD")}),
        converter_factory=StubConverter,
    )
    run_dir = svc.runs_dir / "r1"
    run_dir.mkdir(parents=True)
    from src.webapp.serializers import dump_json
    dump_json({"run_id": "r1", "tax_year": 2026, "modes": ["daily"],
               "created_at": "2026-01-01T00:00:00+00:00"}, run_dir / "meta.json")
    dump_json({"tax_year": 2026, "positions": [LADDER_POSITION]},
              run_dir / "portfolio.json")
    yield svc
    svc.runner.shutdown(wait=False)


class TestLiveOverview:
    def test_distance_pct_matches_his_spreadsheet_formula(self, live_service):
        # live 60, target 70 → (70-60)/60 = +16.67 %
        live_service.save_sell_zone("PYPL", "70")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["live_price"] == Decimal("60")
        assert round(row["next_zone"]["distance_pct"], 2) == Decimal("16.67")

    def test_price_past_the_zone_is_negative_and_reached(self, live_service):
        live_service.save_sell_zone("PYPL", "50")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["next_zone"] is None          # nothing left unreached
        assert row["reached_open"] == 1
        assert row["zones"][0]["reached"] is True

    def test_next_zone_is_the_closest_unreached(self, live_service):
        for p in ("55", "70", "85"):              # 55 already passed at live 60
            live_service.save_sell_zone("PYPL", p)
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["next_zone"]["price"] == "70"
        assert row["reached_open"] == 1

    def test_ladder_does_not_sell_the_same_shares_twice(self, live_service):
        # 60 held; zone 1 takes 40, zone 2 may only have the remaining 20.
        live_service.save_sell_zone("PYPL", "70", "40")
        live_service.save_sell_zone("PYPL", "85", "40")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z70, z85 = row["zones"]
        assert z70["sellable"] == Decimal("40")
        assert z85["sellable"] == Decimal("20")   # cursor, not 40
        assert z85["short_of_plan"] is True
        # The plan asks for 80 while only 60 are held — that is the point of
        # the number; summing the clamped `sellable` would hide it.
        assert row["planned_quantity"] == Decimal("80")
        assert row["over_allocated"] is True

    def test_quantityless_zone_takes_the_whole_holding(self, live_service):
        live_service.save_sell_zone("PYPL", "70")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["zones"][0]["sellable"] == Decimal("60")

    def test_time_test_split_is_exact(self, live_service):
        # FIFO order: the 40 old exempt shares come first.
        live_service.save_sell_zone("PYPL", "70", "50")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z = row["zones"][0]
        assert z["exempt_quantity"] == Decimal("40")
        assert z["taxable_quantity"] == Decimal("10")
        assert z["unknown_quantity"] == Decimal(0)
        assert z["exempt_from"] == "2029-02-07"    # day AFTER the deadline

    def test_gross_proceeds_in_czk(self, live_service):
        live_service.save_sell_zone("PYPL", "70", "10")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["zones"][0]["proceeds_czk"] == Decimal("14000")   # 10*70*20


    def test_done_zone_leaves_the_plan(self, live_service):
        zid = live_service.save_sell_zone("PYPL", "70", "40")
        live_service.save_sell_zone("PYPL", "85", "40")
        live_service.set_zone_state("PYPL", zid, "done")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        # The sold rung no longer eats the cursor
        assert row["next_zone"]["price"] == "85"
        assert row["next_zone"]["sellable"] == Decimal("40")

    def test_symbol_not_in_the_run(self, live_service):
        live_service.save_sell_zone("GHOST", "10")
        row = next(r for r in live_service.sell_targets_overview("r1")["rows"]
                   if r["symbol"] == "GHOST")
        assert row["status"] == "not_held"
        assert row["in_portfolio"] is False
        assert row["next_zone"]["distance_pct"] is None

    def test_no_quote_and_no_eoy_price(self, tmp_path):
        svc = RunService(data_dir=tmp_path / "d", runs_dir=tmp_path / "r",
                         quote_service=StubQuotes({}), converter_factory=StubConverter)
        run_dir = svc.runs_dir / "r1"
        run_dir.mkdir(parents=True)
        from src.webapp.serializers import dump_json
        dump_json({"run_id": "r1", "tax_year": 2026, "modes": ["daily"],
                   "created_at": "2026-01-01T00:00:00+00:00"}, run_dir / "meta.json")
        pos = {**LADDER_POSITION, "eoy_market_price": None}
        dump_json({"tax_year": 2026, "positions": [pos]}, run_dir / "portfolio.json")
        try:
            svc.save_sell_zone("PYPL", "70")
            [row] = svc.sell_targets_overview("r1")["rows"]
            assert row["status"] == "no_price"
            assert row["zones"][0]["distance_pct"] is None
            assert row["zones"][0]["reached"] is False      # never fire blind
        finally:
            svc.runner.shutdown(wait=False)

    def test_currency_mismatch_refuses_to_compare(self, live_service):
        live_service.save_sell_zone("PYPL", "70", currency="EUR")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z = row["zones"][0]
        assert z["currency_mismatch"] is True
        assert z["distance_pct"] is None and z["reached"] is False

    def test_estimated_acquisition_is_unknown_not_exempt(self, tmp_path):
        svc = RunService(data_dir=tmp_path / "d", runs_dir=tmp_path / "r",
                         quote_service=StubQuotes({"PYPL": (Decimal("60"), "USD")}),
                         converter_factory=StubConverter)
        run_dir = svc.runs_dir / "r1"
        run_dir.mkdir(parents=True)
        from src.webapp.serializers import dump_json
        dump_json({"run_id": "r1", "tax_year": 2026, "modes": ["daily"],
                   "created_at": "2026-01-01T00:00:00+00:00"}, run_dir / "meta.json")
        pos = {**LADDER_POSITION, "quantity_long": "10", "lots": [
            {"acquisition_date": "2019-01-01", "quantity": "10", "unit_cost_eur": "5",
             "acquisition_estimated": True, "time_test_deadline": None}]}
        dump_json({"tax_year": 2026, "positions": [pos]}, run_dir / "portfolio.json")
        try:
            svc.save_sell_zone("PYPL", "70")
            [row] = svc.sell_targets_overview("r1")["rows"]
            z = row["zones"][0]
            assert z["unknown_quantity"] == Decimal("10")
            assert z["exempt_quantity"] == Decimal(0)
            assert z["taxable_quantity"] == Decimal(0)     # not guessed either way
        finally:
            svc.runner.shutdown(wait=False)

    def test_rows_sorted_by_distance(self, live_service):
        live_service.save_sell_zone("PYPL", "70")          # +16.67 %
        live_service.save_sell_zone("GHOST", "10")         # no price → last
        rows = live_service.sell_targets_overview("r1")["rows"]
        assert [r["symbol"] for r in rows] == ["PYPL", "GHOST"]

    def test_overview_without_any_run(self, service):
        service.save_sell_zone("PYPL", "70")
        out = service.sell_targets_overview(None)
        assert out["rows"][0]["status"] == "not_held"


class TestZoneGain:
    """The estimated gain per rung — proceeds minus the cost of exactly the
    shares that rung sells.

    The ladder fixture is built for this: the FIFO-first 40 shares cost 78 EUR
    each (bought high, now a loss at the target) and the 20 behind them cost 34
    (a gain). A rung that takes 10 must be measured on the expensive lot alone,
    not on the position average — averaging would report a profit where the
    shares actually being sold lose money.
    """

    def test_first_rung_is_measured_on_the_lots_it_actually_takes(self, live_service):
        live_service.save_sell_zone("PYPL", "70", "10")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z = row["zones"][0]
        # proceeds 10*70*20 = 14 000; cost 10*78*25 = 19 500
        assert z["cost_czk"] == Decimal("19500")
        assert z["gain_czk"] == Decimal("-5500")

    def test_a_later_rung_sees_the_cheaper_lot_the_cursor_left(self, live_service):
        """Rung 1 eats the 40 expensive shares, so rung 2 is costed on the
        cheap ones — the cursor must carry into the cost, not just the count."""
        live_service.save_sell_zone("PYPL", "70", "40")
        live_service.save_sell_zone("PYPL", "85", "20")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z70, z85 = row["zones"]
        # 40 @78 EUR: 40*70*20 = 56 000 vs 40*78*25 = 78 000 → loss
        assert z70["gain_czk"] == Decimal("-22000")
        # 20 @34 EUR: 20*85*20 = 34 000 vs 20*34*25 = 17 000 → gain
        assert z85["gain_czk"] == Decimal("17000")

    def test_a_rung_spanning_both_lots_sums_their_real_costs(self, live_service):
        live_service.save_sell_zone("PYPL", "70", "50")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z = row["zones"][0]
        # 40 @78 + 10 @34 = 3460 EUR * 25 = 86 500; proceeds 50*70*20 = 70 000
        assert z["cost_czk"] == Decimal("86500")
        assert z["gain_czk"] == Decimal("-16500")

    def test_the_gain_agrees_with_what_the_tax_button_reports(self, live_service):
        """The row and the „Daň?" card must never disagree about the same
        shares — a second, differently-computed estimate would be worse than
        none. Both are proceeds − cost over the identical lots.
        """
        zid = live_service.save_sell_zone("PYPL", "70", "10")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        impact = live_service.zone_tax_impact("r1", "PYPL", zid)
        sim = impact["sim"]
        assert (sim["exempt_gain_czk"] + sim["taxable_gain_czk"]
                == row["zones"][0]["gain_czk"])
        assert sim["proceeds_czk"] == row["zones"][0]["proceeds_czk"]

    def test_plan_totals_ride_alongside_the_next_rung(self, live_service):
        """The table's columns describe the NEXT rung; the row-level totals are
        the whole ladder. Both must be present, because the proceeds column used
        to show the plan total in a row of next-rung columns."""
        live_service.save_sell_zone("PYPL", "70", "40")
        live_service.save_sell_zone("PYPL", "85", "20")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["open_zone_count"] == 2
        assert row["next_zone"]["gain_czk"] == Decimal("-22000")     # rung only
        assert row["plan_gain_czk"] == Decimal("-5000")              # -22000 + 17000
        assert row["proceeds_czk"] == Decimal("90000")               # 56000 + 34000

    def test_a_holding_without_lot_costs_reports_no_gain(self, live_service):
        """Better a dash than a gain measured against a cost of zero, which
        would render the whole proceeds figure as pure profit."""
        live_service.save_sell_zone("GHOST", "10", "5")
        row = next(r for r in live_service.sell_targets_overview("r1")["rows"]
                   if r["symbol"] == "GHOST")
        assert row["zones"][0]["gain_czk"] is None
        assert row["plan_gain_czk"] is None


class TestOptionZoneCarriesTheMultiplier:
    """A zone on an option must apply the contract size.

    Options are kept out of the *import* resolver but the manual zone form takes
    any symbol, and `_build_target_row` has always had an "option" status — so a
    ladder rung on a contract is reachable. Its price is quoted per underlying
    share while its FIFO cost is per contract, which puts the two legs 100x
    apart if the multiplier is left out. That is the same trap that once made
    the whole option book read 100x light.
    """

    OPTION = {
        "symbol": "SOFI  280616P00015000", "description": "SOFI 15 PUT",
        "category": "OPTION", "time_test_applicable": False,
        "quantity_long": "2", "multiplier": 100,
        "eoy_currency": "USD", "eoy_market_price": "3",
        "lots": [{"acquisition_date": "2026-01-05", "quantity": "2",
                  "unit_cost_eur": "250", "acquisition_estimated": False,
                  "time_test_deadline": None}],
    }

    def _svc(self, tmp_path, pos):
        svc = RunService(data_dir=tmp_path / "d", runs_dir=tmp_path / "r",
                         quote_service=StubQuotes({}), converter_factory=StubConverter)
        run_dir = svc.runs_dir / "r1"
        run_dir.mkdir(parents=True)
        from src.webapp.serializers import dump_json
        dump_json({"run_id": "r1", "tax_year": 2026, "modes": ["daily"],
                   "created_at": "2026-01-01T00:00:00+00:00"}, run_dir / "meta.json")
        dump_json({"tax_year": 2026, "positions": [pos]}, run_dir / "portfolio.json")
        return svc

    def test_proceeds_and_gain_include_the_contract_size(self, tmp_path):
        svc = self._svc(tmp_path, self.OPTION)
        try:
            svc.save_sell_zone("SOFI  280616P00015000", "5", "2")
            [row] = svc.sell_targets_overview("r1")["rows"]
            z = row["zones"][0]
            assert row["status"] == "option"
            # 2 contracts x 5 USD x 100 shares = 1000 USD x 20 = 20 000 CZK.
            # Without the multiplier this was 200 CZK.
            assert z["proceeds_czk"] == Decimal("20000")
            # cost is already per contract: 2 x 250 EUR x 25 = 12 500
            assert z["cost_czk"] == Decimal("12500")
            assert z["gain_czk"] == Decimal("7500")
        finally:
            svc.runner.shutdown(wait=False)

    def test_an_option_without_a_multiplier_reports_no_money_at_all(self, tmp_path):
        """A run that never recorded the multiplier cannot be priced. Reporting
        nothing is right; reporting a figure 100x light is not."""
        pos = {k: v for k, v in self.OPTION.items() if k != "multiplier"}
        svc = self._svc(tmp_path, pos)
        try:
            svc.save_sell_zone("SOFI  280616P00015000", "5", "2")
            [row] = svc.sell_targets_overview("r1")["rows"]
            z = row["zones"][0]
            assert z["proceeds_czk"] is None
            assert z["gain_czk"] is None
            assert row["proceeds_czk"] is None
            # The rung still reports the shares it is about — only money is void.
            assert z["sellable"] == Decimal("2")
        finally:
            svc.runner.shutdown(wait=False)


class TestLotPinning:
    def test_pinned_zone_takes_that_purchase(self, live_service):
        # The dip lot: 20 shares bought 2026-02-06, nowhere near exempt.
        live_service.save_sell_zone("PYPL", "70", lot_acquired="2026-02-06")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z = row["zones"][0]
        assert z["sellable"] == Decimal("20")              # the lot's own size
        assert z["taxable_quantity"] == Decimal("20")
        assert z["exempt_quantity"] == Decimal(0)
        assert z["exempt_from"] == "2029-02-07"

    def test_pinned_zone_beats_fifo_order(self, live_service):
        # Unpinned, FIFO would hand it the 40 old exempt shares instead.
        live_service.save_sell_zone("PYPL", "70")
        [unpinned] = live_service.sell_targets_overview("r1")["rows"]
        assert unpinned["zones"][0]["exempt_quantity"] == Decimal("40")

        live_service.delete_sell_target("PYPL")
        live_service.save_sell_zone("PYPL", "70", lot_acquired="2026-02-06")
        [pinned] = live_service.sell_targets_overview("r1")["rows"]
        assert pinned["zones"][0]["exempt_quantity"] == Decimal(0)

    def test_pinned_lot_is_reserved_before_loose_zones(self, live_service):
        # Loose zone would otherwise eat the dip lot via FIFO fallthrough.
        live_service.save_sell_zone("PYPL", "65", lot_acquired="2026-02-06")
        live_service.save_sell_zone("PYPL", "80")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z65 = next(z for z in row["zones"] if z["price"] == "65")
        z80 = next(z for z in row["zones"] if z["price"] == "80")
        assert z65["sellable"] == Decimal("20")            # the dip lot
        assert z80["sellable"] == Decimal("40")            # only the old shares left
        assert z80["exempt_quantity"] == Decimal("40")

    def test_vanished_lot_is_flagged(self, live_service):
        live_service.save_sell_zone("PYPL", "70", lot_acquired="2001-01-01")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["zones"][0]["lot_missing"] is True
        assert row["zones"][0]["sellable"] == Decimal(0)

    def test_explicit_quantity_overrides_the_lot_size(self, live_service):
        live_service.save_sell_zone("PYPL", "70", "5", lot_acquired="2026-02-06")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["zones"][0]["sellable"] == Decimal("5")

    def test_bad_lot_date_rejected(self, live_service):
        with pytest.raises(ValueError, match="Datum nákupu"):
            live_service.save_sell_zone("PYPL", "70", lot_acquired="nesmysl")

    def test_position_lots_groups_same_day_fills(self, live_service):
        lots = live_service.position_lots("r1", "PYPL")
        assert [b["acquired"] for b in lots] == ["2021-03-01", "2026-02-06"]
        assert lots[0]["exempt"] is True and lots[1]["exempt"] is False
        assert lots[1]["quantity"] == Decimal("20")

    def test_lot_picker_reaches_the_page(self, tmp_path, live_service):
        with TestClient(create_app(services=live_service)) as c:
            live_service.save_sell_zone("PYPL", "70")
            text = c.get("/targets").text
            assert "2026-02-06" in text and "kterýkoli lot" in text


# --------------------------------------------------------------------------
# Phase 4: alerts — the reached latch, the badge, the acknowledge flow
# --------------------------------------------------------------------------

class TestReachedLatch:
    def test_live_price_at_or_above_target_latches(self, live_service):
        live_service.save_sell_zone("PYPL", "55")          # live is 60
        live_service.get_live_portfolio("r1")
        [zone] = live_service.list_sell_targets()[0]["zones"]
        assert zone["reached_at"] is not None
        assert zone["reached_price"] == "60"

    def test_exactly_at_the_target_counts(self, live_service):
        live_service.save_sell_zone("PYPL", "60")
        live_service.get_live_portfolio("r1")
        assert live_service.list_sell_targets()[0]["zones"][0]["reached_at"]

    def test_below_target_does_not_latch(self, live_service):
        live_service.save_sell_zone("PYPL", "70")
        live_service.get_live_portfolio("r1")
        assert live_service.list_sell_targets()[0]["zones"][0]["reached_at"] is None

    def test_latch_survives_the_price_falling_back(self, live_service):
        """A spike must not evaporate before the user sees it."""
        live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        first = live_service.list_sell_targets()[0]["zones"][0]["reached_at"]

        live_service.quotes.prices["PYPL"] = (Decimal("40"), "USD")   # crash
        live_service.get_live_portfolio("r1")
        zone = live_service.list_sell_targets()[0]["zones"][0]
        assert zone["reached_at"] == first          # unchanged, not cleared
        assert zone["reached_price"] == "60"        # the price that triggered
        assert live_service.sell_alert_count() == 1

    def test_eoy_price_never_latches(self, tmp_path):
        # No live quote → falls back to the 31 Dec mark. A closed-year run
        # must not fire permanent bogus alerts.
        svc = RunService(data_dir=tmp_path / "d", runs_dir=tmp_path / "r",
                         quote_service=StubQuotes({}), converter_factory=StubConverter)
        run_dir = svc.runs_dir / "r1"
        run_dir.mkdir(parents=True)
        from src.webapp.serializers import dump_json
        dump_json({"run_id": "r1", "tax_year": 2026, "modes": ["daily"],
                   "created_at": "2026-01-01T00:00:00+00:00"}, run_dir / "meta.json")
        dump_json({"tax_year": 2026, "positions": [LADDER_POSITION]},
                  run_dir / "portfolio.json")
        try:
            svc.save_sell_zone("PYPL", "10")        # far below the EOY 50
            live = svc.get_live_portfolio("r1")
            assert live["positions"][0]["price_source"] == "eoy"
            assert svc.list_sell_targets()[0]["zones"][0]["reached_at"] is None
            assert svc.sell_alert_count() == 0
        finally:
            svc.runner.shutdown(wait=False)

    def test_symbol_not_held_never_latches(self, live_service):
        live_service.save_sell_zone("GHOST", "1")
        live_service.get_live_portfolio("r1")
        assert live_service.sell_alert_count() == 0

    def test_currency_mismatch_never_latches(self, live_service):
        live_service.save_sell_zone("PYPL", "55", currency="EUR")
        live_service.get_live_portfolio("r1")
        assert live_service.list_sell_targets()[0]["zones"][0]["reached_at"] is None

    def test_sold_zone_never_latches(self, live_service):
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.set_zone_state("PYPL", zid, "done")
        live_service.get_live_portfolio("r1")
        assert live_service.list_sell_targets()[0]["zones"][0]["reached_at"] is None

    def test_no_write_when_nothing_changed(self, live_service):
        live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        stamp = live_service.sell_targets_path.stat().st_mtime_ns
        live_service.get_live_portfolio("r1")       # already latched
        assert live_service.sell_targets_path.stat().st_mtime_ns == stamp

    def test_empty_store_costs_nothing_and_returns_none(self, live_service):
        live = live_service.get_live_portfolio("r1")
        assert live["sell_targets"] is None
        assert not live_service.sell_targets_path.exists()

    def test_corrupt_store_does_not_break_valuation(self, live_service):
        live_service.sell_targets_path.parent.mkdir(parents=True, exist_ok=True)
        live_service.sell_targets_path.write_text("{ not json", encoding="utf-8")
        live = live_service.get_live_portfolio("r1")
        assert live["total_value_czk"] is not None      # card still renders
        assert live["sell_targets"] is None

    def test_evaluation_does_not_deadlock_the_worker(self, live_service):
        """REGRESSION — do not delete.

        _evaluate_sell_targets runs ON the single JobRunner worker. If it ever
        calls runner.run_sync (e.g. via save_sell_zone instead of writing
        directly), this hangs until the 120 s timeout instead of failing.
        """
        live_service.save_sell_zone("PYPL", "55")
        import threading
        done = threading.Event()
        box = {}

        def _run():
            try:
                box["live"] = live_service.get_live_portfolio("r1")
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True).start()
        assert done.wait(timeout=15), "get_live_portfolio deadlocked"
        assert box["live"]["sell_targets"]["alert_count"] == 1


class TestAlertLifecycle:
    def test_acknowledge_clears_the_badge_but_keeps_the_latch(self, live_service):
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        assert live_service.sell_alert_count() == 1

        live_service.set_zone_state("PYPL", zid, "acknowledge")
        assert live_service.sell_alert_count() == 0
        assert live_service.list_sell_targets()[0]["zones"][0]["reached_at"]

    def test_acknowledged_zone_does_not_re_alert(self, live_service):
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        live_service.set_zone_state("PYPL", zid, "acknowledge")
        live_service.get_live_portfolio("r1")       # evaluate again
        assert live_service.sell_alert_count() == 0

    def test_rearm_makes_it_fire_again(self, live_service):
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        live_service.set_zone_state("PYPL", zid, "acknowledge")
        live_service.set_zone_state("PYPL", zid, "rearm")
        assert live_service.sell_alert_count() == 0   # latch cleared
        live_service.get_live_portfolio("r1")
        assert live_service.sell_alert_count() == 1   # re-latched

    def test_done_removes_it_from_the_badge(self, live_service):
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        live_service.set_zone_state("PYPL", zid, "done")
        assert live_service.sell_alert_count() == 0

    def test_alert_payload_carries_what_the_card_needs(self, live_service):
        live_service.save_sell_zone("PYPL", "55", "25")
        live = live_service.get_live_portfolio("r1")
        [alert] = live["sell_targets"]["alerts"]
        assert alert["symbol"] == "PYPL"
        assert alert["price"] == "55" and alert["reached_price"] == "60"
        assert alert["quantity"] == "25"
        assert alert["description"] == "PAYPAL HOLDINGS INC"

    def test_alert_count_without_any_store(self, service):
        assert service.sell_alert_count() == 0

    def test_alert_count_survives_a_corrupt_store(self, service):
        service.sell_targets_path.parent.mkdir(parents=True, exist_ok=True)
        service.sell_targets_path.write_text("nonsense", encoding="utf-8")
        assert service.sell_alert_count() == 0


class TestAlertRoutes:
    def _client(self, svc):
        return TestClient(create_app(services=svc))

    def test_badge_is_empty_at_zero(self, live_service):
        with self._client(live_service) as c:
            r = c.get("/targets/badge")
            assert r.status_code == 200 and r.text.strip() == ""

    def test_badge_shows_the_count(self, live_service):
        live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        with self._client(live_service) as c:
            r = c.get("/targets/badge")
            assert "1" in r.text and "nav-badge" in r.text

    def test_nav_links_to_targets_with_a_lazy_badge(self, live_service):
        with self._client(live_service) as c:
            text = c.get("/targets").text
            assert 'href="/targets"' in text
            assert 'hx-get="/targets/badge"' in text

    def test_targets_page_lists_reached_zones(self, live_service):
        live_service.save_sell_zone("PYPL", "55")
        with self._client(live_service) as c:
            text = c.get("/targets").text          # this load also latches
            assert "Dosažené prodejní zóny" in text
            assert "Beru na vědomí" in text

    def test_acknowledge_from_the_card(self, live_service):
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        with self._client(live_service) as c:
            r = c.post("/targets/zone/state",
                       data={"symbol": "PYPL", "zone_id": zid,
                             "action": "acknowledge"}, follow_redirects=False)
            assert r.status_code == 303
        assert live_service.sell_alert_count() == 0

    def test_opening_the_page_arms_the_latch(self, live_service):
        live_service.save_sell_zone("PYPL", "55")
        assert live_service.sell_alert_count() == 0
        with self._client(live_service) as c:
            c.get("/targets")
        assert live_service.sell_alert_count() == 1

    def test_rearm_offered_in_both_the_card_and_the_ladder(self, live_service):
        live_service.save_sell_zone("PYPL", "55")
        with self._client(live_service) as c:
            text = c.get("/targets").text          # this load also latches
            assert "Zrušit dosažení" in text
            # Two forms: one in the alert card, one on the zone's row in the
            # ladder. Counting the hidden input, not the label — the card's
            # explanatory note mentions the label too.
            assert text.count('value="rearm"') == 2

    def test_no_rearm_button_before_anything_latches(self, live_service):
        live_service.save_sell_zone("PYPL", "70")   # live is 60 — never reached
        with self._client(live_service) as c:
            assert "Zrušit dosažení" not in c.get("/targets").text

    def test_rearm_still_reachable_after_acknowledging(self, live_service):
        """Acknowledging hides the card, so the ladder row is the only way back."""
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")
        live_service.set_zone_state("PYPL", zid, "acknowledge")
        with self._client(live_service) as c:
            text = c.get("/targets").text
            assert "Dosažené prodejní zóny" not in text
            assert text.count('value="rearm"') == 1

    def test_rearm_from_the_page_clears_a_latch_set_by_a_bad_quote(self, live_service):
        """The KWEB case: an unmapped symbol quoted the wrong listing, latched
        the zone, and the marker outlived the symbol_map fix."""
        zid = live_service.save_sell_zone("PYPL", "55")
        live_service.get_live_portfolio("r1")                        # latches at 60
        live_service.quotes.prices["PYPL"] = (Decimal("40"), "USD")  # mapping fixed
        with self._client(live_service) as c:
            assert "dosaženo dřív" in c.get("/targets").text   # latch outlived the price
            r = c.post("/targets/zone/state",
                       data={"symbol": "PYPL", "zone_id": zid, "action": "rearm"},
                       follow_redirects=False)
            assert r.status_code == 303
            # Re-evaluated at 40 against a target of 55: nothing re-latches.
            assert "Dosažené prodejní zóny" not in c.get("/targets").text
        zone = live_service.list_sell_targets()[0]["zones"][0]
        assert zone["reached_at"] is None and zone["reached_price"] is None


class TestUnspecifiedZonesDoNotStarveEachOther:
    """Two rungs that both say "sell everything" are an unfinished plan.

    Letting the cheaper one consume the position left the next rung showing
    0 ks and no time-test data — spotted on the real BABA ladder.
    """

    def test_two_quantityless_zones_both_see_the_position(self, live_service):
        live_service.save_sell_zone("PYPL", "70")
        live_service.save_sell_zone("PYPL", "85")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert [z["sellable"] for z in row["zones"]] == [Decimal("60"), Decimal("60")]
        assert all(z["unspecified"] for z in row["zones"])
        # Nothing is committed, so no plan total and no bogus warning
        assert row["planned_quantity"] == Decimal(0)
        assert row["over_allocated"] is False

    def test_time_test_shown_on_every_unspecified_rung(self, live_service):
        live_service.save_sell_zone("PYPL", "70")
        live_service.save_sell_zone("PYPL", "85")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        for z in row["zones"]:
            assert z["exempt_quantity"] == Decimal("40")     # the old lot
            assert z["taxable_quantity"] == Decimal("20")

    def test_sized_zones_still_consume(self, live_service):
        live_service.save_sell_zone("PYPL", "70", "40")
        live_service.save_sell_zone("PYPL", "85", "40")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert [z["sellable"] for z in row["zones"]] == [Decimal("40"), Decimal("20")]
        assert row["planned_quantity"] == Decimal("80")

    def test_sized_zone_reserves_against_an_unspecified_one(self, live_service):
        live_service.save_sell_zone("PYPL", "70", "40")
        live_service.save_sell_zone("PYPL", "85")            # "the rest"
        [row] = live_service.sell_targets_overview("r1")["rows"]
        z70 = next(z for z in row["zones"] if z["price"] == "70")
        z85 = next(z for z in row["zones"] if z["price"] == "85")
        assert z70["sellable"] == Decimal("40")
        assert z85["sellable"] == Decimal("20")              # what is left
        assert row["planned_quantity"] == Decimal("40")      # only the committed rung

    def test_over_allocation_still_detected_on_sized_zones(self, live_service):
        live_service.save_sell_zone("PYPL", "70", "50")
        live_service.save_sell_zone("PYPL", "85", "50")
        [row] = live_service.sell_targets_overview("r1")["rows"]
        assert row["over_allocated"] is True


# --------------------------------------------------------------------------
# Phase 5: on-demand tax impact per zone
# --------------------------------------------------------------------------

SIM_RESULT = {"sections": {"cz_10_summary": {"line_items": {
    "annual_limit_eligible_proceeds_czk": "715704.73",
    "annual_limit_threshold_czk": "100000.00"}}}}


@pytest.fixture
def tax_service(live_service):
    """live_service + the tax result simulate_sale reads the 100k figure from."""
    from src.webapp.serializers import dump_json
    dump_json(SIM_RESULT, live_service.runs_dir / "r1" / "result.daily.json")
    return live_service


class TestLotSkipping:
    """`skip_quantity` is what makes the tax panel describe the RIGHT shares.

    Without it the simulator always starts at lot 0, so a zone pinned to the
    cheap February purchase would be priced against the old exempt shares —
    exactly backwards for a sell-the-dip plan.
    """

    def test_skip_zero_is_unchanged_behaviour(self, tax_service):
        sim = tax_service.simulate_sale("r1", "PYPL", Decimal("50"), Decimal("70"))
        assert [c["quantity"] for c in sim["consumed"]] == [Decimal("40"), Decimal("10")]
        assert sim["consumed"][0]["acquisition_date"] == "2021-03-01"

    def test_skip_moves_past_the_old_lot(self, tax_service):
        sim = tax_service.simulate_sale("r1", "PYPL", Decimal("20"), Decimal("70"),
                                        skip_quantity=Decimal("40"))
        assert [c["acquisition_date"] for c in sim["consumed"]] == ["2026-02-06"]
        assert sim["consumed"][0]["exempt"] is False
        assert sim["exempt_gain_czk"] == Decimal(0)

    def test_partial_skip_splits_a_lot(self, tax_service):
        sim = tax_service.simulate_sale("r1", "PYPL", Decimal("30"), Decimal("70"),
                                        skip_quantity=Decimal("30"))
        assert [(c["acquisition_date"], c["quantity"]) for c in sim["consumed"]] == [
            ("2021-03-01", Decimal("10")), ("2026-02-06", Decimal("20"))]

    def test_skip_shrinks_what_is_available(self, tax_service):
        sim = tax_service.simulate_sale("r1", "PYPL", Decimal("999"), Decimal("70"),
                                        skip_quantity=Decimal("40"))
        assert sim["quantity"] == Decimal("20")      # clamped to what is left

    def test_skipping_everything_raises(self, tax_service):
        with pytest.raises(ValueError):
            tax_service.simulate_sale("r1", "PYPL", Decimal("5"), Decimal("70"),
                                      skip_quantity=Decimal("60"))


class TestZoneTaxImpact:
    def test_pinned_zone_is_priced_on_its_own_lot(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70", lot_acquired="2026-02-06")
        out = tax_service.zone_tax_impact("r1", "PYPL", zid)
        assert out["skip_quantity"] == Decimal("40")          # the old shares
        assert [c["acquisition_date"] for c in out["sim"]["consumed"]] == ["2026-02-06"]
        assert out["sim"]["quantity"] == Decimal("20")
        assert out["sim"]["exempt_gain_czk"] == Decimal(0)    # dip lot is taxable
        assert out["sim"]["price_source"] == "manual"         # the zone's price

    def test_unpinned_zone_uses_fifo_from_the_start(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70", "50")
        out = tax_service.zone_tax_impact("r1", "PYPL", zid)
        assert out["skip_quantity"] == Decimal(0)
        assert out["sim"]["consumed"][0]["acquisition_date"] == "2021-03-01"

    def test_later_rung_skips_what_the_cheaper_one_took(self, tax_service):
        tax_service.save_sell_zone("PYPL", "70", "40")
        zid = tax_service.save_sell_zone("PYPL", "85", "20")
        out = tax_service.zone_tax_impact("r1", "PYPL", zid)
        assert out["skip_quantity"] == Decimal("40")
        assert [c["acquisition_date"] for c in out["sim"]["consumed"]] == ["2026-02-06"]

    def test_quantityless_zone_prices_the_whole_holding(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70")
        out = tax_service.zone_tax_impact("r1", "PYPL", zid)
        assert out["sim"]["quantity"] == Decimal("60")

    def test_zone_with_nothing_left_explains_itself(self, tax_service):
        tax_service.save_sell_zone("PYPL", "70", "60")        # takes it all
        zid = tax_service.save_sell_zone("PYPL", "85", "10")
        with pytest.raises(ValueError, match="nezbývají"):
            tax_service.zone_tax_impact("r1", "PYPL", zid)

    def test_missing_lot_explains_itself(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70", lot_acquired="2001-01-01")
        with pytest.raises(ValueError, match="nezbývají"):
            tax_service.zone_tax_impact("r1", "PYPL", zid)

    def test_symbol_not_held(self, tax_service):
        zid = tax_service.save_sell_zone("GHOST", "10")
        with pytest.raises(ValueError, match="není mezi otevřenými"):
            tax_service.zone_tax_impact("r1", "GHOST", zid)

    def test_unknown_zone(self, tax_service):
        tax_service.save_sell_zone("PYPL", "70")
        with pytest.raises(ValueError, match="Zóna neexistuje"):
            tax_service.zone_tax_impact("r1", "PYPL", "nope")

    def test_without_a_run(self, service):
        zid = service.save_sell_zone("PYPL", "70")
        with pytest.raises(ValueError, match="výpočet"):
            service.zone_tax_impact(None, "PYPL", zid)

    def test_pairing_method_is_reported_for_the_caveat(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70")
        assert "pairing_method" in tax_service.zone_tax_impact("r1", "PYPL", zid)


class TestZoneTaxRoutes:
    def _client(self, svc):
        return TestClient(create_app(services=svc))

    def test_panel_renders(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70", lot_acquired="2026-02-06")
        with self._client(tax_service) as c:
            r = c.get(f"/targets/tax/PYPL/{zid}?run_id=r1")
            assert r.status_code == 200
            assert "Odhad daně" in r.text
            assert "2026-02-06" in r.text
            assert "přeskočeno" in r.text          # the skip is disclosed
            assert "ne daňové poradenství" in r.text

    def test_errors_render_as_a_friendly_card(self, tax_service):
        tax_service.save_sell_zone("PYPL", "70")
        with self._client(tax_service) as c:
            r = c.get("/targets/tax/PYPL/nope?run_id=r1")
            assert r.status_code == 200 and "Zóna neexistuje" in r.text

    def test_button_is_wired_on_the_page(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70")
        with self._client(tax_service) as c:
            text = c.get("/targets").text
            assert f'hx-get="/targets/tax/PYPL/{zid}' in text
            assert f'id="tax-{zid}"' in text

    def test_no_button_for_a_sold_zone(self, tax_service):
        zid = tax_service.save_sell_zone("PYPL", "70")
        tax_service.set_zone_state("PYPL", zid, "done")
        with self._client(tax_service) as c:
            assert f'hx-get="/targets/tax/PYPL/{zid}' not in c.get("/targets").text

    def test_symbol_with_spaces_is_url_encoded(self, tmp_path):
        """Option keys carry spaces — the button URL must survive them."""
        from src.webapp.serializers import dump_json
        svc = RunService(data_dir=tmp_path / "d", runs_dir=tmp_path / "r",
                         quote_service=StubQuotes({}), converter_factory=StubConverter)
        run_dir = svc.runs_dir / "r1"
        run_dir.mkdir(parents=True)
        dump_json({"run_id": "r1", "tax_year": 2026, "modes": ["daily"],
                   "created_at": "2026-01-01T00:00:00+00:00"}, run_dir / "meta.json")
        dump_json(SIM_RESULT, run_dir / "result.daily.json")
        opt = {"symbol": "C TUI  20260619 9 M", "description": "TUI1 19JUN26 9 C",
               "category": "OPTION", "time_test_applicable": False,
               "quantity_long": "2", "eoy_currency": "EUR", "eoy_market_price": "1.5",
               "lots": [{"acquisition_date": "2026-01-05", "quantity": "2",
                         "unit_cost_eur": "1", "acquisition_estimated": False,
                         "time_test_deadline": None}]}
        dump_json({"tax_year": 2026, "positions": [opt]}, run_dir / "portfolio.json")
        try:
            svc.save_sell_zone("C TUI  20260619 9 M", "5")
            with TestClient(create_app(services=svc)) as c:
                text = c.get("/targets").text
                assert "/targets/tax/C%20TUI%20%2020260619%209%20M/" in text
        finally:
            svc.runner.shutdown(wait=False)
