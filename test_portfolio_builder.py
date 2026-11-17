from asset_store import AssetStore
from portfolio_builder import normalize_weights


def _store():
    s = AssetStore(":memory:")
    s.upsert_asset("AIR.PA", "Airbus", asset_class="Equity", currency="EUR")
    s.upsert_asset("AI.PA", "Air Liquide", asset_class="Equity")
    s.upsert_asset("ELE.PA", "Électricité Test", asset_class="Equity")
    return s


def test_search_rank_and_accents():
    s = _store()
    assert s.search_local("a") == []
    assert [r["symbol"] for r in s.search_local("ai.pa")] == ["AI.PA"]
    assert [r["symbol"] for r in s.search_local("air")] == ["AI.PA", "AIR.PA"]  # préfixe, puis nom alphabétique
    assert [r["symbol"] for r in s.search_local("electricite")] == ["ELE.PA"]
    assert len(s.search_local("air", limit=1)) == 1


def test_upsert_keeps_existing_fields():
    s = _store()
    s.upsert_asset("AIR.PA", "Airbus SE")
    a = s.all_assets()["AIR.PA"]
    assert a["name"] == "Airbus SE" and a["currency"] == "EUR" and a["asset_class"] == "Equity"


def test_portfolios_roundtrip():
    s = _store()
    s.save_portfolio("p", {"AIR.PA": 60.0})
    s.save_portfolio("p", {"AIR.PA": 40.0, "AI.PA": 60.0})
    assert s.load_portfolio("p") == {"AIR.PA": 40.0, "AI.PA": 60.0}
    assert s.list_portfolios() == ["p"]
    s.delete_portfolio("p")
    assert s.load_portfolio("p") is None


def test_normalize_weights():
    assert normalize_weights({"a": 1, "b": 3}, 100.0) == {"a": 25.0, "b": 75.0}
    assert normalize_weights({}) == {} and normalize_weights({"a": 0}) == {}
    assert normalize_weights({"a": 1}, 0) == {}
