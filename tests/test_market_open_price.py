import json
import urllib.parse

import market


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()


def test_fetch_crypto_open_price_uses_polymarket_crypto_price_endpoint(monkeypatch):
    requested = {}

    def fake_urlopen(req, timeout):
        requested["url"] = req.full_url
        requested["timeout"] = timeout
        return FakeResponse({"openPrice": 75769.18, "closePrice": 75656, "completed": False})

    monkeypatch.setattr(market.urllib.request, "urlopen", fake_urlopen)

    price = market.fetch_crypto_open_price(symbol="btc", event_start_time=1779883800)

    assert price == 75769.18
    parsed = urllib.parse.urlparse(requested["url"])
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "polymarket.com"
    assert parsed.path == "/api/crypto/crypto-price"
    assert params == {
        "symbol": ["BTC"],
        "eventStartTime": ["2026-05-27T12:10:00Z"],
        "variant": ["fiveminute"],
        "endDate": ["2026-05-27T12:15:00Z"],
    }
    assert requested["timeout"] == 5


def test_get_current_market_populates_official_open_price(monkeypatch):
    event = {
        "markets": [
            {
                "conditionId": "0xabc",
                "clobTokenIds": json.dumps(["up-token", "down-token"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "outcomePrices": json.dumps(["0.52", "0.48"]),
            }
        ]
    }

    monkeypatch.setattr(market, "current_window_ts", lambda period_minutes=5: 1779883800)
    monkeypatch.setattr(market, "fetch_market_by_slug", lambda slug: event)
    monkeypatch.setattr(
        market,
        "fetch_crypto_open_price",
        lambda symbol, event_start_time: 75769.18,
    )

    current = market.get_current_market(5)

    assert current.opening_price == 75769.18
    assert current.window_start == 1779883800
