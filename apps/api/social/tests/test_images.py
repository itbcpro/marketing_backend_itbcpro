import httpx
import pytest

from core.errors import PermanentProviderError, RateLimitedError
from social.images import _raise_for


def test_an_empty_account_is_a_permanent_error_not_a_rate_limit():
    response = httpx.Response(429, text='{"error": {"type": "insufficient_quota"}}')
    with pytest.raises(PermanentProviderError):
        _raise_for(response)


def test_a_plain_too_many_requests_is_a_rate_limit():
    with pytest.raises(RateLimitedError):
        _raise_for(httpx.Response(429, text="slow down"))
