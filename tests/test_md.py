import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from md2tg import md_to_tg_html, strip_incomplete_markers

TESTS = [
    # (input, must-contain, must-NOT-contain)
    ("**bold**", "<b>bold</b>", "**"),
    ("*italic*", "<i>italic</i>", "*it"),
    ("__also bold__", "<b>also bold</b>", "__"),
    ("_also italic_", "<i>also italic</i>", "_al"),
    ("~~strike~~", "<s>strike</s>", "~~"),
    ("`inline code`", "<code>inline code</code>", "`"),
    ("[link](https://example.com)", '<a href="https://example.com">link</a>', "["),
    ("# Heading 1", "<b>Heading 1</b>", "#"),
    ("## Heading 2", "<b>Heading 2</b>", "##"),
    ("- item1\n- item2", "• item1\n• item2", "\n- "),
    ("* bullet", "• bullet", "* bullet"),
    ("> quote line\n> another", "<blockquote>quote line\nanother</blockquote>", "&gt;"),
    ("```python\nprint('hi')\n```", "<pre><code class=\"language-python\">print(&#x27;hi&#x27;)", "```"),
    ("```\nplain code\n```", "<pre>plain code", "```"),
    ("a < b & c > d", "a &lt; b &amp; c &gt; d", "<"),
    ("**bold with `code` inside**", "<b>bold with <code>code</code> inside</b>", "**"),
    ("mix **bold** and *italic*", "<b>bold</b> and <i>italic</i>", "**"),
    ("---", "─────", "---"),
]

PASS = FAIL = 0
for inp, must_have, must_not in TESTS:
    out = md_to_tg_html(inp)
    ok = must_have in out and must_not not in out
    if ok:
        PASS += 1
        print(f"  ✅ {inp[:40]!r:45} → {out[:60]!r}")
    else:
        FAIL += 1
        print(f"  ❌ {inp!r}")
        print(f"     got:      {out!r}")
        print(f"     expected: contain {must_have!r}, NOT contain {must_not!r}")

print(f"\n{PASS} passed, {FAIL} failed")

print("\n--- Incomplete marker stripping ---")
for inp, exp in [
    ("hello **wor", "hello"),
    ("code `partial", "code"),
    ("```py\nprint(", ""),  # unclosed fence stripped
    ("done **fully** ", "done **fully**"),  # complete stays
]:
    got = strip_incomplete_markers(inp).strip()
    print(f"  {inp!r:35} → {got!r:35}  {'✅' if exp in got or (exp=='' and got=='') else '❌'}")

print("\n--- Real DeepSeek-style example ---")
sample = """Arey wah! Chalo bataata hoon:

---

**📚 KNOWLEDGE & INFORMATION**
- Har tarah ke sawaalon ke jawab (science, history, etc.)
- Complex concepts ko *aasaan* bhasha mein samjhaana

**💻 CODING & TECH**
- Programming help (Python, Java, C++, JavaScript, etc.)
- Code debug karna, `optimize` karna

Example:
```python
def hello():
    print("hi")
```

Visit [Python docs](https://docs.python.org) for more.

> Note: main free hoon!
"""
print(md_to_tg_html(sample))
sys.exit(0 if FAIL == 0 else 1)
