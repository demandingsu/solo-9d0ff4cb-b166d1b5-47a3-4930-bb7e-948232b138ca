"""Exact rational arithmetic for packaging-unit conversion factors.

All factors are stored as pairs of positive integers (numerator, denominator)
and reduced by GCD so that equivalent factors compare equal exactly.
"""

from __future__ import annotations

from math import gcd
from typing import Tuple

# A rational factor as (numerator, denominator), both positive.
Fraction = Tuple[int, int]


def reduce_frac(num: int, den: int) -> Fraction:
    """Return the reduced, sign-checked fraction ``num / den``."""
    if num <= 0 or den <= 0:
        raise ValueError("fraction parts must be positive integers")
    g = gcd(num, den)
    return num // g, den // g


def mul(a: Tuple[int, int], b: Tuple[int, int]) -> Tuple[int, int]:
    return reduce_frac(a[0] * b[0], a[1] * b[1])


def div(a: Tuple[int, int], b: Tuple[int, int]) -> Tuple[int, int]:
    return reduce_frac(a[0] * b[1], a[1] * b[0])


def invert(a: Tuple[int, int]) -> Tuple[int, int]:
    return a[1], a[0]


def frac_str(a: Tuple[int, int]) -> str:
    return f"{a[0]}/{a[1]}"
