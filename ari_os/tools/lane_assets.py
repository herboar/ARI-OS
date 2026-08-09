"""Static assets for the Lane Board — CSS and JS as plain string constants.

Everything here is scoped under `.lb` (or `html[data-lb-*]`) so nothing leaks
into the existing System 7 monitor chrome. Stdlib only, no CDN, no web fonts.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Board stylesheet. Dark warm-neutral by default; a light palette rides along
# behind the theme toggle. Colour carries meaning only.
# --------------------------------------------------------------------------
BOARD_CSS = """
.lb{
  --bg:#0E0E0D; --surface:#161615; --surface2:#1D1D1B; --line:#2C2C29;
  --text:#EDEAE3; --text2:#A5A096; --text3:#6E6A62;
  --amber:#E0A94A; --olive:#8FB04A; --red:#E0654A; --blue:#6E9BD6;
  --dirty:#D7834A;
  --redw:224,101,74; --amberw:224,169,74; --olivew:143,176,74; --bluew:110,155,214;
  --mono:ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,monospace;
  --ui:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
}
.lb[data-theme="light"]{
  --bg:#F6F4EF; --surface:#FFFFFF; --surface2:#F1EEE7; --line:#E1DDD3;
  --text:#1B1A17; --text2:#5D584F; --text3:#8C867B;
  --amber:#A9741A; --olive:#5C7A1E; --red:#BE4227; --blue:#35618F;
  --dirty:#B25E22;
}
.lb,.lb *{box-sizing:border-box;}
.lb{font-family:var(--ui);font-size:13px;line-height:1.45;color:var(--text);
  background:var(--bg);border:1px solid var(--line);border-radius:16px;
  max-width:1180px;margin:22px auto;padding:16px 16px 20px;text-align:left;
  -webkit-font-smoothing:antialiased;}
.lb a{color:var(--blue);}
.lb .num{font-variant-numeric:tabular-nums;font-family:var(--mono);}

/* ---------- header ---------- */
.lb-head{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
  padding:2px 4px 13px;border-bottom:1px solid var(--line);margin-bottom:13px;}
.lb-h1{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em;display:flex;
  align-items:baseline;gap:10px;}
.lb-h1 .lb-when{font-family:var(--mono);font-size:11px;font-weight:400;color:var(--text3);
  font-variant-numeric:tabular-nums;}
.lb-ctrls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;}
.lb-grp{display:flex;align-items:center;gap:6px;}
.lb-grp>span{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--text3);}
.lb-seg{display:inline-flex;background:var(--surface2);border:1px solid var(--line);
  border-radius:9px;padding:2px;}
.lb-seg button{appearance:none;background:transparent;border:0;color:var(--text2);
  font:inherit;font-size:11.5px;padding:3px 9px;border-radius:7px;cursor:pointer;line-height:1.5;}
.lb-seg button:hover{color:var(--text);}
.lb-seg button[aria-pressed="true"]{background:var(--surface);color:var(--text);
  box-shadow:0 1px 2px rgba(0,0,0,.30);}

/* ---------- banners ---------- */
.lb-banner{display:flex;gap:9px;align-items:baseline;padding:9px 12px;border-radius:11px;
  font-size:12px;margin:0 0 10px;border:1px solid var(--line);background:var(--surface);
  color:var(--text2);}
.lb-banner b{font-weight:600;}
.lb-banner .lb-ev{white-space:normal;}
.lb-banner.warn{border-color:rgba(var(--amberw),.45);background:rgba(var(--amberw),.10);color:var(--amber);}
.lb-banner.alarm{border-color:rgba(var(--redw),.5);background:rgba(var(--redw),.12);color:var(--red);}
.lb-banner.info{border-color:rgba(var(--bluew),.45);background:rgba(var(--bluew),.10);color:var(--blue);}
.lb-stale{display:none;}
.lb[data-stale="1"] .lb-stale{display:flex;}
.lb-offline{display:none;position:sticky;top:0;z-index:5;}
html[data-lb-offline="1"] .lb-offline{display:flex;}

