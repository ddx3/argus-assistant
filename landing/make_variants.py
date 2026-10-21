"""A comparison page: three layout variants of the "How it works" block, on the real texts and
frames. It is needed to choose the layout before it goes onto the landing page.

    python make_variants.py     -> out/variants.html
"""
import base64
import json
from pathlib import Path

HERE = Path(__file__).parent
LANG = "en"
C = json.loads((HERE / "copy" / f"{LANG}.json").read_text(encoding="utf-8"))


def img(name: str) -> str:
    p = next((HERE / "img" / LANG).glob(name + ".*"))
    mime = "image/jpeg" if p.suffix == ".jpg" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


I = {n: img(n) for n in ("hear_chips", "hear_rows", "see", "ans_text", "ans_code", "ans_math")}


def im(name: str, alt: str = "") -> str:
    # the frames are shot at double density: 2x gives them back the natural size of the window
    return f'<img srcset="{I[name]} 2x" alt="{alt}">'


STEPS = [
    ("hear", "var(--me)", C["how.hear.h"], C["how.hear.p"], [C["how.hear.f1"], C["how.hear.f2"], C["how.hear.f3"]]),
    ("see", "var(--screen)", C["how.see.h"], C["how.see.p"], [C["how.see.f1"], C["how.see.f2"], C["how.see.f3"]]),
    ("ans", "var(--ok)", C["how.ans.h"], C["how.ans.p"], [C["how.ans.f1"], C["how.ans.f2"], C["how.ans.f3"]]),
]


def facts(fs):
    return '<ul class="facts">' + "".join(f"<li>{f}</li>" for f in fs) + "</ul>"


def shots(key: str) -> str:
    if key == "hear":
        return f'<div class="shots">{im("hear_chips")}{im("hear_rows")}</div>'
    if key == "see":
        return f'<div class="shots">{im("see")}</div>'
    return (f'<div class="shots"><div class="tabs" role="tablist">'
            f'<button class="on" data-t="0">{C["tab.text"]}</button><button data-t="1">{C["tab.code"]}</button>'
            f'<button data-t="2">{C["tab.math"]}</button></div>'
            f'<div class="ans">{im("ans_text")}{im("ans_code")}{im("ans_math")}</div></div>')


# ------------------------------------------------------------------ A: a stage with a list of steps
def variant_a() -> str:
    items, panes = [], []
    for n, (key, color, h, p, fs) in enumerate(STEPS):
        items.append(f'''<button type="button" class="a-item{" on" if n == 0 else ""}" role="tab" data-i="{n}" style="--c:{color}">
          <span class="a-head"><i></i><b>{h}</b></span>
          <span class="a-fold"><span class="a-in"><span class="a-p">{p}</span>{facts(fs)}</span></span>
        </button>''')
        panes.append(f'<div class="a-pane{" on" if n == 0 else ""}" data-i="{n}">{shots(key)}</div>')
    return f'''<div class="a slab" id="va">
      <div class="a-list" role="tablist">{"".join(items)}</div>
      <div class="stage a-stage">{"".join(panes)}</div>
    </div>'''


# ------------------------------------------------------------------ B: bento
def variant_b() -> str:
    def card(i, cls):
        key, color, h, p, fs = STEPS[i]
        return f'''<article class="b-card slab {cls}" style="--c:{color}">
          <div class="b-text"><h3><i></i>{h}</h3><p>{p}</p>{facts(fs)}</div>
          <div class="stage b-stage">{shots(key)}</div>
        </article>'''
    key, color, h, p, fs = STEPS[2]
    wide = f'''<article class="b-card b-wide slab" style="--c:{color}">
          <div class="b-text"><h3><i></i>{h}</h3><p>{p}</p>{facts(fs)}</div>
          <div class="stage b-stage">{shots("ans")}</div>
        </article>'''
    return f'<div class="b">{card(0, "b-hear")}{card(1, "b-see")}{wide}</div>'


# ------------------------------------------------------------------ C: paired cards
def variant_c() -> str:
    rows = []
    for key, color, h, p, fs in STEPS:
        rows.append(f'''<div class="c-row" style="--c:{color}">
          <div class="slab c-text"><h3><i></i>{h}</h3><p>{p}</p>{facts(fs)}</div>
          <div class="slab stage c-stage">{shots(key)}</div>
        </div>''')
    return f'<div class="c">{"".join(rows)}</div>'


