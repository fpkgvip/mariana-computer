"""Unit tests for the vault output-redaction module."""

from mariana.vault.redaction import build_redactor


def test_empty_secrets_returns_identity():
    f = build_redactor({})
    assert f("hello") == "hello"


def test_single_secret_redacted():
    f = build_redactor({"OPENAI_API_KEY": "sk-abcdef1234"})
    assert f("the key is sk-abcdef1234, ok?") == "the key is [REDACTED:OPENAI_API_KEY], ok?"


def test_two_secrets_independently_redacted():
    f = build_redactor({
        "OPENAI_API_KEY": "sk-aaaa1111",
        "ANTHROPIC_API_KEY": "sk-ant-bbbb2222",
    })
    out = f("openai=sk-aaaa1111 anthropic=sk-ant-bbbb2222")
    assert "[REDACTED:OPENAI_API_KEY]" in out
    assert "[REDACTED:ANTHROPIC_API_KEY]" in out
    assert "sk-aaaa1111" not in out
    assert "sk-ant-bbbb2222" not in out


def test_short_value_is_skipped_to_avoid_false_positives():
    # 7 chars → below MIN_TOKEN_LEN, so we deliberately do not redact.
    f = build_redactor({"SHORTKEY": "abc1234"})
    assert f("the abc1234 token") == "the abc1234 token"


def test_longer_value_wins_over_shorter_substring():
    f = build_redactor({"FULL": "abcdef1234567", "PART": "abcdef12"})
    # 'abcdef1234567' contains 'abcdef12'; longest must win.
    out = f("token=abcdef1234567")
    assert out == "token=[REDACTED:FULL]"


def test_multiple_occurrences_all_redacted():
    f = build_redactor({"K": "supersecret-token"})
    out = f("supersecret-token / supersecret-token / supersecret-token")
    assert out.count("[REDACTED:K]") == 3
    assert "supersecret-token" not in out


def test_special_regex_characters_in_value_are_escaped():
    f = build_redactor({"WEIRD_KEY": "sk.+*$^abc12345"})
    out = f("xyz sk.+*$^abc12345 xyz")
    assert "[REDACTED:WEIRD_KEY]" in out
    assert "sk.+*$^abc12345" not in out


def test_empty_string_pass_through():
    f = build_redactor({"K": "supersecret-token"})
    assert f("") == ""


def test_no_match_pass_through():
    f = build_redactor({"K": "supersecret-token"})
    assert f("hello world") == "hello world"


def test_too_many_secrets_raises():
    import pytest
    big = {f"K{i}": "x" * 16 for i in range(257)}
    with pytest.raises(ValueError, match="too many secrets"):
        build_redactor(big)
