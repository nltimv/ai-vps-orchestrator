PROTOCOL_VERSION = "2.0"


def protocol_compatible(other: str) -> bool:
    """Workers and backends are compatible when their protocol major versions match."""
    try:
        return other.split(".", 1)[0] == PROTOCOL_VERSION.split(".", 1)[0]
    except (AttributeError, IndexError):
        return False
