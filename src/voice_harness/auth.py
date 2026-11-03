import hmac


def bearer_value(header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    value = header[7:]
    return value if value and not any(char.isspace() for char in value) else None


def matches(secret: str, presented: str | None) -> bool:
    if presented is None:
        return False
    try:
        return hmac.compare_digest(secret.encode("utf-8"), presented.encode("utf-8"))
    except UnicodeEncodeError:
        return False
