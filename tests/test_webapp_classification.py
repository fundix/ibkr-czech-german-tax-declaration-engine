# tests/test_webapp_classification.py
"""
Phase 2 — web asset classification (the non-interactive cache editor).

Covered:
- ``AssetClassifier.classification_options`` is the shared source for CLI +
  web; ``classification_choices`` deduplicates and glosses to Czech.
- ``scan_unclassified_assets`` discovers a year's assets with NO side effects:
  it never writes the cache and never auto-defaults UNKNOWN→STOCK.
- ``save_classification`` writes the exact ``(category, fund_type, notes)``
  tuple the interactive dialog would, and validates the choice.
- ``delete_classification`` removes an entry.
"""
import json
from pathlib import Path

import pytest

from src.classification.asset_classifier import AssetClassifier
from src.domain.enums import AssetCategory, InvestmentFundType
from src.webapp.services import RunService
from tests.test_webapp_services import _seed_synthetic_year


@pytest.fixture
def service(tmp_path, monkeypatch):
    # Point the (global) classification cache at a throwaway file so tests
    # never touch the developer's real cache/user_classifications.json.
    cache = tmp_path / "cache" / "user_classifications.json"
    monkeypatch.setattr("src.config.CLASSIFICATION_CACHE_FILE_PATH", str(cache))
    svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    svc._cache_path = cache
    yield svc
    svc.runner.shutdown(wait=False)


class TestClassificationOptions:
    def test_options_are_the_shared_source(self):
        opts = AssetClassifier.classification_options()
        pairs = {(cat, ft) for _, cat, ft in opts}
        assert (AssetCategory.STOCK, InvestmentFundType.NONE) in pairs
        assert (AssetCategory.INVESTMENT_FUND, InvestmentFundType.AKTIENFONDS) in pairs
        assert (AssetCategory.PRIVATE_SALE_ASSET, InvestmentFundType.NONE) in pairs
        # An instance exposes the very same list to the CLI dialog.
        assert AssetClassifier().classification_options() == opts

    def test_choices_dedupe_and_are_czech(self, service):
        choices = service.classification_choices()
        values = [c["value"] for c in choices]
        # "Aktie" and "Sonstiges" both map to STOCK:NONE — kept once.
        assert values.count("STOCK:NONE") == 1
        assert len(values) == len(set(values))
        labels = {c["value"]: c["label"] for c in choices}
        assert labels["STOCK:NONE"] == "Akcie"
        assert labels["INVESTMENT_FUND:AKTIENFONDS"] == "Akciový fond"


class TestScan:
    def test_scan_lists_pending_without_touching_cache(self, service):
        _seed_synthetic_year(service)
        assert not service._cache_path.exists()  # nothing cached yet

        scan = service.scan_unclassified_assets(2024)

        # Empty cache → every discovered asset is pending, none classified.
        assert scan["classified_count"] == 0
        assert len(scan["pending"]) > 0
        symbols = {r["symbol"] for r in scan["pending"]}
        assert "DIVCO" in symbols
        # Discovery only: the cache file must NOT have been created/written.
        assert not service._cache_path.exists()

    def test_scan_surfaces_cached_entries_as_classified(self, service):
        _seed_synthetic_year(service)
        # Pre-classify DIVCO as it is discovered (ISIN key from the dataset).
        scan = service.scan_unclassified_assets(2024)
        divco = next(r for r in scan["pending"] if r["symbol"] == "DIVCO")
        service.save_classification(divco["key"], "STOCK:NONE", "ruční")

        scan2 = service.scan_unclassified_assets(2024)
        assert scan2["classified_count"] == 1
        assert divco["key"] not in {r["key"] for r in scan2["pending"]}

    def test_scan_flags_auto_defaults_for_review(self, service):
        _seed_synthetic_year(service)
        scan = service.scan_unclassified_assets(2024)
        key = scan["pending"][0]["key"]
        # An auto-default entry (as a non-interactive run would write).
        service._write_classification(key, "STOCK", "NONE",
                                      "Auto-defaulted from UNKNOWN to STOCK")
        review_keys = {r["key"] for r in service.scan_unclassified_assets(2024)["review"]}
        assert key in review_keys

    def test_confident_heuristic_is_not_flagged_for_review(self, service):
        _seed_synthetic_year(service)
        scan = service.scan_unclassified_assets(2024)
        key = scan["pending"][0]["key"]
        # A confident heuristic hit (STK→STOCK) is trusted, not review-worthy.
        service._write_classification(key, "STOCK", "NONE",
                                      "Auto-classified based on heuristics.")
        rescan = service.scan_unclassified_assets(2024)
        assert key not in {r["key"] for r in rescan["review"]}
        assert rescan["classified_count"] >= 1

    def test_scan_missing_dataset_raises(self, service):
        with pytest.raises(ValueError, match="2031"):
            service.scan_unclassified_assets(2031)


class TestSaveDelete:
    def test_save_writes_exact_tuple(self, service):
        service.save_classification("ISIN:US0000000001",
                                    "INVESTMENT_FUND:AKTIENFONDS", "fond")
        cache = json.loads(service._cache_path.read_text(encoding="utf-8"))
        assert cache["ISIN:US0000000001"] == ["INVESTMENT_FUND", "AKTIENFONDS", "fond"]

    def test_non_fund_choice_forces_fund_type_none(self, service):
        service.save_classification("CONID:42", "STOCK:NONE", "")
        cache = json.loads(service._cache_path.read_text(encoding="utf-8"))
        assert cache["CONID:42"] == ["STOCK", "NONE", ""]

    def test_saved_entry_is_readable_by_asset_classifier(self, service):
        service.save_classification("ISIN:DE0000000002",
                                    "BOND:NONE", "dluhopis")
        # The engine's classifier loads exactly what we wrote.
        loaded = service._new_classifier().classifications_cache
        assert loaded["ISIN:DE0000000002"] == ("BOND", "NONE", "dluhopis")

    def test_invalid_choice_rejected(self, service):
        with pytest.raises(ValueError, match="Neplatná klasifikace"):
            service.save_classification("ISIN:X", "STOCK:AKTIENFONDS", "")

    def test_empty_key_rejected(self, service):
        with pytest.raises(ValueError, match="identifikátor"):
            service.save_classification("  ", "STOCK:NONE", "")

    def test_delete_removes_entry(self, service):
        service.save_classification("CONID:7", "STOCK:NONE", "")
        service.delete_classification("CONID:7")
        cache = json.loads(service._cache_path.read_text(encoding="utf-8"))
        assert "CONID:7" not in cache
