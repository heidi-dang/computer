"""Tests for SkillExperimentScheduler and delta_Q measurement."""

import asyncio
import pytest

from cptr.services.capability_os.skill_experiment import (
    ExperimentVariant,
    VariantResult,
    SkillExperimentScheduler,
    make_experiment_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_scheduler():
    return SkillExperimentScheduler()


def fake_results(variant_success_map, task_refs=None):
    """Build a flat list of VariantResult from {variant_id: [bool, ...]}."""
    if task_refs is None:
        task_refs = ["task-1"]
    results = []
    for vid, successes in variant_success_map.items():
        for i, s in enumerate(successes):
            results.append(
                VariantResult(
                    variant_id=vid,
                    task_ref=task_refs[i % len(task_refs)],
                    success=s,
                )
            )
    return results


# ---------------------------------------------------------------------------
# make_experiment_config
# ---------------------------------------------------------------------------

def test_make_experiment_config_creates_two_variants():
    cfg = make_experiment_config(
        name="test-exp",
        hypothesis="B is better",
        task_refs=["t1", "t2"],
        baseline_skill_id="skill-base",
        candidate_skill_id="skill-cand",
    )
    assert len(cfg.variants) == 2
    variant_ids = {v.variant_id for v in cfg.variants}
    assert variant_ids == {"A", "B"}
    a = next(v for v in cfg.variants if v.variant_id == "A")
    b = next(v for v in cfg.variants if v.variant_id == "B")
    assert a.skill_id == "skill-base"
    assert b.skill_id == "skill-cand"
    assert cfg.experiment_id  # non-empty UUID


# ---------------------------------------------------------------------------
# _compute_delta_q
# ---------------------------------------------------------------------------

def test_compute_delta_q_positive():
    sched = make_scheduler()
    # A: 7/10, B: 9/10 => delta_B ~= 0.2
    results = fake_results(
        {"A": [True] * 7 + [False] * 3, "B": [True] * 9 + [False] * 1},
        task_refs=[f"t{i}" for i in range(10)],
    )
    dq = sched._compute_delta_q(results)
    assert abs(dq["B"] - 0.2) < 1e-6
    assert abs(dq["A"] - 0.0) < 1e-6


def test_compute_delta_q_negative():
    sched = make_scheduler()
    # A: 1.0, B: 0.0 => delta_B = -1.0
    results = fake_results({"A": [True], "B": [False]})
    dq = sched._compute_delta_q(results)
    assert dq["B"] < 0


# ---------------------------------------------------------------------------
# _select_winner
# ---------------------------------------------------------------------------

def test_select_winner_above_threshold():
    sched = make_scheduler()
    dq = {"A": 0.0, "B": 0.15}
    assert sched._select_winner(dq, threshold=0.05) == "B"


def test_select_winner_below_threshold():
    sched = make_scheduler()
    dq = {"A": 0.0, "B": 0.03}
    assert sched._select_winner(dq, threshold=0.05) is None


def test_select_winner_exact_threshold():
    sched = make_scheduler()
    dq = {"A": 0.0, "B": 0.05}
    assert sched._select_winner(dq, threshold=0.05) == "B"


# ---------------------------------------------------------------------------
# _make_recommendation
# ---------------------------------------------------------------------------

def test_make_recommendation_promote():
    sched = make_scheduler()
    dq = {"A": 0.0, "B": 0.15}
    assert sched._make_recommendation(dq, winner="B") == "promote"


def test_make_recommendation_retain():
    sched = make_scheduler()
    # No winner, no negative delta
    dq = {"A": 0.0, "B": 0.03}
    assert sched._make_recommendation(dq, winner=None) == "retain"


def test_make_recommendation_discard():
    sched = make_scheduler()
    # B is worse than A
    dq = {"A": 0.0, "B": -0.1}
    assert sched._make_recommendation(dq, winner=None) == "discard"


# ---------------------------------------------------------------------------
# run_experiment (async)
# ---------------------------------------------------------------------------

def test_run_experiment_returns_valid_result():
    sched = make_scheduler()
    cfg = make_experiment_config(
        name="smoke",
        hypothesis="anything",
        task_refs=["t1"],
        baseline_skill_id="base",
        candidate_skill_id="cand",
    )
    result = asyncio.run(sched.run_experiment(cfg))
    assert result.experiment_id == cfg.experiment_id
    assert result.recommendation in ("promote", "retain", "discard")
    assert len(result.variant_results) == 2  # 2 variants x 1 task


# ---------------------------------------------------------------------------
# Multi-variant: C beats B
# ---------------------------------------------------------------------------

def test_select_winner_multi_variant_c_beats_b():
    sched = make_scheduler()
    # A=baseline(0), B=+0.10, C=+0.20 => C wins
    dq = {"A": 0.0, "B": 0.10, "C": 0.20}
    winner = sched._select_winner(dq, threshold=0.05)
    assert winner == "C"


def test_compute_delta_q_multi_variant():
    sched = make_scheduler()
    # A=0.5 (5/10), B=0.6 (6/10) delta=+0.10, C=0.7 (7/10) delta=+0.20
    # C should win with highest delta above threshold=0.05
    tasks = [f"t{i}" for i in range(10)]
    results = (
        [VariantResult("A", t, i < 5) for i, t in enumerate(tasks)]
        + [VariantResult("B", t, i < 6) for i, t in enumerate(tasks)]
        + [VariantResult("C", t, i < 7) for i, t in enumerate(tasks)]
    )
    dq = sched._compute_delta_q(results)
    assert abs(dq["B"] - 0.10) < 1e-6
    assert abs(dq["C"] - 0.20) < 1e-6
    winner = sched._select_winner(dq, threshold=0.05)
    assert winner == "C"