CSS = r"""
:root{color-scheme:dark;--bg:#0a0c10;--panel:#171b21;--win:#181b21;--slab:rgba(255,255,255,.03);--slab-edge:rgba(255,255,255,.09);
  --btn-hover:rgba(255,255,255,.09);--edge-hi:rgba(255,255,255,.2);--line:rgba(255,255,255,.08);--text:#f2f4f7;--dim:#9aa1ad;--faint:#5b6270;
  --me:#4fb8d8;--screen:#d8b54f;--ok:#5fd68a;--lift:0 20px 40px -26px rgba(0,0,0,.65);--r2:9px;--r3:12px;
  --face:"Segoe UI",system-ui,-apple-system,sans-serif;--mono:Consolas,"Cascadia Mono",ui-monospace,monospace;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 var(--face);-webkit-font-smoothing:antialiased}
h1,h2,h3,p{margin:0}h2,h3{font-weight:600;line-height:1.16;text-wrap:balance}
img{display:block;max-width:100%;height:auto}
.wrap{max-width:1240px;margin-inline:auto;padding-inline:clamp(16px,4vw,40px)}
.slab{background:var(--slab);border:1px solid var(--slab-edge);border-radius:var(--r2);box-shadow:var(--lift)}
.tag{font-size:11px;letter-spacing:.13em;text-transform:uppercase;color:var(--dim);font-weight:600}
.intro{padding-block:2.4rem 1.2rem;display:flex;flex-direction:column;gap:.5rem;max-width:62ch}
.intro h1{font-size:1.9rem}.intro p{color:var(--dim)}
.opt{padding-block:2.6rem;border-top:1px solid var(--line)}
.opt-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:1.4rem}
.opt-head .k{font-family:var(--mono);font-size:12px;color:var(--bg);background:var(--text);border-radius:5px;padding:2px 8px;font-weight:700}
.opt-head h2{font-size:1.35rem}.opt-head p{color:var(--dim);font-size:13.5px;flex-basis:100%;max-width:70ch}
h3{font-size:clamp(1.2rem,2vw,1.45rem);display:flex;align-items:center;gap:11px}
h3 i,.a-head i{width:11px;height:11px;border-radius:50%;flex:none;background:var(--c);box-shadow:0 0 0 4px color-mix(in srgb,var(--c) 14%,transparent),0 0 10px color-mix(in srgb,var(--c) 30%,transparent)}
p{color:var(--dim);line-height:1.62}
.facts{list-style:none;margin:2px 0 0;padding:0;display:flex;flex-direction:column;gap:7px}
.facts li{position:relative;padding-left:16px;font-size:13.5px;color:var(--dim);line-height:1.5}
.facts li::before{content:"";position:absolute;left:0;top:.55em;width:6px;height:6px;border-radius:2px;background:var(--faint)}
/* stage for the frames: a dotted grid, so the empty space around a frame looks intentional */
.stage{background:radial-gradient(circle at 1px 1px,rgba(255,255,255,.075) 1px,transparent 0) 0 0/16px 16px,rgba(0,0,0,.22);
  border:1px solid var(--slab-edge);border-radius:var(--r2);display:grid;place-items:center;padding:clamp(14px,2.4vw,26px);min-width:0}
.shots{display:flex;flex-direction:column;align-items:center;gap:12px;max-width:100%;min-width:0}
.shots img{border:1px solid var(--slab-edge);border-radius:7px;background:#08090b;max-width:100%;box-shadow:0 14px 30px -16px #000}
.tabs{display:flex;gap:2px;padding:3px;background:var(--slab);border:1px solid var(--slab-edge);border-radius:9px;width:fit-content}
.tabs button{padding:4px 14px;border:1px solid transparent;border-radius:6px;background:none;color:var(--dim);font:inherit;font-size:13px;cursor:pointer}
.tabs button:hover{color:var(--text);background:var(--btn-hover)}
.tabs button.on{color:var(--text);background:var(--btn-hover);border-color:var(--edge-hi)}
.ans{display:grid;max-width:100%}.ans img{grid-area:1/1;transition:opacity .2s}.ans img:not(.on){opacity:0;pointer-events:none}

/* A: a list of steps on the left, one stage on the right */
.a{display:grid;grid-template-columns:minmax(300px,4fr) minmax(0,8fr);gap:0;overflow:hidden}
.a-list{display:flex;flex-direction:column;padding:10px;gap:4px;border-right:1px solid var(--slab-edge)}
.a-item{all:unset;box-sizing:border-box;display:block;cursor:pointer;padding:16px 18px;border-radius:var(--r2);border:1px solid transparent;transition:background .15s,border-color .15s}
.a-item:hover{background:rgba(255,255,255,.04)}
.a-item.on{background:rgba(255,255,255,.06);border-color:var(--slab-edge)}
.a-item:focus-visible{outline:2px solid rgba(242,244,247,.7);outline-offset:2px}
.a-head{display:flex;align-items:center;gap:11px;font-size:1.15rem}.a-head b{font-weight:600}
.a-fold{display:grid;grid-template-rows:0fr;transition:grid-template-rows .3s ease}
.a-item.on .a-fold{grid-template-rows:1fr}
.a-in{overflow:hidden;display:flex;flex-direction:column;gap:12px;padding-top:0;min-height:0}
.a-item.on .a-in{padding-top:12px}
.a-p{color:var(--dim);line-height:1.62;font-size:14.5px}
.a-stage{border:0;border-radius:0;min-height:430px;background-color:transparent}
.a-pane{display:none}.a-pane.on{display:grid;animation:fade .3s ease}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* B: bento layout: text on top, frame below, the bottom card has two columns */
.b{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px}
.b-card{padding:22px;display:flex;flex-direction:column;gap:18px;min-width:0}
.b-hear{grid-column:span 7}.b-see{grid-column:span 5}.b-wide{grid-column:1/-1;display:grid;grid-template-columns:minmax(0,4fr) minmax(0,8fr);gap:22px;align-items:stretch}
.b-text{display:flex;flex-direction:column;gap:12px}
.b-stage{margin-top:auto}.b-wide .b-stage{margin-top:0}

/* C: paired cards: text and frame in two tiles of the same height */
.c{display:flex;flex-direction:column;gap:14px}
.c-row{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,8fr);gap:14px;align-items:stretch}
.c-row:nth-child(even){grid-template-columns:minmax(0,8fr) minmax(0,4fr)}
.c-row:nth-child(even) .c-text{order:2}
.c-text{padding:26px;display:flex;flex-direction:column;gap:12px;justify-content:center}
.c-stage{padding:clamp(14px,2.4vw,26px)}

@media (max-width:900px){
  .a{grid-template-columns:1fr}.a-list{border-right:0;border-bottom:1px solid var(--slab-edge)}.a-stage{min-height:0}
  .b-hear,.b-see{grid-column:1/-1}.b-wide{grid-template-columns:1fr}
  .c-row,.c-row:nth-child(even){grid-template-columns:1fr}.c-row:nth-child(even) .c-text{order:0}
  .shots{align-items:flex-start;overflow-x:auto;max-width:100%}.shots img{max-width:none}.ans{overflow-x:auto}
}
"""

