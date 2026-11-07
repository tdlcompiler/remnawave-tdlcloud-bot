from types import SimpleNamespace

from app.services.receipt_email import user_receipt_email, validate_receipt_email


def test_receipt_address_precedes_account_address():
    user = SimpleNamespace(email='account@example.com', receipt_email='receipt@example.org')
    assert user_receipt_email(user) == 'receipt@example.org'
    user.email = 'new@example.com'
    assert user_receipt_email(user) == 'receipt@example.org'


def test_account_address_is_used_until_receipt_address_is_set():
    user = SimpleNamespace(email='account@example.com', receipt_email=None)
    assert user_receipt_email(user) == 'account@example.com'
    user.email = None
    assert user_receipt_email(user) is None


def test_invalid_receipt_address_is_rejected():
    assert validate_receipt_email('not-an-email') is None
    assert validate_receipt_email(' ') is None
    assert validate_receipt_email('x' * 256) is None
    assert validate_receipt_email('  person@example.com  ') == 'person@example.com'
