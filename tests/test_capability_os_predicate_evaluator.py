"""Tests for the safe bounded predicate evaluator."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cptr.services.capability_os.predicate_evaluator import (
    PredicateContext,
    PredicateError,
    PredicateEvaluator,
)

ctx = PredicateContext(
    inputs={'count': 10, 'path': '/safe/dir'},
    outputs={'status': 'ok', 'code': 200},
    effects_applied=['tool.invoke', 'read'],
    metadata={'env': 'prod'},
)

evaluator = PredicateEvaluator()


def test_inputs_count_gt_5():
    assert evaluator.evaluate('inputs.count > 5', ctx) is True


def test_inputs_count_lt_5():
    assert evaluator.evaluate('inputs.count < 5', ctx) is False


def test_outputs_status_eq_ok():
    assert evaluator.evaluate('outputs.status == "ok"', ctx) is True


def test_and_compound():
    assert evaluator.evaluate('inputs.count > 5 and outputs.status == "ok"', ctx) is True


def test_in_effects_applied_read():
    assert evaluator.evaluate('"read" in effects_applied', ctx) is True


def test_not_in_effects_applied_write():
    assert evaluator.evaluate('"write" in effects_applied', ctx) is False


def test_string_method_startswith():
    assert evaluator.evaluate('inputs.path.startswith("/safe")', ctx) is True


def test_empty_expression():
    assert evaluator.evaluate('', ctx) is True


def test_arithmetic():
    assert evaluator.evaluate('inputs.count * 2 == 20', ctx) is True


def test_outputs_code_eq_200():
    assert evaluator.evaluate('outputs.code == 200', ctx) is True


def test_metadata_env_eq_prod():
    assert evaluator.evaluate('metadata.env == "prod"', ctx) is True


def test_blocked_import():
    with pytest.raises(PredicateError):
        evaluator.evaluate('__import__("os").system("id")', ctx)


def test_blocked_open():
    with pytest.raises(PredicateError):
        evaluator.evaluate('open("/etc/passwd")', ctx)


def test_blocked_list_comprehension():
    with pytest.raises(PredicateError):
        evaluator.evaluate('[x for x in range(10)]', ctx)


def test_blocked_lambda():
    with pytest.raises(PredicateError):
        evaluator.evaluate('(lambda: 42)()', ctx)


def test_syntax_error():
    with pytest.raises(PredicateError, match='Syntax error'):
        evaluator.evaluate('inputs.count >', ctx)
