# src/countries/cz/enums.py
"""
Czech Republic tax reporting categories.

Maps to sections of the Czech personal income tax return
(Přiznání k dani z příjmů fyzických osob):

- §8 ZDP — Příjmy z kapitálového majetku (capital income)
  - Dividendy, úroky
- §10 ZDP — Ostatní příjmy (other income)
  - Prodej cenných papírů, opce, deriváty

Note: These are placeholder categories. The final mapping to specific
form lines (Příloha č. 2, etc.) requires detailed Czech tax law review.
"""
from enum import Enum, auto


class CzTaxSection(Enum):
    """High-level Czech tax form sections for income classification.

    Exemption status is tracked per-item on ``CzTaxItem.is_exempt``,
    NOT as a separate section value.
    """

    # §8 ZDP — Příjmy z kapitálového majetku
    CZ_8_DIVIDENDS = auto()      # Dividendy ze zahraničí
    CZ_8_INTEREST = auto()       # Úroky ze zahraničí

    # §10 ZDP — Ostatní příjmy
    CZ_10_SECURITIES = auto()    # Prodej cenných papírů (akcie, dluhopisy, fondy)
    CZ_10_OPTIONS = auto()       # Opce a deriváty


def category_to_cz_section(asset_category_name: str) -> CzTaxSection:
    """Map a core ``AssetCategory`` name to a ``CzTaxSection``.

    Single source of truth — used by both the classifier and item builder.
    Accepts the enum *name* (string) to avoid importing ``AssetCategory``
    into this low-level enum module.
    """
    _MAP = {
        "STOCK": CzTaxSection.CZ_10_SECURITIES,
        "BOND": CzTaxSection.CZ_10_SECURITIES,
        "INVESTMENT_FUND": CzTaxSection.CZ_10_SECURITIES,
        "OPTION": CzTaxSection.CZ_10_OPTIONS,
        "CFD": CzTaxSection.CZ_10_OPTIONS,
        "PRIVATE_SALE_ASSET": CzTaxSection.CZ_10_SECURITIES,
    }
    return _MAP.get(asset_category_name, CzTaxSection.CZ_10_SECURITIES)


def category_requires_manual_review(asset_category_name: str) -> bool:
    """Categories whose CZ treatment must not be assumed silently.

    PRIVATE_SALE_ASSET (Gold-ETC, crypto-ETP, …) and unknown categories may
    not be securities at all — the §4/1/w time test and the 100k annual
    limit apply only to securities, so such items must go through manual
    review instead of receiving the exemptions by a silent default.
    """
    return asset_category_name not in (
        "STOCK", "BOND", "INVESTMENT_FUND", "OPTION", "CFD",
    )