JS = r"""
(function(){
  var items=[].slice.call(document.querySelectorAll('.a-item')), panes=[].slice.call(document.querySelectorAll('.a-pane'));
  function pick(i){items.forEach(function(el,j){el.classList.toggle('on',j===i);el.setAttribute('aria-selected',j===i)});
    panes.forEach(function(el,j){el.classList.toggle('on',j===i)});}
  items.forEach(function(el,i){el.setAttribute('role','tab');el.setAttribute('tabindex','0');el.addEventListener('click',function(){pick(i)});
    el.addEventListener('keydown',function(e){if(e.key==='ArrowDown'||e.key==='ArrowRight'){e.preventDefault();var n=(i+1)%items.length;pick(n);items[n].focus()}
      else if(e.key==='ArrowUp'||e.key==='ArrowLeft'){e.preventDefault();var n=(i+items.length-1)%items.length;pick(n);items[n].focus()}})});
  document.querySelectorAll('.shots').forEach(function(box){
    var tabs=[].slice.call(box.querySelectorAll('.tabs button')), imgs=[].slice.call(box.querySelectorAll('.ans img'));
    if(!tabs.length)return; imgs[0].classList.add('on');
    tabs.forEach(function(b,i){b.addEventListener('click',function(){tabs.forEach(function(x,j){x.classList.toggle('on',j===i)});imgs.forEach(function(m,j){m.classList.toggle('on',j===i)})})});
  });
})();
"""

HTML = f"""<title>Argus blocks</title>
<style>{CSS}</style>
<div class="wrap">
  <div class="intro"><span class="tag">Block layout</span><h1>Three variants of "How it works"</h1>
  <p>The texts and frames are real. The frames are shown at the natural size of the window, so the text on them is the same size as the page text.</p></div>

  <section class="opt"><div class="opt-head"><span class="k">A</span><h2>Stage and a list of steps</h2>
    <p>One block instead of three rows. Three steps on the left, the selected one expands its description, one stage on the right, the frames change on click. Always the same height, nothing ragged.</p></div>
    {variant_a()}</section>

  <section class="opt"><div class="opt-head"><span class="k">B</span><h2>Bento</h2>
    <p>Cards of different sizes in one grid: "Hears" and "Sees" side by side, "Answers" across the full width. Text on top, frame below, each on its own stage with a dot grid.</p></div>
    {variant_b()}</section>

  <section class="opt"><div class="opt-head"><span class="k">C</span><h2>Paired cards</h2>
    <p>Three rows, each with two tiles of the same height: text and frame. The rows alternate left to right, and the frame always sits on its own stage instead of floating in an empty slab.</p></div>
    {variant_c()}</section>
</div>
<script>{JS}</script>
"""

if __name__ == "__main__":
    out = HERE / "out" / "variants.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(HTML, encoding="utf-8")
    print(out, len(HTML))
