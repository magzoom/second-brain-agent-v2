"""
Personal Finance helper for SBA 2.0.

- Account alias resolution (user phrases → internal account names)
- Gold price fetching (metals.live + nationalbank.kz)
- Zakat calculation
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

NISAB_GOLD_GRAMS = 85.0
TROY_OZ_TO_GRAM = 31.1035

# Maps user phrases (lowercased) to internal account names.
# Customize these for your own banks — add any phrases you use naturally.
ACCOUNT_ALIASES: dict[str, str] = {
    # account_main
    "основной":      "account_main",
    "main":          "account_main",
    "account_main":  "account_main",
    "kaspi":         "account_main",
    "каспи":         "account_main",
    "kaspi main":    "account_main",
    # account_2
    "второй":        "account_2",
    "second":        "account_2",
    "второй счёт":   "account_2",
    "account_2":     "account_2",
    "kaspi second":  "account_2",
    "второй каспи":  "account_2",
    # account_3
    "account_3":     "account_3",
    "freedom":       "account_3",
    "фридом":        "account_3",
    # account_4
    "account_4":     "account_4",
    "halyk":         "account_4",
    "халык":         "account_4",
    # account_5
    "account_5":     "account_5",
    "rbk":           "account_5",
    "рбк":           "account_5",
    # account_biz
    "бизнес":        "account_biz",
    "business":      "account_biz",
    "account_biz":   "account_biz",
    # account_otbasy
    "отбасы":        "account_otbasy",
    "отбасыбанк":    "account_otbasy",
    "otbasy":        "account_otbasy",
    "account_otbasy": "account_otbasy",
}

LIABILITY_ALIASES: dict[str, str] = {
    "долги людям":        "people_debt",
    "долг людям":         "people_debt",
    "people_debt":        "people_debt",
    "рассрочка":          "kaspi_installment",
    "рассрочка каспи":    "kaspi_installment",
    "kaspi_installment":  "kaspi_installment",
    "налог транспорт":    "transport_tax",
    "транспортный налог": "transport_tax",
    "transport_tax":      "transport_tax",
}


def resolve_account(name: str) -> str:
    """Resolve user phrase to internal account name."""
    return ACCOUNT_ALIASES.get(name.lower().strip(), name.lower().strip())


def resolve_liability(name: str) -> str:
    """Resolve user phrase to internal liability name."""
    return LIABILITY_ALIASES.get(name.lower().strip(), name.lower().strip())


async def _yahoo_price(session, ticker: str) -> float:
    """Fetch regularMarketPrice from Yahoo Finance chart API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    async with session.get(url, headers=headers) as resp:
        data = await resp.json()
        return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])


async def fetch_gold_price_kzt() -> Optional[float]:
    """Fetch current gold price in KZT per gram via Yahoo Finance. Returns None on failure."""
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            # Gold futures in USD per troy ounce (GC=F)
            gold_usd_oz = await _yahoo_price(session, "GC%3DF")
            # USD/KZT exchange rate (KZT=X means 1 USD = X KZT)
            usd_kzt = await _yahoo_price(session, "KZT%3DX")

        price = (gold_usd_oz * usd_kzt) / TROY_OZ_TO_GRAM
        logger.info(f"Gold: ${gold_usd_oz:.2f}/oz × {usd_kzt:.2f} KZT/USD = {price:.0f} KZT/g")
        return price
    except Exception as e:
        logger.warning(f"fetch_gold_price_kzt failed: {e}")
        return None


def nisab_kzt(gold_price_per_gram: float) -> float:
    return NISAB_GOLD_GRAMS * gold_price_per_gram


async def calculate_zakat_status(db) -> dict:
    """
    Calculate current zakat status from DB.
    Returns dict: obligatory, reason, cash_assets, total_liabilities,
                  net_assets, nisab_kzt, amount_due, gold_price_per_gram.
    """
    accounts = await db.fin_get_accounts()
    liabilities = await db.fin_get_liabilities()
    profile = await db.fin_get_zakat_profile()

    cash_assets = sum(a["balance"] for a in accounts if a["balance"] > 0)
    total_liabilities = sum(l["amount"] for l in liabilities if l.get("is_active", 1))
    net_assets = cash_assets - total_liabilities

    fetched_price = await fetch_gold_price_kzt()
    price_is_stale = fetched_price is None
    gold_price = fetched_price or 80000.0  # fallback ~6.8M/85g
    nisab = nisab_kzt(gold_price)
    if price_is_stale:
        logger.warning("Yahoo Finance недоступен, используется устаревший fallback курс золота 80000 ₸/г")

    obligatory = net_assets >= nisab
    amount_due = net_assets * 0.025 if obligatory else 0.0

    if net_assets <= 0:
        reason = f"Долги ({total_liabilities:,.0f} ₸) превышают денежные активы ({cash_assets:,.0f} ₸)"
    elif not obligatory:
        reason = f"Чистые активы {net_assets:,.0f} ₸ < нисаб {nisab:,.0f} ₸"
    else:
        reason = f"Чистые активы {net_assets:,.0f} ₸ ≥ нисаб {nisab:,.0f} ₸ — закят обязателен"

    return {
        "obligatory": obligatory,
        "reason": reason,
        "cash_assets": cash_assets,
        "total_liabilities": total_liabilities,
        "net_assets": net_assets,
        "nisab_kzt": nisab,
        "gold_price_per_gram": gold_price,
        "amount_due": amount_due,
        "price_is_stale": price_is_stale,
    }
