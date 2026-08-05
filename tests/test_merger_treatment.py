# tests/test_merger_treatment.py
"""
Merger treatment classification — the decision the statement cannot make.

Covers the engine-level key and mechanics (country-agnostic), the Czech
§23b/§23c store and policy, and the processor's refusal to guess.
"""
import json
import uuid
from decimal import Decimal
from typing import Any, Dict

import pytest

from src.countries.cz.merger_treatment import (
    CzMergerPolicy,
    CzMergerTreatment,
    MergerTreatmentStore,
)
from src.engine.merger_policy import (
    MergerDecision,
    MergerMechanics,
    merger_event_key,
)


class TestEventKey:
    def test_key_is_readable_and_identifies_both_sides(self):
        key = merger_event_key("A123", "2025-06-10", "OLDCO", "NEWCO")
        assert key == "A123|2025-06-10|OLDCO->NEWCO"

    def test_same_action_id_different_legs_get_different_keys(self):
        """A multi-leg corporate action repeats its ActionID across legs."""
        a = merger_event_key("A1", "2025-06-10", "OLD", "NEWA")
        b = merger_event_key("A1", "2025-06-10", "OLD", "NEWB")
        assert a != b

    def test_missing_parts_do_not_collapse_the_key_or_raise(self):
        key = merger_event_key(None, "2025-06-10", None, "NEWCO")
        assert key == "?|2025-06-10|?->NEWCO"

    def test_input_cannot_inject_the_separator(self):
        """A symbol containing '|' must not be able to forge extra key fields."""
        key = merger_event_key("A|1", "2025-06-10", "O|X", "N")
        assert key == "A1|2025-06-10|OX->N"
        assert key.count("|") == 2   # the key's own two separators, no more


class TestStore:
    def _store(self, tmp_path, payload=None):
        path = tmp_path / "mergers.json"
        if payload is not None:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return MergerTreatmentStore(cache_file_path=path), path

    def test_unknown_key_is_unclassified_not_an_error(self, tmp_path):
        store, _ = self._store(tmp_path)
        assert store.get("nope").treatment is CzMergerTreatment.UNCLASSIFIED

    def test_set_persists_and_reloads(self, tmp_path):
        store, path = self._store(tmp_path)
        store.set("k", CzMergerTreatment.QUALIFIED_23C, note="S-4 on file")

        reloaded = MergerTreatmentStore(cache_file_path=path)
        record = reloaded.get("k")
        assert record.treatment is CzMergerTreatment.QUALIFIED_23C
        assert record.note == "S-4 on file"

    def test_bare_string_value_is_accepted(self, tmp_path):
        """Hand-edited files are the normal case — a plain value must work."""
        store, _ = self._store(tmp_path, {"k": "outside_safe_harbor"})
        assert store.get("k").treatment is CzMergerTreatment.OUTSIDE_SAFE_HARBOR

    def test_unknown_treatment_value_is_ignored_not_guessed(self, tmp_path):
        store, _ = self._store(tmp_path, {"k": {"treatment": "probably_fine"}})
        assert store.get("k").treatment is CzMergerTreatment.UNCLASSIFIED

    def test_malformed_entry_does_not_lose_the_valid_ones(self, tmp_path):
        store, _ = self._store(tmp_path, {
            "bad": 42,
            "good": {"treatment": "qualified_23b"},
        })
        assert store.get("bad").treatment is CzMergerTreatment.UNCLASSIFIED
        assert store.get("good").treatment is CzMergerTreatment.QUALIFIED_23B

    def test_unreadable_file_starts_empty(self, tmp_path):
        path = tmp_path / "mergers.json"
        path.write_text("{ not json", encoding="utf-8")
        store = MergerTreatmentStore(cache_file_path=path)
        assert store.get("k").treatment is CzMergerTreatment.UNCLASSIFIED

    def test_placeholder_gives_the_preparer_a_line_to_edit(self, tmp_path):
        store, path = self._store(tmp_path)
        store.record_placeholder("A1|2025-06-10|OLD->NEW")

        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["A1|2025-06-10|OLD->NEW"]["treatment"] == "unclassified"

    def test_placeholder_never_overwrites_a_real_decision(self, tmp_path):
        store, path = self._store(tmp_path)
        store.set("k", CzMergerTreatment.QUALIFIED_23B, note="verified")
        store.record_placeholder("k")

        assert store.get("k").treatment is CzMergerTreatment.QUALIFIED_23B
        assert store.get("k").note == "verified"


