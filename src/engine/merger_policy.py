# src/engine/merger_policy.py
"""
How the ledger should treat a stock-for-stock merger — the mechanical question,
asked without knowing anyone's tax law.

There are only two things the FIFO ledger can do with such an event: move the
lots across to the new asset keeping their cost and dates, or close them and
open fresh ones at the consideration's value. Which is correct is a legal
question with different answers per jurisdiction (Czech §23b/§23c vs German
§20 Abs 4a EStG), so it is answered by a ``MergerPolicy`` supplied through the
processor context. This module deliberately holds no citations and no country
strings — see ``src/countries/cz/merger_treatment.py`` for the Czech policy.

The engine never invents a decision: a policy that cannot decide returns
``MergerDecision.undecided(reason)`` and the processor refuses, because both
defaults are wrong in a way that lands in someone's tax return.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Protocol


class MergerMechanics(Enum):
    """What the ledger does with the source lots."""

    #: Transfer the lots to the new asset, preserving cost basis and dates.
    CARRY_OVER = auto()

    #: Close the lots against the consideration's value and open new ones.
    TAXABLE_DISPOSAL = auto()


@dataclass(frozen=True)
class MergerDecision:
    """A policy's answer: mechanics to apply, or a reason it cannot say."""

    mechanics: Optional[MergerMechanics]
    #: Human-readable, caller-facing explanation when ``mechanics`` is None.
    #: Written by the policy so jurisdiction-specific wording stays there.
    reason: str = ""
    #: Short description of what was decided, for logs and audit records.
    label: str = ""

    @classmethod
    def undecided(cls, reason: str) -> "MergerDecision":
        return cls(mechanics=None, reason=reason)

    @property
    def is_decided(self) -> bool:
        return self.mechanics is not None


class MergerPolicy(Protocol):
    """Answers the mechanics question for one merger event."""

    def decide(self, event_key: str) -> MergerDecision:
        ...


def _slug(value: Optional[str]) -> str:
    """Collapse a symbol/id to something safe and readable inside a key."""
    return re.sub(r"[^A-Za-z0-9._-]+", "", (value or "").strip()) or "?"


def merger_event_key(
    action_id: Optional[str],
    event_date: Optional[str],
    old_symbol: Optional[str],
    new_symbol: Optional[str],
) -> str:
    """Stable, human-readable identifier for one merger event.

    Built from the broker's action id plus the date and both symbols. The
    action id alone will not do: a multi-leg corporate action repeats it across
    legs. ``FinancialEvent.event_id`` will not do either — it is a fresh uuid4
    on every run, so a decision keyed on it would be forgotten immediately.

    The symbols are in the key on purpose: whoever records a decision has to be
    able to tell which merger a stored line refers to.
    """
    return (
        f"{_slug(action_id)}|{_slug(event_date)}"
        f"|{_slug(old_symbol)}->{_slug(new_symbol)}"
    )
