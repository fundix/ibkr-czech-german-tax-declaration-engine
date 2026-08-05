# src/countries/cz/merger_treatment.py
"""
Which tax regime a stock-for-stock merger falls under — a decision the input
data cannot make.

A tax advisor's answer (2026-08-05, see ``docs/otazky-danovy-poradce-fuze.md``)
splits these transactions in two, with opposite consequences:

* **Qualified** §23b (výměna podílů — the acquirer takes more than 50 % of the
  voting rights) or §23c (fúze — the target ceases to exist without
  liquidation), all conditions including residence met: a deferral. The
  acquisition cost carries over and the holding period keeps running.
* **Everything else**, which is where an ordinary US stock-for-stock merger
  lands: a taxable disposal of the old shares at the fair value of the
  consideration, a fresh acquisition cost and a fresh holding period.

What decides it is the tax residence and legal form of the companies involved
— not the venue, ticker or ISIN. An Irish company on Nasdaq may be an EU
transaction; a US company listed in Frankfurt stays American. The broker's
``MERGER`` label carries none of that, so the regime cannot be inferred from
the statement and is recorded per event here instead.

Unclassified is NOT a usable default: the processor refuses rather than
guessing, because both wrong answers are wrong in a way that shows up as tax
(taxing a deferral, or ignoring a realised gain).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

from src.engine.merger_policy import MergerDecision, MergerMechanics

logger = logging.getLogger(__name__)

DEFAULT_TREATMENT_CACHE_PATH = Path("cache/merger_treatments.json")


class CzMergerTreatment(Enum):
    """Regime recorded for one stock-for-stock merger event."""

    #: No decision on file. The processor refuses; it never picks for you.
    UNCLASSIFIED = "unclassified"

    #: Outside §23b/§23c — taxable disposal at the consideration's fair value.
    #: The safe default for a US transaction, but it must still be chosen
    #: explicitly, since choosing it wrongly over-taxes a real deferral.
    OUTSIDE_SAFE_HARBOR = "outside_safe_harbor"

    #: §23b výměna podílů — deferral, cost and holding period carry over.
    QUALIFIED_23B = "qualified_23b"

    #: §23c fúze — deferral, cost and holding period carry over.
    QUALIFIED_23C = "qualified_23c"

    @property
    def is_qualified(self) -> bool:
        return self in (CzMergerTreatment.QUALIFIED_23B, CzMergerTreatment.QUALIFIED_23C)

    @property
    def citation(self) -> str:
        return {
            CzMergerTreatment.QUALIFIED_23B: "§23b ZDP",
            CzMergerTreatment.QUALIFIED_23C: "§23c ZDP",
            CzMergerTreatment.OUTSIDE_SAFE_HARBOR: "§10 ZDP (mimo §23b/§23c)",
            CzMergerTreatment.UNCLASSIFIED: "neurčeno",
        }[self]


#: What the preparer has to hold before claiming a qualified regime. Quoted in
#: the refusal message so the requirement is visible where the decision is made.
QUALIFIED_EVIDENCE_CHECKLIST = (
    "merger agreement / prospekt (u USA typicky S-4 nebo F-4 a closing 8-K)",
    "právní datum účinnosti (legal effective date)",
    "identita, daňová rezidence a právní forma všech zúčastněných společností",
    "zda cílová společnost zanikla (§23c), nebo šlo o získání většiny hlasů (§23b)",
    "výměnný poměr, případný doplatek a zacházení se zlomkovými akciemi",
    "oznámení postupu správci daně podle §23d odst. 1 (před transakcí)",
)


@dataclass(frozen=True)
class MergerTreatmentRecord:
    """A recorded decision plus the note documenting it."""

    treatment: CzMergerTreatment
    note: str = ""


class MergerTreatmentStore:
    """Per-event merger regimes, persisted as readable JSON.

    Mirrors ``AssetClassifier``'s cache: a flat dict the preparer can edit by
    hand, since there is no way to derive these values. Unknown keys resolve to
    ``UNCLASSIFIED`` rather than raising, so a run reaches the refusal message
    (which names the key to add) instead of a KeyError.
    """

    def __init__(self, cache_file_path: Optional[Path] = None):
        self.cache_file_path = Path(cache_file_path or DEFAULT_TREATMENT_CACHE_PATH)
        self._records: Dict[str, MergerTreatmentRecord] = {}
        self.load()

    def load(self) -> None:
        self._records = {}
        if not self.cache_file_path.is_file():
            return
        try:
            raw = json.loads(self.cache_file_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(
                f"Cannot read merger treatments from {self.cache_file_path}: {exc}. "
                "Treating every merger as unclassified."
            )
            return
        for key, value in (raw or {}).items():
            record = self._parse(key, value)
            if record is not None:
                self._records[key] = record

    @staticmethod
    def _parse(key: str, value: object) -> Optional[MergerTreatmentRecord]:
        """Read one entry; an unusable one is skipped loudly, not guessed at."""
        if isinstance(value, str):
            treatment_raw, note = value, ""
        elif isinstance(value, dict):
            treatment_raw = str(value.get("treatment") or "")
            note = str(value.get("note") or "")
        else:
            logger.error(f"Merger treatment for '{key}' is not a string or object — ignored.")
            return None
        try:
            treatment = CzMergerTreatment(treatment_raw)
        except ValueError:
            allowed = ", ".join(t.value for t in CzMergerTreatment)
            logger.error(
                f"Merger treatment '{treatment_raw}' for '{key}' is not one of: "
                f"{allowed} — ignored, the event stays unclassified."
            )
            return None
        return MergerTreatmentRecord(treatment=treatment, note=note)

    def save(self) -> None:
        try:
            self.cache_file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                key: {"treatment": rec.treatment.value, "note": rec.note}
                for key, rec in sorted(self._records.items())
            }
            self.cache_file_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(f"Cannot save merger treatments to {self.cache_file_path}: {exc}")

    def get(self, key: str) -> MergerTreatmentRecord:
        return self._records.get(key, MergerTreatmentRecord(CzMergerTreatment.UNCLASSIFIED))

    def set(self, key: str, treatment: CzMergerTreatment, note: str = "") -> None:
        self._records[key] = MergerTreatmentRecord(treatment=treatment, note=note)
        self.save()

    def record_placeholder(self, key: str) -> None:
        """Write an ``unclassified`` stub so the preparer has a line to edit.

        Without this the cache file stays empty and the only trace of the
        pending decision is a log line.
        """
        if key in self._records:
            return
        self._records[key] = MergerTreatmentRecord(
            treatment=CzMergerTreatment.UNCLASSIFIED,
            note="Vyplňte režim — viz docs/cz-tax-policy.md (Merger).",
        )
        self.save()

    def keys(self) -> Dict[str, MergerTreatmentRecord]:
        return dict(self._records)


#: Czech regime → what the ledger does. Both qualified regimes defer, so both
#: carry the lots over; anything else realises them.
_MECHANICS = {
    CzMergerTreatment.QUALIFIED_23B: MergerMechanics.CARRY_OVER,
    CzMergerTreatment.QUALIFIED_23C: MergerMechanics.CARRY_OVER,
    CzMergerTreatment.OUTSIDE_SAFE_HARBOR: MergerMechanics.TAXABLE_DISPOSAL,
}


class CzMergerPolicy:
    """``MergerPolicy`` over the recorded Czech regimes.

    The engine asks only what to do with the lots; every §23b/§23c judgement,
    citation and Czech-language message stays on this side of the boundary.
    """

    def __init__(self, store: Optional[MergerTreatmentStore] = None):
        self.store = store if store is not None else MergerTreatmentStore()

    def decide(self, event_key: str) -> MergerDecision:
        record = self.store.get(event_key)
        mechanics = _MECHANICS.get(record.treatment)
        if mechanics is None:
            # Leave a line in the file so the pending decision is visible
            # somewhere other than a log message.
            self.store.record_placeholder(event_key)
            return MergerDecision.undecided(unclassified_message(event_key))
        return MergerDecision(
            mechanics=mechanics,
            label=f"{record.treatment.value} ({record.treatment.citation})",
        )


def unclassified_message(key: str) -> str:
    """The refusal text, naming the key and every allowed value."""
    options = "\n".join(
        f"    {t.value:<22} {t.citation}"
        for t in CzMergerTreatment if t is not CzMergerTreatment.UNCLASSIFIED
    )
    evidence = "\n".join(f"    - {item}" for item in QUALIFIED_EVIDENCE_CHECKLIST)
    return (
        f"Fúze '{key}' nemá určený daňový režim, a nelze ho odvodit z dat brokera "
        f"(rozhoduje daňová rezidence a právní forma zúčastněných společností, "
        f"ne burza ani ISIN).\n"
        f"  Doplňte do {DEFAULT_TREATMENT_CACHE_PATH} položku pro tento klíč "
        f"s jednou z hodnot:\n{options}\n"
        f"  Pro kvalifikovaný režim (§23b/§23c) je potřeba doložit:\n{evidence}"
    )
