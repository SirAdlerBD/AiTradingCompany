import pytest
from pydantic import ValidationError

from desk.schemas import AnalystView, check_evidence, flatten

FIELDS = {"indicators.last_close": 101.5, "indicators.sma_200": 95.0, "indicators.rsi_14": 61.2,
          "indicators.sma_50_above_sma_200": True, "instrument.currency": "USD", "indicators.sma_20": None}


def view(**over):
    base = dict(stance="favourable", thesis="Price sits above its 200-day average with momentum intact.",
                evidence=[{"field": "indicators.last_close", "value": 101.5, "why": "above the long average"},
                          {"field": "indicators.sma_200", "value": 95.0, "why": "the long average"}],
                confidence=0.6, would_be_wrong_if="indicators.last_close falls below indicators.sma_200",
                horizon_days=60)
    base.update(over)
    return AnalystView.model_validate(base)


def test_flatten_paths():
    assert flatten({"a": {"b": 1}, "c": [{"d": 2}, 3]}) == {"a.b": 1, "c[0].d": 2, "c[1]": 3}


def test_valid_view_passes():
    assert check_evidence(view(), FIELDS) == []


def test_hallucinated_value_is_rejected():
    v = view(evidence=[{"field": "indicators.last_close", "value": 120.0, "why": "current level"},
                       {"field": "indicators.sma_200", "value": 95.0, "why": "the long average"}])
    probs = check_evidence(v, FIELDS)
    assert len(probs) == 1 and "does not match" in probs[0]


def test_unknown_field_is_rejected():
    v = view(evidence=[{"field": "indicators.macd", "value": 1.0, "why": "current level"},
                       {"field": "indicators.sma_200", "value": 95.0, "why": "the long average"}])
    assert "not a FIELDS key" in check_evidence(v, FIELDS)[0]


def test_tolerances_and_types():
    v = view(evidence=[{"field": "indicators.rsi_14", "value": "61.2", "why": "quoted as string"},
                       {"field": "indicators.sma_50_above_sma_200", "value": True, "why": "bool"},
                       {"field": "instrument.currency", "value": "USD", "why": "str"},
                       {"field": "indicators.sma_20", "value": None, "why": "null"},
                       {"field": "indicators.last_close", "value": 101.6, "why": "within 0.5%"}])
    assert check_evidence(v, FIELDS) == []


def test_schema_rejects_bad_shapes():
    with pytest.raises(ValidationError):
        view(stance="bullish")
    with pytest.raises(ValidationError):
        view(evidence=[{"field": "indicators.last_close", "value": 101.5, "why": "only one"}])
    with pytest.raises(ValidationError):
        view(confidence=1.5)
