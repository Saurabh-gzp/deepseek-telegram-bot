"""
Markdown → Telegram HTML converter.

Telegram HTML supports only these tags:
  <b>, <i>, <u>, <s>, <code>, <pre>, <a href>, <blockquote>, <tg-spoiler>
No <br>, no <p>, no <h1>...<h6>, no <ul>/<li>.

We convert DeepSeek's markdown output to safe Telegram HTML.
"""
import re
import html as _html

_PLACEHOLDER_FENCED = "\x00F{}\x00"
_PLACEHOLDER_INLINE = "\x00I{}\x00"
_PLACEHOLDER_LINK   = "\x00L{}\x00"


def md_to_tg_html(text: str) -> str:
    if not text:
        return ""

    fenced_blocks: list[str] = []
    inline_codes: list[str] = []
    links: list[tuple[str, str]] = []

    # 1. Extract fenced code blocks ```lang\n...\n```
    def _fenced(m):
        lang = (m.group(1) or "").strip()
        body = m.group(2)
        body_escaped = _html.escape(body)
        if lang:
            block = (f'<pre><code class="language-{_html.escape(lang)}">'
                     f'{body_escaped}</code></pre>')
        else:
            block = f"<pre>{body_escaped}</pre>"
        fenced_blocks.append(block)
        return _PLACEHOLDER_FENCED.format(len(fenced_blocks) - 1)

    text = re.sub(r"```(\w+)?\n?([\s\S]*?)```", _fenced, text)

    # 2. Extract inline code `...`
    def _inline(m):
        body = _html.escape(m.group(1))
        inline_codes.append(f"<code>{body}</code>")
        return _PLACEHOLDER_INLINE.format(len(inline_codes) - 1)

    text = re.sub(r"`([^`\n]+?)`", _inline, text)

    # 3. Extract links [text](url)
    def _link(m):
        label = m.group(1)
        url = m.group(2)
        links.append((label, url))
        return _PLACEHOLDER_LINK.format(len(links) - 1)

    text = re.sub(r"\[([^\]\n]+?)\]\(([^)\s]+)\)", _link, text)

    # 4. Escape HTML entities in the remaining text
    text = _html.escape(text)

    # 5. Headings (# .. ######) → bold + newline
    def _heading(m):
        return f"<b>{m.group(2).strip()}</b>"
    text = re.sub(r"^(#{1,6})\s+(.+?)\s*$", _heading, text, flags=re.MULTILINE)

    # 6. Bold: **text** or __text__  (non-greedy, must have content)
    #    Handle bold BEFORE italic so ** doesn't get eaten as two *
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text,
                  flags=re.DOTALL)

    # 7. Italic: *text* or _text_  (avoid matching remaining ** or __)
    text = re.sub(r"(?<![\*\w])\*(?!\s)(?!\*)([^\*\n]+?)(?<!\s)\*(?!\*)",
                  r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)",
                  r"<i>\1</i>", text)

    # 8. Strikethrough ~~text~~
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # 9. List bullets: -, *, + at line start → "• "
    text = re.sub(r"^(\s{0,3})[-*+]\s+", r"\1• ", text, flags=re.MULTILINE)

    # 10. Numbered lists: keep as-is (they already look fine)

    # 11. Horizontal rules --- or ***
    text = re.sub(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$", "─────", text, flags=re.MULTILINE)

    # 12. Blockquotes: lines starting with "> " (after escape, > became &gt;)
    def _quotes(match):
        lines = match.group(0).splitlines()
        inner = "\n".join(re.sub(r"^\s*&gt;\s?", "", ln) for ln in lines)
        return f"<blockquote>{inner}</blockquote>"
    text = re.sub(r"(?:^\s*&gt;\s?.*(?:\n|$))+", _quotes, text, flags=re.MULTILINE)

    # 13. Restore links (label may contain already-processed markdown)
    for i, (label, url) in enumerate(links):
        # Escape URL & label
        safe_url = _html.escape(url, quote=True)
        safe_label = _html.escape(label)
        text = text.replace(_PLACEHOLDER_LINK.format(i),
                            f'<a href="{safe_url}">{safe_label}</a>')

    # 14. Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(_PLACEHOLDER_INLINE.format(i), code)

    # 15. Restore fenced code blocks
    for i, block in enumerate(fenced_blocks):
        text = text.replace(_PLACEHOLDER_FENCED.format(i), block)

    return text


def strip_incomplete_markers(text: str) -> str:
    """
    For live streaming: if text ends with an unmatched marker like `**` or `_`,
    trim it so the intermediate render doesn't include a raw dangling marker.
    """
    # Strip trailing whitespace first
    t = text.rstrip()
    # Unclosed **
    if t.count("**") % 2 == 1:
        idx = t.rfind("**")
        if idx != -1:
            t = t[:idx]
    # Unclosed ~~
    if t.count("~~") % 2 == 1:
        idx = t.rfind("~~")
        if idx != -1:
            t = t[:idx]
    # Unclosed inline code `
    ticks = t.count("`") - t.count("```") * 3
    # simpler: if odd single ticks (excluding fenced), leave them; Telegram will render as text if we strip
    if t.endswith("`") and not t.endswith("```"):
        t = t.rstrip("`")
    # Unclosed fenced ```
    if t.count("```") % 2 == 1:
        idx = t.rfind("```")
        if idx != -1:
            t = t[:idx]
    return t


def safe_for_telegram(text: str, limit: int = 4000) -> str:
    """Truncate at char boundary avoiding partial HTML entity."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    lt = cut.rfind("<")
    gt = cut.rfind(">")
    if lt > gt:
        cut = cut[:lt]
    return cut