/* ---------- repo regions ---------- */
.lb-repo{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:11px 12px 6px;margin:0 0 13px;}
.lb-repo-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  padding:2px 4px 9px;border-bottom:1px solid var(--line);margin-bottom:2px;}
.lb-repo-name{font-weight:600;font-size:13.5px;display:flex;align-items:center;gap:8px;}
.lb-accent{width:8px;height:8px;border-radius:50%;background:var(--text3);flex:none;}
.lb-repo[data-key="animarek"] .lb-accent{background:var(--amber);}
.lb-repo[data-key="xfactor"] .lb-accent{background:var(--blue);}
.lb-repo-stats{margin-left:auto;display:flex;gap:14px;flex-wrap:wrap;
  font-family:var(--mono);font-size:11px;color:var(--text3);font-variant-numeric:tabular-nums;}
.lb-repo-stats b{color:var(--text2);font-weight:600;}
.lb-repo-sub{width:100%;font-family:var(--mono);font-size:11px;color:var(--text3);
  margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.lb-hidden-note{padding:6px 10px;font-size:11.5px;color:var(--text3);}
.lb-hidden-note:empty{display:none;}

/* ---------- rows ---------- */
.lb-row{border-top:1px solid var(--line);}
.lb-lanes>.lb-row:first-child{border-top:0;}
.lb-row>summary{list-style:none;display:grid;gap:11px;align-items:center;cursor:pointer;
  padding:9px 8px;border-left:2px solid transparent;border-radius:0 9px 9px 0;}
.lb-row>summary::-webkit-details-marker{display:none;}
.lb-row>summary:hover{background:var(--surface2);}
.lb-row[data-verdict="RESCUE"]>summary{border-left-color:var(--red);}
.lb-row[data-verdict="DIRTY"]>summary{border-left-color:var(--dirty);}
.lb-row[data-verdict="MERGE"]>summary{border-left-color:var(--amber);}
.lb-row[data-verdict="PARKED"]>summary{border-left-color:var(--blue);}
.lb[data-density="compact"] .lb-row>summary{padding:4px 8px;gap:8px;}
.lb[data-clean="hide"] .lb-row[data-verdict="CLEAN"]{display:none;}

.lb-name{min-width:0;}
.lb-name .n{font-weight:600;font-size:12.5px;display:flex;align-items:center;gap:7px;}
.lb-name .n .lb-kind{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--text3);border:1px solid var(--line);border-radius:5px;padding:0 4px;}
.lb-name .p{color:var(--text2);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;}
.lb-name .p.none{color:var(--text3);font-style:italic;}
.lb[data-density="compact"] .lb-name .p{display:none;}
.lb-chev{color:var(--text3);font-size:15px;line-height:1;transition:transform .12s ease;
  display:inline-block;}
.lb-row[open]>summary .lb-chev{transform:rotate(90deg);}

/* ---------- verdict pills ---------- */
.lb-pill{display:inline-flex;align-items:center;gap:6px;font-size:10px;font-weight:600;
  letter-spacing:.09em;padding:3px 9px;border-radius:999px;white-space:nowrap;
  border:1px solid var(--line);color:var(--text3);background:var(--surface2);}
.lb-pill[data-v="RESCUE"]{color:var(--red);border-color:rgba(var(--redw),.5);background:rgba(var(--redw),.13);}
.lb-pill[data-v="DIRTY"]{color:var(--dirty);border-color:rgba(var(--amberw),.45);background:rgba(var(--amberw),.10);}
.lb-pill[data-v="MERGE"]{color:var(--amber);border-color:rgba(var(--amberw),.45);background:rgba(var(--amberw),.10);}
.lb-pill[data-v="PARKED"]{color:var(--blue);border-color:rgba(var(--bluew),.45);border-style:dashed;background:transparent;}
.lb-pill[data-v="CLEAN"]{color:var(--text3);background:transparent;}
.lb-pill[data-v="SWEEP"]{color:var(--text2);background:transparent;border-style:dotted;}

/* ---------- divergence ---------- */
.lb-drift{display:flex;flex-direction:column;gap:2px;min-width:0;}
.lb-div{display:flex;align-items:center;height:8px;}
.lb-div .h{width:44px;display:flex;align-items:center;}
.lb-div .h.l{justify-content:flex-end;}
.lb-div i{display:block;height:4px;border-radius:2px;}
.lb-div .h.l i{background:var(--text3);}
.lb-div .h.r i{background:var(--amber);}
.lb-div .tick{width:1px;height:9px;background:var(--line);margin:0 4px;flex:none;}
.lb-figs{font-family:var(--mono);font-size:10px;color:var(--text3);
  font-variant-numeric:tabular-nums;letter-spacing:.02em;white-space:nowrap;}
