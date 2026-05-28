from security import redact_secret, sanitize_exception_text


def test_redact_secret_never_returns_private_key():
    secret = "0x" + "a" * 64
    redacted = redact_secret(secret)

    assert secret not in redacted
    assert redacted.startswith("0xaa")
    assert redacted.endswith("aaaa")
    assert "..." in redacted


def test_sanitize_exception_text_removes_private_key_like_values():
    secret = "0x" + "b" * 64
    text = f"request failed with key={secret} and authorization=Bearer abcdef1234567890"

    sanitized = sanitize_exception_text(text)

    assert secret not in sanitized
    assert "authorization=***" in sanitized
