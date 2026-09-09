"""Tests for the daily self-improvement loop (model_learner.py)."""

import data.model_learner as learner
from data.config import LEARN_BIAS_CLAMP


def _fake_journal(entries, monkeypatch):
    """Monkeypatch get_journal to return canned settled days (newest first, as
    the real journal does)."""
    def fake():
        return [dict(e) for e in entries]
    monkeypatch.setattr(learner, "get_journal", fake)


class TestSelfTune:
    def test_not_learned_until_min_samples(self, monkeypatch):
        _fake_journal([
            {"predicted_mu": 32.0, "predicted_sigma": 1.0, "actual_max": 33.0},
        ], monkeypatch)
        t = learner.self_tune()
        assert t["source"] == "default"
        assert t["bias"] == 0.0

    def test_bias_uses_all_history_not_just_last_day(self, monkeypatch):
        # 100 settled days with a consistent +0.3C error (below the clamp). The
        # estimate must converge toward +0.3, NOT collapse to only the newest
        # day's error (the pre-fix alpha=1.0 bug).
        entries = [
            {"predicted_mu": 32.0, "predicted_sigma": 1.0, "actual_max": 32.3}
            for _ in range(100)
        ]
        _fake_journal(entries, monkeypatch)
        t = learner.self_tune()
        assert t["source"] == "learned"
        assert abs(t["bias"] - 0.3) < 0.05

    def test_bias_clamped(self, monkeypatch):
        # A single massive outlier must be clamped, not applied raw.
        entries = [
            {"predicted_mu": 32.0, "predicted_sigma": 1.0, "actual_max": 33.0}
            for _ in range(19)
        ]
        entries.append({"predicted_mu": 32.0, "predicted_sigma": 1.0, "actual_max": 99.0})
        _fake_journal(entries, monkeypatch)
        t = learner.self_tune()
        assert abs(t["bias"]) <= LEARN_BIAS_CLAMP

    def test_clim_mean_is_plain_mean(self, monkeypatch):
        actuals = [33.0, 34.0, 32.0, 35.0]
        entries = [
            {"predicted_mu": 32.0, "predicted_sigma": 1.0, "actual_max": a}
            for a in actuals
        ]
        _fake_journal(entries, monkeypatch)
        t = learner.self_tune()
        assert t["clim_mean"] == round(sum(actuals) / len(actuals), 3)

    def test_sigma_floor_keeps_minimum(self, monkeypatch):
        # Tightly clustered actuals still floor the uncertainty at 0.5.
        entries = [
            {"predicted_mu": 32.0, "predicted_sigma": 1.0, "actual_max": 32.0}
            for _ in range(20)
        ]
        _fake_journal(entries, monkeypatch)
        t = learner.self_tune()
        assert t["sigma_floor"] >= 0.5