.lb-files{font-family:var(--mono);font-size:10.5px;color:var(--text2);
  font-variant-numeric:tabular-nums;white-space:nowrap;}
.lb-files.hot{color:var(--red);}
.lb-files.muted{color:var(--text3);}
.lb-age{font-family:var(--mono);font-size:11px;color:var(--text3);
  font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;}

/* ---------- actors / liveness ---------- */
.lb-actors{display:flex;flex-direction:column;gap:3px;min-width:0;}
.lb-actor{display:flex;align-items:baseline;gap:7px;min-width:0;}
.lb-dot{width:8px;height:8px;border-radius:50%;flex:none;align-self:center;}
.lb-dot[data-l="live"]{background:var(--olive);box-shadow:0 0 0 3px rgba(var(--olivew),.18);}
.lb-dot[data-l="warm"]{background:var(--amber);}
.lb-dot[data-l="cold"]{background:var(--text3);}
.lb-dot[data-l="unknown"]{background:transparent;border:1px dashed var(--blue);width:9px;height:9px;}
.lb-alabel{font-size:11.5px;color:var(--text2);white-space:nowrap;}
.lb-lv{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--text3);}
.lb-lv[data-l="unknown"]{color:var(--blue);}
.lb-ev{font-family:var(--mono);font-size:10.5px;color:var(--text3);min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.lb-actor[data-conflict="1"]{border:1px solid rgba(var(--redw),.55);background:rgba(var(--redw),.11);
  border-radius:8px;padding:2px 8px;}
.lb-actor[data-conflict="1"] .lb-ev{color:var(--red);}
.lb-flag{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--red);
  font-weight:600;white-space:nowrap;}
.lb-ro{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);
  border:1px solid var(--line);border-radius:5px;padding:0 4px;}
.lb-none{font-size:11px;color:var(--text3);font-style:italic;}

/* ---------- rail ----------
   The spine is a CSS line, not part of each row's SVG, so it stays continuous
   no matter how tall a row grows. Only the branch curve lives in the SVG. */
.lb-railcell{position:relative;width:96px;align-self:stretch;display:flex;align-items:center;}
.lb-railcell::before{content:"";position:absolute;left:23px;top:0;bottom:0;width:2px;
  background:var(--line);}
.lb-lanes>.lb-row:first-child .lb-railcell::before{top:50%;}
.lb-rail{width:96px;height:64px;display:block;flex:none;position:relative;}
.lb-rail .branch{fill:none;stroke-width:1.75;stroke-linecap:round;}
.lb-rail .node{stroke-width:1.5;}

/* ---------- expansion ---------- */
.lb-body{padding:4px 12px 16px 20px;border-left:2px solid var(--line);margin-left:8px;}
.lb-purpose{font-size:13px;font-weight:600;margin:6px 0 2px;}
.lb-purpose.none{font-weight:400;color:var(--text3);font-style:italic;}
.lb-kv{display:flex;flex-wrap:wrap;gap:4px 18px;margin:6px 0 10px;
  font-family:var(--mono);font-size:11px;color:var(--text3);}
.lb-kv b{color:var(--text2);font-weight:600;}
.lb-sec{margin:12px 0 0;}
.lb-sec>h4{margin:0 0 6px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--text3);font-weight:600;}
.lb-cmd{display:flex;align-items:center;gap:10px;background:var(--surface2);
  border:1px solid var(--line);border-radius:9px;padding:6px 8px;margin:4px 0;}
.lb-cmd code{font-family:var(--mono);font-size:11px;color:var(--text2);
  overflow:auto;white-space:nowrap;flex:1;}
.lb-cmd button{appearance:none;border:1px solid var(--line);background:var(--surface);
  color:var(--text2);font:inherit;font-size:10.5px;border-radius:7px;padding:3px 9px;cursor:pointer;flex:none;}
.lb-cmd button:hover{color:var(--text);}
.lb-manual{border:1px dashed var(--line);border-radius:9px;padding:8px 10px;
  font-size:11.5px;color:var(--text2);}
.lb-manual .lb-path{display:block;margin-top:4px;}
.lb-path{font-family:var(--mono);font-size:10.5px;color:var(--text3);word-break:break-all;}
.lb-overlap{border:1px solid rgba(var(--amberw),.4);background:rgba(var(--amberw),.08);
  border-radius:9px;padding:8px 10px;font-size:11.5px;color:var(--amber);}
