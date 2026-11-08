"""Lamport rounding for paper SOL amounts."""

from decimal import Decimal, ROUND_DOWN, ROUND_UP

from paper.limits import LAMPORT


def D(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TypeError("not a decimal amount")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def lamport_down(value: Decimal) -> Decimal:
    return D(value).quantize(LAMPORT, rounding=ROUND_DOWN)


def lamport_up(value: Decimal) -> Decimal:
    return D(value).quantize(LAMPORT, rounding=ROUND_UP)


def money_str(value: Decimal) -> str:
    return format(lamport_down(D(value)), "f")
