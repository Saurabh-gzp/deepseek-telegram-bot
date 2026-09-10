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
    # markdown table → aligned monospace <pre>, no raw pipes/markers
    ("| Name | Age |\n|---|---|\n| Ram | 21 |\n| **Shyam** | 30 |",
     "<pre>Name", "|---|"),
    ("| x | y |\n|---|---|\n| 1 | 2 |", "  ", "**"),
    # pipe-like lines WITHOUT a separator row must stay untouched
    ("a | b\nc | d", "a | b", "<pre>"),
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

print("\n--- split_message (fence-aware long response cut) ---")
from md2tg import split_message


def _sp(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


# 1. fits whole
h, t = split_message("hello world", 100)
_sp("short text fits whole", h == "hello world" and t == "", repr((h, t)))

# 2. paragraph boundary preferred
long = "A" * 50 + "\n\n" + "B" * 50
h, t = split_message(long, 60)
_sp("splits at blank line",
    h == "A" * 50 and t == "B" * 50, repr((h[:20], t[:20])))

# 3. fence straddling the cut → closed in head, re-opened in tail
fencey = "intro line\n\n```python\n" + "x = 1\n" * 30 + "print('end')\n```\nbye"
h, t = split_message(fencey, 150)
_sp("fence closed in head", h.endswith("```") and h.count("```") % 2 == 0,
    repr(h[-25:]))
_sp("fence re-opened in tail", t.startswith("```python"), repr(t[:20]))
_sp("head renders within limit", len(md_to_tg_html(h)) <= 150,
    f"got {len(md_to_tg_html(h))}")

# 4. expansion-heavy markdown (italic → HTML grows ~2.7x) still fits
exp = "*word " * 4000
h, t = split_message(exp, 3800)
_sp("expansion-heavy head fits",
    len(md_to_tg_html(h)) <= 3800 and t != "",
    f"head_html={len(md_to_tg_html(h))}")

# 5. giant single code line → hard cut but fence intact
giant = "```python\n" + "z" * 9000 + "\n```"
h, t = split_message(giant, 3800)
_sp("giant code line cut with fence closed",
    h.count("```") % 2 == 0 and t.startswith("```python"),
    repr(h[-10:]))

# 6. table + fence combo renders both
combo = "| a | b |\n|---|---|\n| 1 | 2 |\n\n```js\nlet x = 1\n```"
out = md_to_tg_html(combo)
_sp("table + fence combo", "<pre>a" in out and "language-js" in out, repr(out[:80]))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
