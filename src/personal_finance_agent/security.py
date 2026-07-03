"""Security helpers for local banking workflows."""

from __future__ import annotations


MAX_FINTS_PIN_LENGTH = 35


def validate_fints_pin(pin: str) -> None:
    """Validate a PIN before handing it to FinTS/AqBanking."""
    if not pin:
        raise ValueError("FinTS PIN must not be empty.")
    if len(pin) > MAX_FINTS_PIN_LENGTH:
        raise ValueError(f"FinTS PIN must be at most {MAX_FINTS_PIN_LENGTH} ASCII characters.")
    try:
        pin.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("FinTS PIN must contain ASCII characters only.") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in pin):
        raise ValueError("FinTS PIN must not contain control characters.")