.lb-overlap ul{margin:5px 0 0;padding-left:16px;}
.lb-overlap li{font-family:var(--mono);font-size:10.5px;color:var(--text2);}
.lb-hist{list-style:none;margin:0;padding:0;}
.lb-hist li{display:grid;grid-template-columns:62px 108px 1fr 62px;gap:10px;
  padding:3px 0;border-top:1px solid var(--line);font-size:11.5px;align-items:baseline;}
.lb-hist li:first-child{border-top:0;}
.lb-hist .a{font-family:var(--mono);font-size:10.5px;color:var(--text3);}
.lb-hist .w{color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.lb-hist .t{color:var(--text);}
.lb-hist .s{font-family:var(--mono);font-size:10.5px;color:var(--text3);text-align:right;}

/* ---------- footer / errors ---------- */
.lb-foot{margin-top:6px;border-top:1px solid var(--line);padding-top:12px;}
.lb-foot h4{margin:0 0 6px;font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--text3);font-weight:600;}
.lb-err{display:flex;gap:9px;align-items:baseline;font-size:11.5px;color:var(--text2);
  padding:3px 0;}
.lb-err .sc{font-family:var(--mono);font-size:10.5px;color:var(--red);flex:none;}

/* ---------- view switching ---------- */
.lb-view{display:none;}
.lb[data-view="rail"] .lb-view[data-v="rail"]{display:block;}
.lb[data-view="dense"] .lb-view[data-v="dense"]{display:block;}
.lb-view[data-v="dense"] .lb-row>summary{
  grid-template-columns:88px minmax(150px,1.15fr) 100px 78px 46px minmax(170px,1.25fr) 14px;}
.lb-view[data-v="rail"] .lb-row>summary{
  grid-template-columns:96px minmax(200px,1fr) auto auto 14px;padding:0 10px 0 0;gap:14px;
  min-height:64px;border-left:0;border-radius:9px;}
