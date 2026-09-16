from datetime import date

import pytest

from desk import datapack


def test_match_uses_symbol_and_mic_not_exchange_id():
    hits = [
        {"Identifier": 1, "Symbol": "SAPG:xetr", "ExchangeId": "FSE", "CurrencyCode": "EUR"},
        {"Identifier": 2, "Symbol": "SXR8:xetr", "ExchangeId": "XETR_ETF", "CurrencyCode": "EUR"},
        {"Identifier": 3, "Symbol": "MSFT:xnas", "ExchangeId": "NASDAQ", "CurrencyCode": "USD"},
        {"Identifier": 4, "Symbol": "1MSFT:xnas", "ExchangeId": "NASDAQ", "CurrencyCode": "USD"},
        {"Identifier": 5, "Symbol": "MSFT:xmil", "ExchangeId": "MIL", "CurrencyCode": "EUR"},
    ]
    assert datapack.match_instrument(hits, "sxr8", "XETR", "EUR")["Identifier"] == 2
    assert datapack.match_instrument(hits, "SAPG", "xetr", None)["Identifier"] == 1
    assert datapack.match_instrument(hits, "MSFT", "xnas", "USD")["Identifier"] == 3
    with pytest.raises(datapack.InstrumentNotFound):
        datapack.match_instrument(hits, "MSFT", "xetr", "EUR")      # not listed there
    with pytest.raises(datapack.InstrumentNotFound, match="not in USD"):
        datapack.match_instrument(hits, "MSFT", "xmil", "USD")      # wrong currency is fatal


def test_normalise_bars_reads_saxo_mcp_shape_and_sorts():
    raw = {"count": 2, "bars": [
        {"Time": "2026-09-15T00:00:00.000000Z", "Open": 2, "High": 3, "Low": 1, "Close": 2.5, "Volume": 10},
        {"Time": "2026-09-14T00:00:00.000000Z", "Open": 1, "High": 2, "Low": 0.5, "Close": 1.5},
    ]}
    bars = datapack.normalise_bars(raw)
    assert [b["date"] for b in bars] == ["2026-09-14", "2026-09-15"]
    assert bars[0]["volume"] is None and bars[1]["close"] == 2.5


def test_drop_partial_removes_today_only():
    bars = [{"date": "2026-09-15"}, {"date": "2026-09-16"}]
    assert datapack.drop_partial(bars, date(2026, 9, 16)) == [{"date": "2026-09-15"}]


def test_normalise_quote_infoprice_shape():
    raw = {"Quote": {"Bid": 99.9, "Ask": 100.1, "MarketState": "Open"},
           "PriceInfo": {"High": 101, "Low": 99, "PercentChange": 0.5},
           "PriceInfoDetails": {"LastTraded": 100.02}, "LastUpdated": "2026-09-16T15:00:00Z"}
    q = datapack.normalise_quote(raw)
    assert q["mid"] == pytest.approx(100.0)       # no Mid field: derived from bid/ask
    assert q["last"] == 100.02 and q["market_state"] == "Open"
    assert datapack.mark_price(q) == pytest.approx(100.0)
    assert datapack.mark_price({"mid": None, "last": 5.0}) == 5.0
    with pytest.raises(ValueError):
        datapack.mark_price({"mid": None, "last": None})
