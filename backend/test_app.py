"""Тесты чистой логики (без сети): токен-гейт, SSRF-защита, rate-limit."""
from app import token_ok, is_blocked_url, RateLimiter


def test_token_ok_open_mode_when_no_tokens():
    # токены не настроены -> режим открыт (локалка)
    assert token_ok(None, set()) is True


def test_token_ok_valid_and_invalid():
    tokens = {"secret123"}
    assert token_ok("Bearer secret123", tokens) is True
    assert token_ok("bearer secret123", tokens) is True   # регистр схемы не важен
    assert token_ok("Bearer wrong", tokens) is False
    assert token_ok(None, tokens) is False
    assert token_ok("secret123", tokens) is False          # без 'Bearer '


def test_ssrf_blocks_non_http_and_local():
    assert is_blocked_url("file:///etc/passwd") is True
    assert is_blocked_url("ftp://example.com") is True
    assert is_blocked_url("http://localhost/x") is True
    assert is_blocked_url("http://127.0.0.1/x") is True
    assert is_blocked_url("http://169.254.169.254/latest/meta-data") is True  # облачный метадата-эндпоинт
    assert is_blocked_url("http://10.0.0.5/") is True
    assert is_blocked_url("not a url") is True


def test_ssrf_allows_public():
    assert is_blocked_url("https://example.com/page") is False


def test_rate_limiter_window():
    rl = RateLimiter(per_min=2)
    assert rl.allow("ip", now=1000.0) is True
    assert rl.allow("ip", now=1000.5) is True
    assert rl.allow("ip", now=1001.0) is False           # третий за минуту — блок
    assert rl.allow("ip", now=1061.1) is True            # минута прошла — снова можно
    assert rl.allow("other", now=1001.0) is True         # другой IP не затронут
