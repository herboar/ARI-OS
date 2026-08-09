"""ARI-OS local web monitor — System 7 styled dashboard + Lane Board.

`python3 -m ari_os.tools.monitor` serves http://localhost:7777, reading
~/.ari-os state and rendering a classic Mac OS 7 control panel. Stdlib only.

Two routes, both read-only: `GET /` (the page) and `GET /state.json` (the
same state as JSON plus pre-rendered fragments, polled by the page every 5s).
The old whole-page `<meta http-equiv=refresh>` is gone: it failed *silently*
when the server died — the page simply stopped reloading and looked fine.
"""
from __future__ import annotations
import html as _html
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from . import state as _state
from .lane_assets import BOARD_CSS, BOARD_JS, MONITOR_JS
from . import lane_render as _lane_render

PORT = 7777
DEFAULT_THEME = "beige"

# Both themes share the System 7 stipple style (a 4px dot pattern over a
# vertical gradient); only the palette differs. Selected via
# $ARI_OS_MONITOR_THEME or the control panel.
THEMES = {
    "beige": ("radial-gradient(circle at 0 0,rgba(255,255,255,0.4) 1px,transparent 1.5px),"
              "linear-gradient(180deg,#D6D2C4 0%,#C9C4B8 100%)"),
    "stipple": ("radial-gradient(circle at 0 0,rgba(255,255,255,0.5) 1px,transparent 1.5px),"
                "linear-gradient(180deg,#C8A8E9 0%,#F2B8DC 100%)"),
}


def current_theme() -> str:
    t = os.environ.get("ARI_OS_MONITOR_THEME", DEFAULT_THEME)
    return t if t in THEMES else DEFAULT_THEME


def body_background(theme: str) -> str:
    img = THEMES.get(theme, THEMES[DEFAULT_THEME])
    # Pin to the viewport so the fill gradient never tiles below short content;
    # only the 4px stipple repeats.
    return (f"background-image:{img};background-size:4px 4px,100% 100%;"
            f"background-repeat:repeat,no-repeat;background-attachment:fixed;")


_CSS = """
body{font-family:Chicago,'ChicagoFLF',system-ui,sans-serif;
  color:#000;margin:0;padding:24px;min-height:100vh;box-sizing:border-box;}
.window{background:#fff;border:2px solid #000;box-shadow:2px 2px 0 #000;
  max-width:560px;margin:0 auto;}
.title-bar{background:repeating-linear-gradient(#000 0 1px,#fff 1px 2px);
  border-bottom:2px solid #000;padding:3px 8px;display:flex;align-items:center;}
.title-bar .name{background:#fff;padding:0 8px;font-weight:bold;}
.body{padding:12px;}
.row{display:flex;justify-content:space-between;border:1px solid #000;
  padding:6px 8px;margin:4px 0;background:#fff;}
.s-running{font-weight:bold;}
.s-blocked{background:#000;color:#fff;}
.s-done{color:#555;}
.q{border:2px solid #000;background:#fff;padding:8px;margin-top:8px;}
.picker{position:fixed;top:12px;right:12px;background:#fff;border:2px solid #000;
  box-shadow:2px 2px 0 #000;padding:4px 8px;font-size:12px;font-weight:bold;}
.picker input{vertical-align:middle;margin-left:6px;}
"""

# Client-side gradient picker: recolors the dithered wallpaper from a chosen
# hue and remembers it in localStorage (survives the 3s auto-refresh).
_PICKER = """
<div class="picker">BG<input type="color" id="bg" value="#C8A8E9"></div>
<script>
(function(){
  function lighten(hex,amt){
    var n=parseInt(hex.slice(1),16),r=n>>16&255,g=n>>8&255,b=n&255;
    r=Math.round(r+(255-r)*amt);g=Math.round(g+(255-g)*amt);b=Math.round(b+(255-b)*amt);
    return '#'+((1<<24)+(r<<16)+(g<<8)+b).toString(16).slice(1);
  }
  function apply(hex){
    document.body.style.backgroundImage=
      'radial-gradient(circle at 0 0,rgba(255,255,255,0.5) 1px,transparent 1.5px),'+
      'linear-gradient(180deg,'+lighten(hex,0.35)+' 0%,'+hex+' 100%)';
    document.body.style.backgroundSize='4px 4px,100% 100%';
    document.body.style.backgroundRepeat='repeat,no-repeat';
    document.body.style.backgroundAttachment='fixed';
  }
  var input=document.getElementById('bg');
  var saved=localStorage.getItem('ariosBg');
  if(saved){input.value=saved;apply(saved);}
  input.addEventListener('input',function(){
    localStorage.setItem('ariosBg',input.value);apply(input.value);});
})();
</script>
"""


def load_state() -> dict:
    qdir = _state.state_dir() / "questions"
    return {"workers": _state.reconcile(),
            "questions": [p.stem for p in sorted(qdir.glob("*.md"))]}


def render_workers(state: dict) -> str:
    """The System 7 worker rows — unchanged markup, now swappable in place."""
    rows = []
    for w in state.get("workers", []):
        st_raw = w.get("status", "?")
        st_class = st_raw if st_raw in {"running", "blocked", "done"} else "unknown"
        label = _html.escape(w.get("label", ""), quote=True)
        st = _html.escape(st_raw, quote=True)
        rows.append(
            f'<div class="row s-{st_class}"><span>{label}</span>'
            f'<span>{st}</span></div>')
    return "".join(rows) or '<div class="row"><span>no workers</span></div>'


def render_questions(state: dict) -> str:
    qs = state.get("questions", [])
    if not qs:
        return ""
    items = "".join(
        f"<div>&#x26A0; {_html.escape(q, quote=True)} needs an answer</div>"
        for q in qs)
    return f'<div class="q"><b>Questions</b>{items}</div>'


def render_html(state: dict, theme: str | None = None) -> str:
    theme = theme if theme in THEMES else current_theme()
    board = _lane_render.render_board_fragment(_lane_render._snapshot())
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>ARI-OS</title><style>{_CSS}\nbody{{{body_background(theme)}}}\n"
            f"{BOARD_CSS}</style>"
            f"</head><body>"
            f"<div class='window'><div class='title-bar'>"
            f"<span class='name'>ARI-OS Monitor</span></div>"
            f"<div class='body'><div id='lb-workers'>{render_workers(state)}</div>"
            f"<div id='lb-questions'>{render_questions(state)}</div></div></div>"
            f"<div id='lb-root'>{board}</div>{_PICKER}"
            f"<script>{BOARD_JS}</script>"
            f"<script>{MONITOR_JS}</script></body></html>")


def state_payload(state: dict) -> dict:
    """`/state.json` — read-only. Raw state plus the fragments the page swaps."""
    snap = _lane_render._snapshot()
    return {
        "generated_at": snap.get("generated_at"),
        "workers": state.get("workers", []),
        "questions": state.get("questions", []),
        "snapshot": snap,
        "board_html": _lane_render.render_board_fragment(snap),
        "workers_html": render_workers(state),
        "questions_html": render_questions(state),
    }


class _Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/state.json":
            payload = json.dumps(state_payload(load_state()), default=str).encode()
            self._send(payload, "application/json; charset=utf-8")
        elif path == "/":
            self._send(render_html(load_state()).encode(), "text/html; charset=utf-8")
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)

    def log_message(self, *a):
        pass


def main() -> None:
    srv = HTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"Monitor live at http://localhost:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
