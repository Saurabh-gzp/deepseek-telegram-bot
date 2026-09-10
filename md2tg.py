"""
Markdown → Telegram HTML converter.

Telegram HTML supports only these tags:
  <b>, <i>, <u>, <s>, <code>, <pre>, <a href>, <blockquote>, <tg-spoiler>
No <br>, no <p>, no <h1>...<h6>, no <ul>/<li>.

We convert DeepSeek's markdown output to safe Telegram HTML.
"""
import re
import html as _html
from typing import Tuple

_PLACEHOLDER_FENCED = "\x00F{}\x00"
_PLACEHOLDER_INLINE = "\x00I{}\x00"
_PLACEHOLDER_LINK   = "\x00L{}\x00"
_PLACEHOLDER_TABLE  = "\x00T{}\x00"


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

    # 1b. Extract markdown tables — Telegram has no table support, so we
    # render them as an aligned monospace (<pre>) block. Column widths are
    # computed from plain text (markdown markers stripped from cells).
    table_blocks: list[str] = []

    def _table(m):
        raw_rows = m.group(0).strip("\n").splitlines()

        def _cells(row: str) -> list:
            # protect escaped pipes \| before splitting
            prepared = row.strip().strip("|").replace("\\|", "\x00")
            return [c.replace("\x00", "|").strip() for c in prepared.split("|")]

        def _is_sep(cells: list) -> bool:
            return bool(cells) and all(
                re.fullmatch(r":?-{2,}:?", c) for c in cells if c != "") and any(c for c in cells)

        parsed = [_cells(r) for r in raw_rows]
        if not any(_is_sep(r) for r in parsed):
            return m.group(0)  # no separator row → not a real table
        body = [r for r in parsed if not _is_sep(r)]
        if not body:
            return m.group(0)

        def _plain(c: str) -> str:
            c = re.sub(r"`([^`]*)`", r"\1", c)
            c = re.sub(r"\*\*([^*]*)\*\*", r"\1", c)
            c = re.sub(r"__([^_]*)__", r"\1", c)
            c = re.sub(r"~~([^~]*)~~", r"\1", c)
            c = re.sub(r"\*([^*]*)\*", r"\1", c)
            c = re.sub(r"_([^_]*)_", r"\1", c)
            return c

        ncol = min(10, max(len(r) for r in body))
        pbody = []
        for r in body:
            r = (r + [""] * ncol)[:ncol]
            pbody.append([_plain(c) for c in r])

        widths = [3] * ncol
        for r in pbody:
            for i, c in enumerate(r):
                widths[i] = min(40, max(widths[i], len(c)))

        def _fmt(row: list) -> str:
            out = []
            for i, c in enumerate(row):
                if len(c) > widths[i]:
                    c = c[:widths[i] - 1] + "\u2026"
                out.append(c.ljust(widths[i]))
            return "  ".join(out).rstrip()

        lines = [_fmt(pbody[0]), "  ".join("-" * w for w in widths)]
        for r in pbody[1:]:
            lines.append(_fmt(r))
        block = "<pre>" + _html.escape("\n".join(lines)) + "</pre>"
        table_blocks.append(block)
        return _PLACEHOLDER_TABLE.format(len(table_blocks) - 1)

    text = re.sub(r"(?:^[ \t]*\|[^\n]*\|[ \t]*\n?){2,}", _table, text,
                  flags=re.MULTILINE)

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

    # 16. Restore table blocks
    for i, block in enumerate(table_blocks):
        text = text.replace(_PLACEHOLDER_TABLE.format(i), block)

    return text


def split_message(text: str, limit: int = 3800) -> Tuple[str, str]:
    """
    Fence-aware single cut for Telegram's 4096-char message limit.

    Returns (head, tail): head is shown now, tail continues in a new bubble.
    tail == "" when everything fits as one message.

    Guarantees:
      • a ``` code fence is never left dangling — if the best cut falls
        inside a fence, head gets the fence closed and tail re-opens it
        (with the same language tag), so both halves render correctly;
      • cut preference: blank line > newline > space > hard cut;
      • head is only accepted when its RENDERED HTML fits `limit`
        (markdown tags expand on conversion — raw char count lies).
    """
    text = text or ""
    if len(text) <= limit and len(md_to_tg_html(text)) <= limit:
        return text, ""

    base = min(limit, len(text))
    zone = text[:base]

    candidates: list[int] = []
    if zone.count("```") % 2 == 1:
        # cut lands inside an open fence → prefer a line boundary inside it
        nl = zone.rfind("\n")
        if nl > int(limit * 0.4):
            candidates.append(nl)
        candidates.append(base)  # hard cut (giant single code line)
    else:
        nl2 = zone.rfind("\n\n")
        nl = zone.rfind("\n")
        sp = zone.rfind(" ")
        if nl2 > int(limit * 0.5):
            candidates.append(nl2 + 1)
        if nl > int(limit * 0.6):
            candidates.append(nl + 1)
        if sp > int(limit * 0.7):
            candidates.append(sp + 1)
        candidates.append(base)

    # Quality-first order: blank line > newline > space > hard cut.
    # (Order matters — do NOT sort by size.)
    for cut in dict.fromkeys(c for c in candidates if 0 < c < len(text)):
        last_cut = cut
        head, tail = _apply_cut(text, cut)
        if len(md_to_tg_html(head)) <= limit:
            return head, tail

    # Rendered HTML still exceeds the limit → back off in ~10% steps,
    # preferring word boundaries, then line boundaries, and — inside a
    # giant single code line — plain hard steps. Every step re-checks
    # the RENDERED size, since markdown tags expand on conversion.
    floor = int(limit * 0.4)
    cut = last_cut
    while cut > floor:
        step = int(limit * 0.1)
        nxt = text.rfind(" ", 0, cut - step)
        if nxt < 0:
            nxt = text.rfind("\n", 0, cut - step)
        if nxt < 0 and zone.count("```") % 2 == 1:
            nxt = cut - step  # giant single code line: hard backoff
        if nxt <= 0:
            break
        cut = nxt
        head, tail = _apply_cut(text, cut)
        if len(md_to_tg_html(head)) <= limit:
            return head, tail

    # last resort: hard slice (safe_for_telegram guards at the call site)
    return text[:limit], text[limit:].lstrip("\n")


def _apply_cut(text: str, cut: int) -> Tuple[str, str]:
    """Cut raw markdown at `cut`, closing/re-opening an active fence."""
    zone = text[:cut]
    head = text[:cut].rstrip()
    tail = text[cut:].lstrip("\n")
    if zone.count("```") % 2 == 1:
        head = head + "\n```"
        open_at = zone.rfind("```")
        m = re.match(r"```([^\s`]+)", text[open_at:open_at + 30])
        lang = m.group(1) if m else ""
        tail = f"```{lang}\n{tail}" if tail else ""
    return head, tail


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
