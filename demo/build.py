"""Build a single-file demo page that runs the web UI with no server.

    python demo/build.py            -> demo/phone-demo.html

The page bundles static/ (the real UI) with demo/mockserver.js, an
in-browser port of the API, and the trial dataset (demo/export_state.py), so
it can be opened on a phone or shared as a link. Business Central is
simulated; changes are saved in the browser.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
sys.path.insert(0, str(ROOT))

BANNER = """
<div class="demo-bar" id="demo-bar">
  <span><b>Trial data</b> · 10 weeks of history and 20 live jobs, with a simulated Business Central.
    Runs in your browser; your changes are kept on this device.</span>
  <button type="button" class="btn" id="demo-reset">Reset demo data</button>
</div>
"""

DEMO_CSS = """
.demo-bar { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; padding: 8px 16px;
            background: var(--bg); border-bottom: 1px solid var(--line); font-size: 13px; color: var(--muted); }
.demo-bar b { color: var(--text); }
.demo-bar span { flex: 1; min-width: 0; }
"""

DEMO_JS = """
document.getElementById('live').textContent = '● demo';
document.getElementById('demo-reset').onclick = async () => {
  if (!await askConfirm('Reset all demo data back to the starting sample? Jobs, lots and postings you created are removed.', 'Reset')) return;
  window.mfgDemoReset();
  toast('Demo data reset');
  render();
};
"""


def build() -> Path:
    from demo.export_state import export
    seed_json = json.dumps(export(), separators=(",", ":")).replace("</", "<\\/")
    html = (STATIC / "index.html").read_text()
    body = re.search(r"<body>(.*?)<script", html, re.S).group(1)
    body = body.replace("<main", BANNER + "<main", 1)
    css = (STATIC / "styles.css").read_text() + DEMO_CSS
    page = "\n".join([
        "<title>Manufacturing Costing</title>",
        f"<style>\n{css}\n</style>",
        body.strip(),
        f"<script>window.MFG_SEED = {seed_json};</script>",
        f"<script>\n{(ROOT / 'demo' / 'mockserver.js').read_text()}\n</script>",
        f"<script>\n{(STATIC / 'app.js').read_text()}\n{DEMO_JS}\n</script>",
    ])
    out = ROOT / "demo" / "phone-demo.html"
    out.write_text(page)
    return out


if __name__ == "__main__":
    print(build())
