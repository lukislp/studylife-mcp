"""Self-hosted DM Sans for the pages this server renders (sign-in, consent, connected apps).

The pages used to load DM Sans from Google's font servers, which sent every visitor's IP
address to a third party (GDPR audit 2026-11-19, S11). The woff2 files now live in
``studylife_mcp/fonts`` and are served by this process, so rendering a page makes no
request to any host other than this server.

The fonts are licensed under the SIL Open Font License 1.1; the notice ships next to the
files (``fonts/OFL.txt``).

The route lives under ``/auth/consent/fonts`` on purpose: the public ingress allowlist
(``k8s/07-tailscale-funnel.yaml``) already forwards the ``/auth/consent`` prefix, so no
ingress change is needed for the consent page to get its font.
"""

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import Response

FONT_DIR = Path(__file__).parent / "fonts"
FONT_URL_PREFIX = "/auth/consent/fonts"
FONT_CONTENT_TYPE = "font/woff2"

# Same unicode-range split as Google's CSS API (and the StudyAtrium web client) so a page
# in a Latin-script language downloads only the small latin subset.
_LATIN = (
    "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, "
    "U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, "
    "U+FEFF, U+FFFD"
)
_LATIN_EXT = (
    "U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, "
    "U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, "
    "U+2113, U+2C60-2C7F, U+A720-A7FF"
)

# (file name, font-style, font-weight, unicode-range). The upright files are variable
# fonts covering weights 300-500; only the 300 italic cut is used by the pages.
_FACES: tuple[tuple[str, str, str, str], ...] = (
    ("dmsans-300-italic-latin-ext.woff2", "italic", "300", _LATIN_EXT),
    ("dmsans-300-italic-latin.woff2", "italic", "300", _LATIN),
    ("dmsans-normal-latin-ext.woff2", "normal", "300 500", _LATIN_EXT),
    ("dmsans-normal-latin.woff2", "normal", "300 500", _LATIN),
)

FONT_FILES: frozenset[str] = frozenset(face[0] for face in _FACES)


def font_face_css() -> str:
    """The ``@font-face`` rules for DM Sans, pointing at this server's own font route."""
    return "\n".join(
        f"@font-face {{ font-family: 'DM Sans'; font-style: {style}; font-weight: {weight}; "
        f"font-display: swap; src: url('{FONT_URL_PREFIX}/{name}') format('woff2'); "
        f"unicode-range: {unicode_range}; }}"
        for name, style, weight, unicode_range in _FACES
    )


def register_font_route(mcp: MCPServer) -> None:
    """Serves the vendored font files. Only the names in ``FONT_FILES`` resolve, so the
    path parameter can never reach outside the font directory."""

    @mcp.custom_route(FONT_URL_PREFIX + "/{filename}", methods=["GET"])  # type: ignore[untyped-decorator]
    async def serve_font(request: Request) -> Response:
        filename = request.path_params["filename"]
        if filename not in FONT_FILES:
            return Response(status_code=404)
        return Response(
            (FONT_DIR / filename).read_bytes(),
            media_type=FONT_CONTENT_TYPE,
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )
