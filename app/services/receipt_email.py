"""Receipt addresses are independent from authentication and notifications."""

from email_validator import EmailNotValidError, validate_email


def validate_receipt_email(value: str | None) -> str | None:
    if not value or len(value.strip()) > 255:
        return None
    try:
        normalized = validate_email(value.strip(), check_deliverability=False).normalized
        return normalized if len(normalized) <= 255 else None
    except EmailNotValidError:
        return None


def user_receipt_email(user) -> str | None:
    return validate_receipt_email(getattr(user, 'receipt_email', None) or getattr(user, 'email', None))