class TestCzPolicy:
    def _policy(self, tmp_path, payload=None):
        path = tmp_path / "mergers.json"
        if payload is not None:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return CzMergerPolicy(store=MergerTreatmentStore(cache_file_path=path))

    @pytest.mark.parametrize("recorded,expected", [
        ("qualified_23b", MergerMechanics.CARRY_OVER),
        ("qualified_23c", MergerMechanics.CARRY_OVER),
        ("outside_safe_harbor", MergerMechanics.TAXABLE_DISPOSAL),
    ])
    def test_regime_maps_to_ledger_mechanics(self, tmp_path, recorded, expected):
        policy = self._policy(tmp_path, {"k": recorded})
        decision = policy.decide("k")

        assert decision.is_decided
        assert decision.mechanics is expected
        assert recorded in decision.label   # the regime is named for the audit trail

    def test_unclassified_is_undecided_and_says_what_to_do(self, tmp_path):
        policy = self._policy(tmp_path)
        decision = policy.decide("A1|2025-06-10|OLD->NEW")

        assert not decision.is_decided
        assert decision.mechanics is None
        # The reason has to be actionable: the key, the choices, the evidence.
        assert "A1|2025-06-10|OLD->NEW" in decision.reason
        assert "qualified_23b" in decision.reason
        assert "outside_safe_harbor" in decision.reason
        assert "§23d" in decision.reason

    def test_deciding_leaves_a_placeholder_behind(self, tmp_path):
        path = tmp_path / "mergers.json"
        policy = CzMergerPolicy(store=MergerTreatmentStore(cache_file_path=path))
        policy.decide("A1|2025-06-10|OLD->NEW")

        assert path.is_file()
        assert "A1|2025-06-10|OLD->NEW" in json.loads(path.read_text(encoding="utf-8"))

    def test_qualified_flag_matches_the_two_deferral_regimes(self):
        assert CzMergerTreatment.QUALIFIED_23B.is_qualified
        assert CzMergerTreatment.QUALIFIED_23C.is_qualified
        assert not CzMergerTreatment.OUTSIDE_SAFE_HARBOR.is_qualified
        assert not CzMergerTreatment.UNCLASSIFIED.is_qualified


class TestProcessorRefuses:
    """The stub used to return [] — silently keeping wrong positions."""

    def _event_and_ledger(self):
        from src.domain.events import CorpActionMergerStock
        from tests.test_audit_fixes import _make_ledger

        old_id, new_id = uuid.uuid4(), uuid.uuid4()
        event = CorpActionMergerStock(
            asset_internal_id=old_id,
            event_date="2025-06-10",
            new_asset_internal_id=new_id,
            new_shares_received_per_old=Decimal("2.5"),
            ca_action_id_ibkr="A1",
        )
        return event, _make_ledger()

    def _context(self, policy) -> Dict[str, Any]:
        return {"asset_resolver": None, "merger_policy": policy}

    def test_unclassified_merger_refuses_with_an_actionable_message(self, tmp_path):
        from src.engine.event_processors.corporate_action_processor import (
            MergerStockProcessor,
        )

        event, ledger = self._event_and_ledger()
        policy = CzMergerPolicy(
            store=MergerTreatmentStore(cache_file_path=tmp_path / "m.json")
        )

        with pytest.raises(ValueError, match="qualified_23b"):
            MergerStockProcessor().process(event, ledger, self._context(policy))

    def test_a_recorded_regime_still_refuses_but_names_the_decision(self, tmp_path):
        """Mechanics are unimplemented — but the message must prove the
        recorded decision was read, not lost."""
        from src.engine.event_processors.corporate_action_processor import (
            MergerStockProcessor,
        )

        path = tmp_path / "m.json"
        path.write_text(json.dumps({
            "A1|2025-06-10|?->?": "qualified_23c",
        }), encoding="utf-8")
        policy = CzMergerPolicy(store=MergerTreatmentStore(cache_file_path=path))
        event, ledger = self._event_and_ledger()

        with pytest.raises(ValueError, match="CARRY_OVER"):
            MergerStockProcessor().process(event, ledger, self._context(policy))

    def test_missing_policy_refuses_rather_than_defaulting(self):
        from src.engine.event_processors.corporate_action_processor import (
            MergerStockProcessor,
        )

        event, ledger = self._event_and_ledger()
        with pytest.raises(ValueError, match="no merger policy"):
            MergerStockProcessor().process(event, ledger, self._context(None))

    def test_no_ledger_still_returns_empty_as_before(self):
        """Unrelated guard clauses are untouched."""
        from src.engine.event_processors.corporate_action_processor import (
            MergerStockProcessor,
        )

        event, _ = self._event_and_ledger()
        assert MergerStockProcessor().process(event, None, {}) == []


class TestGermanPluginHasNoRuleOnFile:
    def test_german_plugin_returns_no_policy(self):
        """§20 Abs 4a EStG was never ported — it must not be assumed."""
        from src.countries.de.plugin import GermanTaxPlugin

        assert GermanTaxPlugin().get_merger_policy() is None

    def test_czech_plugin_supplies_one(self):
        from src.countries.cz.plugin import CzechTaxPlugin

        policy = CzechTaxPlugin().get_merger_policy()
        assert isinstance(policy.decide("whatever"), MergerDecision)
