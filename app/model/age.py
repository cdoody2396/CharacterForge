"""The 20+ structural gate (DECISIONS.md §11, Layer 3).

Age is a value type with **no sub-20 representation**. Under-20 is not
"caught and rejected" downstream — an `Age` object below the minimum cannot
be constructed at all, and the record's age field is typed as `Age`, so no
`CharacterRecord` can hold an under-20 age. The frozen dataclass also means an
`Age` cannot be mutated below the floor after construction.

`MIN_AGE` is the single source of truth; the age option data file advertises
the same floor to the creator UI, but the gate here holds regardless of what
any data file, hand-edit, or caller supplies.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_AGE = 20
# Sanity ceiling — non-negotiable floor is MIN_AGE; the cap only rejects
# obviously-nonsensical values (typos, overflow) and keeps sliders bounded.
MAX_AGE = 10_000


class AgeError(ValueError):
    """Raised when a value cannot be represented as a valid adult Age."""


@dataclass(frozen=True, order=True)
class Age:
    """An age of at least ``MIN_AGE``. Construction is the gate."""

    value: int

    def __post_init__(self) -> None:
        value = self.value
        # bool is an int subclass; reject it explicitly so True/False can't
        # slip through as 1/0.
        if isinstance(value, bool) or not isinstance(value, int):
            raise AgeError(f"age must be a whole number, got {value!r}")
        if value < MIN_AGE:
            raise AgeError(
                f"age {value} is below the minimum of {MIN_AGE}; "
                f"under-{MIN_AGE} characters are unconstructable"
            )
        if value > MAX_AGE:
            raise AgeError(f"age {value} exceeds the maximum of {MAX_AGE}")

    @classmethod
    def coerce(cls, value: object) -> "Age":
        """Build an Age from loosely-typed input (e.g. a JSON number or a
        numeric string), still routing through the same floor. Non-integral
        or unparseable input raises AgeError rather than silently clamping."""
        if isinstance(value, Age):
            return value
        if isinstance(value, bool):
            raise AgeError(f"age must be a whole number, got {value!r}")
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, float):
            if value.is_integer():
                return cls(int(value))
            raise AgeError(f"age must be a whole number, got {value!r}")
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
                return cls(int(text))
            raise AgeError(f"age is not a whole number: {value!r}")
        raise AgeError(f"age must be a whole number, got {value!r}")

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return str(self.value)
