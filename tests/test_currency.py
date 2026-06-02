"""Currency configuration — ISO mapping and URL parameter threading."""
from custom_components.cs2.const import (
    DEFAULT_CURRENCY,
    STEAM_CURRENCIES,
    STEAM_HISTORY_URL,
    STEAM_MARKET_PRICE_URL,
    currency_code,
)


def test_default_currency_is_eur():
    assert DEFAULT_CURRENCY == 3
    assert currency_code(DEFAULT_CURRENCY) == "EUR"


def test_currency_code_known_values():
    assert currency_code(1) == "USD"
    assert currency_code(2) == "GBP"
    assert currency_code(17) == "TRY"


def test_currency_code_unknown_falls_back_to_eur():
    assert currency_code(99999) == "EUR"


def test_market_url_threads_currency():
    url = STEAM_MARKET_PRICE_URL.format(appid=730, name="AK", currency=1)
    assert "currency=1" in url
    assert "&currency=3" not in url


def test_history_url_threads_currency():
    url = STEAM_HISTORY_URL.format(appid=730, name="AK", currency=20)
    assert "currency=20" in url
