"""Unit tests for ``_apply_limit`` reconciling a caller-written LIMIT.

``run_sql`` always passes a row cap, so SQL that already ends in its own
``LIMIT`` used to become ``LIMIT 20\\nLIMIT 21`` and fail with MySQL error
1064. These tests pin the reconciliation rules; none of them need a server.
"""

from __future__ import annotations

import pytest

from wren.connector.mysql import _apply_limit

pytestmark = pytest.mark.unit


def test_appends_when_there_is_no_limit() -> None:
    assert _apply_limit("SELECT a FROM t", 10) == "SELECT a FROM t\nLIMIT 10"


def test_strips_trailing_semicolon_before_appending() -> None:
    assert _apply_limit("SELECT a FROM t;", 10) == "SELECT a FROM t\nLIMIT 10"


def test_does_not_append_a_second_limit() -> None:
    """The regression: two LIMIT clauses are a syntax error (1064)."""
    result = _apply_limit("SELECT a FROM t LIMIT 20", 21)
    assert result.upper().count("LIMIT") == 1, f"two LIMIT clauses in {result!r}"


def test_tighter_user_limit_is_kept() -> None:
    """A user asking for fewer rows than the cap must get exactly those."""
    assert _apply_limit("SELECT a FROM t LIMIT 20", 1001) == "SELECT a FROM t LIMIT 20"


def test_looser_user_limit_is_lowered_to_the_cap() -> None:
    assert (
        _apply_limit("SELECT a FROM t LIMIT 5000", 1001) == "SELECT a FROM t LIMIT 1001"
    )


def test_equal_limits_leave_the_sql_untouched() -> None:
    assert _apply_limit("SELECT a FROM t LIMIT 10", 10) == "SELECT a FROM t LIMIT 10"


def test_offset_comma_form_keeps_its_offset() -> None:
    """``LIMIT offset, count`` -- only the count may be lowered."""
    assert _apply_limit("SELECT a FROM t LIMIT 10, 5000", 100) == (
        "SELECT a FROM t LIMIT 10, 100"
    )


def test_offset_comma_form_tighter_is_kept() -> None:
    assert (
        _apply_limit("SELECT a FROM t LIMIT 10, 5", 100)
        == "SELECT a FROM t LIMIT 10, 5"
    )


def test_offset_keyword_form_keeps_its_offset() -> None:
    assert _apply_limit("SELECT a FROM t LIMIT 5000 OFFSET 40", 100) == (
        "SELECT a FROM t LIMIT 100 OFFSET 40"
    )


def test_offset_keyword_form_tighter_is_kept() -> None:
    sql = "SELECT a FROM t LIMIT 5 OFFSET 40"
    assert _apply_limit(sql, 100) == sql


def test_lowercase_limit_is_recognised() -> None:
    result = _apply_limit("select a from t limit 5000", 100)
    assert result.lower().count("limit") == 1
    assert result.endswith("LIMIT 100")


def test_limit_inside_a_subquery_is_not_touched() -> None:
    """Only a LIMIT terminating the statement is the outer row cap."""
    sql = "SELECT a FROM (SELECT a FROM t LIMIT 5) x"
    assert _apply_limit(sql, 100) == f"{sql}\nLIMIT 100"


def test_inner_and_outer_limits_are_both_respected() -> None:
    sql = "SELECT a FROM (SELECT a FROM t LIMIT 5) x LIMIT 4000"
    result = _apply_limit(sql, 100)
    assert result == "SELECT a FROM (SELECT a FROM t LIMIT 5) x LIMIT 100"
    assert "LIMIT 5" in result, "the subquery's own limit must survive"


def test_limit_inside_a_string_literal_is_not_treated_as_a_clause() -> None:
    """A literal closes with a quote, so it cannot terminate the statement."""
    sql = "SELECT a FROM t WHERE note = 'see LIMIT 5'"
    assert _apply_limit(sql, 100) == f"{sql}\nLIMIT 100"


def test_semicolon_after_an_existing_limit() -> None:
    assert (
        _apply_limit("SELECT a FROM t LIMIT 5000;", 100) == "SELECT a FROM t LIMIT 100"
    )


def test_zero_cap_overrides_any_existing_limit() -> None:
    assert _apply_limit("SELECT a FROM t LIMIT 10", 0) == "SELECT a FROM t LIMIT 0"
