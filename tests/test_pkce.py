import re

from studylife_mcp.pkce import challenge_for, new_pkce_pair

# RFC 7636 appendix B.
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_challenge_matches_rfc_7636_appendix_b_vector() -> None:
    assert challenge_for(RFC_VERIFIER) == RFC_CHALLENGE


def test_new_pair_is_valid_and_consistent() -> None:
    verifier, challenge = new_pkce_pair()

    assert re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", verifier)
    assert "=" not in challenge
    assert challenge == challenge_for(verifier)


def test_new_pair_is_fresh_every_time() -> None:
    assert new_pkce_pair()[0] != new_pkce_pair()[0]
