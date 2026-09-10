"""Conservative port identity matching; unknown codes never get guessed."""

import re
import unicodedata


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


_ALIASES = {
    "gtprq": "puerto quetzal gt",
    "puerto quetzal": "puerto quetzal gt",
    "puerto quetzal guatemala": "puerto quetzal gt",
    "puerto quetzal gt": "puerto quetzal gt",
    "hnpcr": "puerto cortes hn",
    "puerto cortes": "puerto cortes hn",
    "puerto cortes honduras": "puerto cortes hn",
    "puerto cortes hn": "puerto cortes hn",
    "pacon": "colon pa",
    "colon panama": "colon pa",
    "colon pa": "colon pa",
}


def same_port(destination: str | None, event_location: str | None) -> bool:
    if not destination or not event_location:
        return False
    # Do not interpret lists of alternative ports as one location.
    if any(mark in value for value in (destination, event_location) for mark in ("/", ";", "|")):
        return False
    left, right = _normalize(destination), _normalize(event_location)
    if left in {"", "unknown", "n a", "tbd"} or right in {"", "unknown", "n a", "tbd"}:
        return False
    left, right = _ALIASES.get(left, left), _ALIASES.get(right, right)
    # Unknown bare names, dropdown indices and unrecognized codes are ambiguous.
    if not re.fullmatch(r"[a-z][a-z0-9 ]+ [a-z]{2}", left):
        return False
    return left == right