.lb-view[data-v="rail"] .lb-row>summary .lb-actors{margin-top:3px;}
.lb-view[data-v="rail"] .lb-row{border-top:0;}
@media (max-width:820px){
  .lb-view[data-v="dense"] .lb-row>summary{grid-template-columns:80px 1fr 14px;}
  .lb-view[data-v="dense"] .lb-drift,.lb-view[data-v="dense"] .lb-files,
  .lb-view[data-v="dense"] .lb-age{display:none;}
}
"""

# --------------------------------------------------------------------------
# Board behaviour: view prefs (localStorage, same idea as the hue picker),
# sorting, clean-lane filter, ticking freshness clock, copy buttons.
# Idempotent — safe to re-run after a DOM swap via window.__lbApply().
# --------------------------------------------------------------------------
BOARD_JS = """
(function(){
  var K={view:'lbView',density:'lbDensity',sort:'lbSort',clean:'lbClean',theme:'lbTheme'};
  var D={view:'dense',density:'comfortable',sort:'verdict',clean:'show',theme:'dark'};
  function get(k){try{var v=localStorage.getItem(K[k]);return v===null?D[k]:v}catch(e){return D[k]}}
  function set(k,v){try{localStorage.setItem(K[k],v)}catch(e){}}
  function each(sel,root,fn){
    var n=(root||document).querySelectorAll(sel);
    for(var i=0;i<n.length;i++)fn(n[i],i);
  }
  function sortRows(board,mode){
    each('.lb-lanes',board,function(list){
      var rows=[],i;
      for(i=0;i<list.children.length;i++){
        if(list.children[i].classList.contains('lb-row'))rows.push(list.children[i]);
      }
      rows.sort(function(a,b){
        var pa=+a.getAttribute('data-first'),pb=+b.getAttribute('data-first');
        if(pa!==pb)return pb-pa;                      /* main tree stays pinned first */
        if(mode==='age')return (+b.getAttribute('data-age'))-(+a.getAttribute('data-age'));
        if(mode==='name')return a.getAttribute('data-name').localeCompare(b.getAttribute('data-name'));
        var ra=+a.getAttribute('data-rank'),rb=+b.getAttribute('data-rank');
        if(ra!==rb)return ra-rb;
        return (+b.getAttribute('data-age'))-(+a.getAttribute('data-age'));
      });
      for(i=0;i<rows.length;i++)list.appendChild(rows[i]);
    });
  }
  function countHidden(board){
    var hide=board.getAttribute('data-clean')==='hide';
    each('.lb-lanes',board,function(list){
      var note=list.parentNode.querySelector('.lb-hidden-note');
      if(!note)return;
      var n=list.querySelectorAll('.lb-row[data-verdict="CLEAN"]').length;
      note.textContent=(hide&&n)?(n+' clean lane'+(n===1?'':'s')+' hidden'):'';
    });
  }
  function syncControls(board){
    each('[data-lb-set]',board,function(btn){
      var p=btn.getAttribute('data-lb-set').split(':');
      btn.setAttribute('aria-pressed',String(board.getAttribute('data-'+p[0])===p[1]));
    });
  }
  function apply(){
    each('.lb',document,function(b){
      if(!b.hasAttribute('data-pin'))b.setAttribute('data-view',get('view'));
      b.setAttribute('data-density',get('density'));
      b.setAttribute('data-theme',get('theme'));
      b.setAttribute('data-clean',get('clean'));
      b.setAttribute('data-sort',get('sort'));
      sortRows(b,get('sort'));countHidden(b);syncControls(b);
      b.__t0=Date.now();b.__age0=parseFloat(b.getAttribute('data-age-s')||'0');
    });
    tick();
  }
  function tick(){
    each('.lb',document,function(b){
      if(b.__t0===undefined){b.__t0=Date.now();b.__age0=parseFloat(b.getAttribute('data-age-s')||'0');}
      var s=b.__age0+(Date.now()-b.__t0)/1000;
      var txt=s<90?(Math.round(s)+'s'):(s<5400?(Math.round(s/60)+'m'):(Math.round(s/3600)+'h'));
      each('[data-lb-clock]',b,function(el){el.textContent='snapshot '+txt+' old';});
      each('[data-lb-staleage]',b,function(el){el.textContent=txt;});
      b.setAttribute('data-stale',s>15?'1':'0');
    });
  }
  document.addEventListener('click',function(ev){
    var t=ev.target;
    while(t&&t!==document){
      if(t.getAttribute&&t.getAttribute('data-lb-set')){
        var p=t.getAttribute('data-lb-set').split(':');
        set(p[0],p[1]);apply();ev.preventDefault();return;
      }
      if(t.getAttribute&&t.getAttribute('data-lb-copy')!==null){
        var cmd=t.getAttribute('data-lb-copy'),old=t.textContent;
        var done=function(){t.textContent='copied';setTimeout(function(){t.textContent=old;},1200);};
        if(navigator.clipboard&&navigator.clipboard.writeText){
          navigator.clipboard.writeText(cmd).then(done,function(){});
        }else{
          var ta=document.createElement('textarea');ta.value=cmd;document.body.appendChild(ta);
          ta.select();try{document.execCommand('copy');done();}catch(e){}
          document.body.removeChild(ta);
        }
        ev.preventDefault();return;
      }
      t=t.parentNode;
    }
  },true);
  window.__lbApply=apply;
  if(!window.__lbTimer)window.__lbTimer=setInterval(tick,1000);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',apply);
  else apply();
})();
"""

# --------------------------------------------------------------------------
# Monitor-side live refresh: fetch /state.json every 5s and swap three
# containers in place. Replaces <meta http-equiv=refresh>, which failed
# silently when the server died. A failed fetch raises the OFFLINE banner.
# --------------------------------------------------------------------------
MONITOR_JS = """
(function(){
  var INTERVAL=5000;
  function openKeys(){
    var out=[],n=document.querySelectorAll('#lb-root details[open]');
    for(var i=0;i<n.length;i++){var k=n[i].getAttribute('data-key');if(k)out.push(k);}
    return out;
  }
  function put(id,html){var el=document.getElementById(id);if(el&&html!==undefined)el.innerHTML=html;}
  function poll(){
    fetch('/state.json',{cache:'no-store'}).then(function(r){
      if(!r.ok)throw new Error('http '+r.status);return r.json();
    }).then(function(d){
      var open=openKeys(),i,n;
      put('lb-root',d.board_html);
      put('lb-workers',d.workers_html);
      put('lb-questions',d.questions_html);
      n=document.querySelectorAll('#lb-root details');
      for(i=0;i<n.length;i++){
        if(open.indexOf(n[i].getAttribute('data-key'))>=0)n[i].open=true;
      }
      document.documentElement.removeAttribute('data-lb-offline');
      if(window.__lbApply)window.__lbApply();
    }).catch(function(){
      document.documentElement.setAttribute('data-lb-offline','1');
    });
  }
  setInterval(poll,INTERVAL);
})();
"""
