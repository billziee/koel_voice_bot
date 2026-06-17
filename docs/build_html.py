"""Render the design-doc markdown into styled, standalone HTML.

Reads HLD.md / LLD.md and emits HLD.html / LLD.html, matching the KOEL PRD
aesthetic. Prose/tables/code are rendered to static HTML at build time (so the
docs are fully readable offline); only the mermaid diagrams render in-browser
via mermaid.js (CDN) — and degrade to readable source if offline.

Run:  python docs/build_html.py   (needs the `markdown` package; in backend/.venv)
"""

from __future__ import annotations

import html as _html
import pathlib
import re

import markdown as _md

DOCS = pathlib.Path(__file__).parent
PAGES = [
    ("HLD.md", "HLD.html", "KOEL — High-Level Design", "HLD"),
    ("LLD.md", "LLD.html", "KOEL — Low-Level Design", "LLD"),
]

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&family=Lora:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --primary:#053BEE; --primary-dark:#032CB8; --primary-light:#E8EDFF; --primary-subtle:#F2F5FF;
    --accent:#EB5324; --ink:#000A1B; --ink-2:#1A1F2E; --ink-muted:#5b6473; --faint:#8a93a3;
    --rule:#E2E6EE; --surface:#F5F7FA; --white:#fff; --green:#05604F; --green-light:#EDFAF4;
    --font-body:'DM Sans',sans-serif; --font-display:'Lora',serif; --font-mono:'JetBrains Mono',monospace;
  }}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
  html{{scroll-behavior:smooth;}}
  body{{font-family:var(--font-body);color:var(--ink);background:var(--white);line-height:1.7;font-size:15.5px;-webkit-font-smoothing:antialiased;}}
  nav{{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--rule);
       display:flex;align-items:center;justify-content:space-between;padding:0 32px;height:54px;}}
  .nav-logo{{font-family:var(--font-display);font-size:19px;font-weight:600;color:var(--primary);letter-spacing:-.01em;}}
  .nav-logo small{{color:var(--ink-muted);font-family:var(--font-body);font-size:12px;font-weight:500;letter-spacing:.04em;margin-left:8px;}}
  .nav-links{{display:flex;gap:8px;}}
  .nav-links a{{text-decoration:none;font-size:13px;font-weight:500;color:var(--ink-muted);padding:6px 12px;border-radius:7px;transition:.15s;}}
  .nav-links a:hover{{color:var(--primary);background:var(--primary-subtle);}}
  .nav-links a.active{{color:var(--primary);background:var(--primary-light);}}
  main{{max-width:880px;margin:0 auto;padding:56px 32px 120px;}}
  h1{{font-family:var(--font-display);font-size:42px;font-weight:600;letter-spacing:-.02em;line-height:1.1;margin:8px 0 28px;}}
  h2{{font-family:var(--font-display);font-size:27px;font-weight:600;letter-spacing:-.015em;margin:52px 0 16px;padding-top:24px;border-top:1px solid var(--rule);}}
  h3{{font-size:18px;font-weight:600;margin:30px 0 10px;color:var(--ink-2);}}
  h4{{font-size:14px;font-weight:600;margin:20px 0 8px;color:var(--ink-muted);text-transform:uppercase;letter-spacing:.05em;}}
  p{{margin:0 0 14px;color:var(--ink-2);}}
  a{{color:var(--primary);}}
  ul,ol{{margin:0 0 16px;padding-left:24px;}} li{{margin:5px 0;color:var(--ink-2);}}
  strong{{font-weight:600;color:var(--ink);}}
  blockquote{{border-left:3px solid var(--primary);background:var(--primary-subtle);padding:12px 18px;border-radius:0 8px 8px 0;margin:0 0 20px;color:var(--primary-dark);font-size:14px;}}
  blockquote p{{margin:0;color:var(--primary-dark);}}
  code{{font-family:var(--font-mono);font-size:.86em;background:var(--surface);border:1px solid var(--rule);border-radius:5px;padding:1px 5px;color:var(--accent);}}
  pre{{background:#0d1117;border-radius:10px;padding:18px 20px;overflow-x:auto;margin:0 0 20px;}}
  pre code{{background:none;border:none;padding:0;color:#e6edf3;font-size:12.5px;line-height:1.6;}}
  table{{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--rule);border-radius:10px;overflow:hidden;margin:0 0 22px;font-size:13.5px;}}
  th{{background:var(--primary);color:#fff;text-align:left;padding:10px 14px;font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;}}
  td{{padding:9px 14px;border-bottom:1px solid var(--rule);color:var(--ink-muted);vertical-align:top;line-height:1.55;}}
  tr:last-child td{{border-bottom:none;}} tr:nth-child(even) td{{background:var(--surface);}}
  td code{{font-size:.84em;}}
  hr{{border:none;border-top:1px solid var(--rule);margin:36px 0;}}
  .mermaid{{background:var(--surface);border:1px solid var(--rule);border-radius:12px;padding:22px;margin:0 0 24px;text-align:center;overflow-x:auto;}}
  .doc-meta{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:4px;}}
  footer{{max-width:880px;margin:0 auto;padding:24px 32px 60px;color:var(--faint);font-size:12.5px;border-top:1px solid var(--rule);}}
  @media(max-width:640px){{main,nav,footer{{padding-left:18px;padding-right:18px;}} h1{{font-size:32px;}}}}
</style>
</head>
<body>
<nav>
  <span class="nav-logo">KOEL <small>Design Docs</small></span>
  <span class="nav-links">
    <a href="HLD.html"{hld_active}>HLD</a>
    <a href="LLD.html"{lld_active}>LLD</a>
  </span>
</nav>
<main><p class="doc-meta">{kind} · KOEL Voice Bot</p><div id="content">{content}</div></main>
<footer>Generated from <code>docs/{src}</code> · rebuild with <code>python docs/build_html.py</code></footer>

<script type="module">
  // Prose/tables are static HTML (offline-readable); only diagrams need JS.
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{startOnLoad:false, theme:'neutral', securityLevel:'loose',
    themeVariables:{{primaryColor:'#E8EDFF', primaryBorderColor:'#053BEE', lineColor:'#5b6473', fontFamily:'DM Sans, sans-serif'}}}});
  try {{ await mermaid.run({{querySelector:'.mermaid'}}); }}
  catch (e) {{ console.error('mermaid render failed (offline?):', e); }}
</script>
</body>
</html>
"""


_MERMAID_RE = re.compile(
    r'<pre>\s*<code[^>]*class="[^"]*language-mermaid[^"]*"[^>]*>(.*?)</code>\s*</pre>',
    re.DOTALL,
)


def _render_markdown(md: str) -> str:
    """Markdown → HTML, then promote ```mermaid code blocks to <div class="mermaid">."""
    html = _md.markdown(md, extensions=["fenced_code", "tables", "sane_lists", "toc"])
    return _MERMAID_RE.sub(
        lambda m: f'<div class="mermaid">{_html.unescape(m.group(1))}</div>', html
    )


def main() -> None:
    for src, out, title, kind in PAGES:
        md = (DOCS / src).read_text(encoding="utf-8")
        html = TEMPLATE.format(
            title=title,
            kind=kind,
            src=src,
            content=_render_markdown(md),
            hld_active=' class="active"' if kind == "HLD" else "",
            lld_active=' class="active"' if kind == "LLD" else "",
        )
        (DOCS / out).write_text(html, encoding="utf-8")
        print(f"wrote docs/{out} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
