"""Save report to Markdown and HTML files."""
from pathlib import Path

import config


def save_report_md(content: str, path: Path | str | None = None) -> Path:
    """Save Markdown report to file. Returns path."""
    if path is None:
        path = config.DATA_DIR / "reports" / "report.md"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def save_report_html(content_md: str, path: Path | str | None = None) -> Path:
    """Convert Markdown to minimal HTML and save. Returns path."""
    try:
        import markdown
    except ImportError:
        html = f"<pre>{content_md}</pre>"
    else:
        html = markdown.markdown(content_md, extensions=["extra"])
    full_html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>比赛复盘</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 1rem auto; padding: 0 1rem; line-height: 1.5; }}
h1 {{ border-bottom: 1px solid #eee; }}
pre {{ background: #f5f5f5; padding: 1rem; overflow: auto; }}
</style>
</head>
<body>
{html}
</body>
</html>
"""
    if path is None:
        path = config.DATA_DIR / "reports" / "report.html"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full_html, encoding="utf-8")
    return path
