"""Opening pull requests with a scoped token rather than whatever `gh` is logged into.

The token lives in the login Keychain, so the thing worth testing without a network is what happens
when it is not there: a missing token must say how to create one, not fail somewhere deeper with a
stack trace the button cannot render.
"""

import pytest

from rfa import github


def test_a_missing_token_says_how_to_make_one():
    with pytest.raises(github.PublishError) as raised:
        github.token({"github": {"keychain_service": "rfa-github-pat-that-does-not-exist"}})
    message = str(raised.value)
    assert "security add-generic-password -s rfa-github-pat-that-does-not-exist -a rfa -w" in message
    assert "Contents and Pull requests" in message  # the scopes, so nobody reaches for a classic PAT


def test_the_looked_up_service_is_the_configured_one():
    """Which Keychain entry is read is configuration, not a constant compiled into the error."""
    with pytest.raises(github.PublishError, match="somewhere-else"):
        github.token({"github": {"keychain_service": "somewhere-else"}})
    with pytest.raises(github.PublishError, match="somewhere-else-again"):
        github.token({"github": {"keychain_service": "somewhere-else-again"}})
