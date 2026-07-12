"""The 20+ structural gate (DECISIONS.md §11 Layer 3). Under-20 must be
UNCONSTRUCTABLE, not merely rejected downstream."""

import pytest

from app.model.age import MAX_AGE, MIN_AGE, Age, AgeError


def test_minimum_is_twenty():
    assert MIN_AGE == 20


@pytest.mark.parametrize("value", [20, 21, 25, 40, 99, 120, MAX_AGE])
def test_valid_ages_construct(value):
    assert int(Age(value)) == value


@pytest.mark.parametrize("value", [19, 18, 15, 1, 0, -1, -100])
def test_sub_minimum_ages_cannot_be_constructed(value):
    with pytest.raises(AgeError):
        Age(value)


def test_above_ceiling_rejected():
    with pytest.raises(AgeError):
        Age(MAX_AGE + 1)


def test_bool_is_not_an_age():
    # bool is an int subclass — must not sneak through as 1/0.
    with pytest.raises(AgeError):
        Age(True)
    with pytest.raises(AgeError):
        Age(False)


def test_non_int_rejected():
    for bad in (20.5, "twenty", None, [20]):
        with pytest.raises(AgeError):
            Age(bad)  # type: ignore[arg-type]


def test_frozen_cannot_be_mutated_below_floor():
    age = Age(25)
    with pytest.raises(Exception):
        age.value = 15  # type: ignore[misc]


def test_coerce_from_int_float_str():
    assert int(Age.coerce(30)) == 30
    assert int(Age.coerce(30.0)) == 30
    assert int(Age.coerce("30")) == 30
    assert int(Age.coerce(Age(30))) == 30


def test_coerce_still_enforces_floor():
    with pytest.raises(AgeError):
        Age.coerce(15)
    with pytest.raises(AgeError):
        Age.coerce("15")
    with pytest.raises(AgeError):
        Age.coerce(19.0)


def test_coerce_rejects_non_integral_float():
    with pytest.raises(AgeError):
        Age.coerce(25.5)


def test_ordering_and_equality():
    assert Age(20) < Age(21)
    assert Age(30) == Age(30)
    assert sorted([Age(40), Age(20), Age(25)]) == [Age(20), Age(25), Age(40)]
