PROTOCOL_VERSION = "2.1"


def protocol_tuple(v: str | None) -> tuple[int, int]:
    """``"2.1"`` -> ``(2, 1)``; anything unreadable -> ``(0, 0)``."""
    try:
        major, _, minor = (v or "").partition(".")
        return int(major), int(minor or 0)
    except ValueError:
        return 0, 0


def protocol_compatible(other: str) -> bool:
    """Workers and backends are compatible when their protocol major versions match."""
    try:
        return other.split(".", 1)[0] == PROTOCOL_VERSION.split(".", 1)[0]
    except (AttributeError, IndexError):
        return False
