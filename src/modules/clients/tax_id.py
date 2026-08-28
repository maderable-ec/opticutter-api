"""Ecuadorian cédula / RUC validation.

Pure functions, no exceptions of their own: the caller decides whether a bad
number is a 422 (the dashboard form) or a skipped row in a sync report. That is
why this returns a *message* instead of raising — ``client_sync`` needs the text
to put in ``issues[]``, and Pydantic needs it to put in a ``ValueError``.

The check digit is the point: a mistyped cédula is accepted by any
length-only rule and then never matches the client's real record in SIFAC, the
SRI, or anywhere else.
"""

from __future__ import annotations

from typing import Optional, Tuple

_LENGTH_MSG = (
    "Identificador inválido: '{value}'. Debe ser una cédula (10 dígitos) o "
    "un RUC (13 dígitos) ecuatoriano."
)
_CHECK_MSG = (
    "Identificador inválido: '{value}'. El número no corresponde a una cédula "
    "o RUC ecuatoriano válido."
)

# Provinces 1-24, plus 30 for Ecuadorians registered abroad.
_VALID_PROVINCES = frozenset(range(1, 25)) | {30}

# Third digit of a 13-digit RUC: 6 = public entity, 9 = private company.
# Anything else is a natural person's RUC (their cédula plus an establishment
# suffix), which validates with the cédula algorithm.
_PUBLIC_DIGIT = 6
_PRIVATE_DIGIT = 9

_NATURAL_COEFFICIENTS = (2, 1, 2, 1, 2, 1, 2, 1, 2)
_PUBLIC_COEFFICIENTS = (3, 2, 7, 6, 5, 4, 3, 2)
_PRIVATE_COEFFICIENTS = (4, 3, 2, 7, 6, 5, 4, 3, 2)


def _kinds(value: str) -> Tuple[bool, bool]:
    """``(is_public, is_private)`` for a 13-digit RUC; ``(False, False)`` else."""
    if len(value) != 13:
        return False, False
    third = int(value[2])
    return third == _PUBLIC_DIGIT, third == _PRIVATE_DIGIT


def is_company_ruc(value: str) -> bool:
    """Whether this is a *company's* RUC rather than a person's.

    Only the third digit separates them: a natural person with a RUC is their
    own cédula plus an establishment suffix, so 13 digits alone says nothing.
    Used by the client sync to decide whether a name is a person's (split into
    first/last) or an organization's (kept whole).
    """
    if not value or not value.isdigit():
        return False
    is_public, is_private = _kinds(value)
    return is_public or is_private


def tax_id_error(value: str) -> Optional[str]:
    """The reason ``value`` isn't a valid cédula/RUC, or ``None`` if it is."""
    if not isinstance(value, str) or not value.isdigit():
        return _LENGTH_MSG.format(value=value)

    if len(value) not in (10, 13):
        return _LENGTH_MSG.format(value=value)

    if int(value[:2]) not in _VALID_PROVINCES:
        return _CHECK_MSG.format(value=value)

    is_public, is_private = _kinds(value)
    is_natural = not (is_public or is_private)

    if is_public:
        coefficients = _PUBLIC_COEFFICIENTS
    elif is_private:
        coefficients = _PRIVATE_COEFFICIENTS
    else:
        coefficients = _NATURAL_COEFFICIENTS

    # The check digit sits right after the coefficients it is computed from:
    # position 9 for a cédula/natural RUC, 8 for public, 9 for private.
    checker = int(value[len(coefficients)])
    base = 10 if is_natural else 11

    total = 0
    for index, coefficient in enumerate(coefficients):
        product = int(value[index]) * coefficient
        if is_natural:
            # Digit sum of the product, which for a one-digit multiplier is
            # the same as subtracting 9.
            total += product if product < 10 else (product // 10) + (product % 10)
        else:
            total += product

    remainder = total % base
    expected = base - remainder if remainder != 0 else 0

    if expected != checker:
        return _CHECK_MSG.format(value=value)
    return None


def identifier_error(value: str) -> Optional[str]:
    """The rule the dashboard's client form and API enforce.

    An all-digits identifier is held to the full cédula/RUC algorithm; anything
    carrying a letter is taken as a foreign document (passport) and accepted as
    typed. The split is deliberate: nine digits is a mistyped cédula, not a
    passport, so it must still fail.
    """
    if not value:
        return None
    if value.isdigit():
        return tax_id_error(value)
    return None
