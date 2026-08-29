PX_MARKER = "<!-- PX_BLOCKQUOTE_START -->"

# Label shown on the inline enrichment button attached to channel posts.
PX_BUTTON_LABEL = "ספר לי עוד על זה"

# Label for the inline button that opens today's channel headlines page.
TOC_BUTTON_LABEL = "הכותרות של היום"


def toc_page_url(server_base: str, ch: str, key: str) -> str:
    """URL for the today's-headlines page.

    ``ch`` is ``"test"`` or ``"prod"``; ``key`` is the DIGEST_TOC_KEY secret.
    """
    base = server_base.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return f"{base}/digest/today?ch={ch}&k={key}"
