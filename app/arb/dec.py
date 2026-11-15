"""Small Decimal helpers for order sizes and prices."""
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from fractions import Fraction
from math import gcd


def D(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


def floor_to(x, step):
    x, step = D(x), D(step)
    if step <= 0:
        return x
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to(x, step):
    x, step = D(x), D(step)
    if step <= 0:
        return x
    return (x / step).to_integral_value(rounding=ROUND_UP) * step


def lcm_step(a, b):
    """Smallest step that is a multiple of both a and b."""
    fa, fb = Fraction(str(D(a))), Fraction(str(D(b)))
    num = fa.numerator * fb.numerator // gcd(fa.numerator, fb.numerator)
    den = gcd(fa.denominator, fb.denominator)
    return Decimal(num) / Decimal(den)


def fmt(x):
    """Decimal -> plain string with no exponent and no trailing zeros."""
    s = format(D(x), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
