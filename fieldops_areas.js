(function(){
  const NOTES = D.area_notes || {};
  const PHOTOS = D.area_photos || {};
  const CMAP = D.catalog_map || {};
  const _qs = new URLSearchParams(location.search);
  if (_qs.get('present')==='1') D.present = true;
  if (_qs.get('walk')==='1') D.walk = true;
  if (D.present) document.body.classList.add('pd-present');
  if (D.walk) document.body.classList.add('pd-walk');

  function areaCount(fn){
    return DEV.filter(d => d.plan === fn).length;
  }
  function postJSON(url, body, label){
    const send = window.foSend || fetch;
    return send(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}, label || 'Plan update')
      .then(async r => {
        const d = await r.json().catch(()=>({}));
        if (r.queued) return {ok:true, queued:true, ...d};
        if (!r.ok) throw new Error(d.error || 'Not saved');
        return d;
      });
  }
  function applyPayload(d){
    if (d.plan_info) P = d.plan_info;
    if (d.devices) DEV = d.devices;
    if (d.area_notes) Object.keys(d.area_notes).forEach(k => NOTES[k] = d.area_notes[k]);
    if (d.area_photos) Object.keys(d.area_photos).forEach(k => PHOTOS[k] = d.area_photos[k]);
    if (d.catalog_map) Object.assign(CMAP, d.catalog_map);
    if (typeof d.quote_id !== 'undefined') D.quote_id = d.quote_id;
  }
  function areaName(p){ return p && p.name ? p.name : 'Area'; }

  /* image base for share present */
  const _draw = draw;
  draw = function(){
    _draw();
    if (D.plan_base){
      const img = $('pd-img');
      const h = img.getAttribute('href') || '';
      if (h.indexOf('/uploads/') === 0){
        img.setAttribute('href', D.plan_base + h.slice('/uploads/'.length));
      }
    }
  };

  /* top actions + scale prompt + count */
  (function buildChrome(){
    const top = $('pd-topx') || document.querySelector('.pd-top');
    if (top && !$('pd-walk')){
      const box = document.createElement('span');
      box.style.cssText = 'display:flex;gap:6px;margin-left:auto;align-items:center';
      box.innerHTML =
        (EDIT && !D.present ? '<a class="pd-btn gold" id="pd-walk" href="?walk=1">Start walk</a>' : '') +
        (D.walk ? '<a class="pd-btn" href="?">Done walking</a>' : '') +
        '<a class="pd-btn" id="pd-pdfbtn" target="_blank" href="/job/'+encodeURIComponent(JID)+(D.present?'/plan.pdf':'/plan.pdf')+'">PDF</a>' +
        (!D.present ? '<a class="pd-btn" id="pd-present" href="?present=1" target="_blank">Present</a>' : '<a class="pd-btn" href="?">Exit present</a>');
      top.appendChild(box);
    }
    const main = document.querySelector('.pd-main');
    if (!main) return;
    if (!$('pd-areas')){
      const side = document.createElement('div');
      side.className = 'pd-areas';
      side.id = 'pd-areas';
      main.insertBefore(side, main.firstChild);
    }
    if (!$('pd-areabar')){
      const bar = document.createElement('div');
      bar.className = 'pd-areabar';
      bar.id = 'pd-areabar';
      main.insertBefore(bar, $('pd-view'));
    }
    if (!$('pd-toolrow')){
      const row = document.createElement('div');
      row.className = 'pd-toolrow';
      row.id = 'pd-toolrow';
      row.innerHTML =
        '<strong id="pd-toolname" style="margin-right:6px"></strong>' +
        (EDIT?'<button class="pd-btn" onclick="renamePlan()" title="Rename">Rename</button>':'') +
        (EDIT?'<button class="pd-btn" id="pd-dup" type="button">Duplicate</button>':'') +
        '<button class="pd-btn pd-noscale hidden" id="pd-noscale" type="button">No scale, set it</button>' +
        (EDIT?'<button class="pd-btn edit-only" type="button" onclick="pickFile()">Background</button>':'') +
        (D.office && EDIT && !D.present?'<button class="pd-btn gold edit-only" id="pd-count" type="button">Count into quote</button>':'');
      const view = $('pd-view');
      view.parentNode.insertBefore(row, view);
    }
    if (!$('pd-walkbar')){
      const wb = document.createElement('div');
      wb.className = 'pd-walkbar';
      wb.id = 'pd-walkbar';
      wb.innerHTML = '<button class="pd-btn" id="pd-wprev" type="button">Back</button>' +
        '<div style="flex:1;font-weight:800" id="pd-wlabel"></div>' +
        '<button class="pd-btn gold" id="pd-wnext" type="button">Next area</button>';
      document.body.appendChild(wb);
    }
    if (!$('pd-notes')){
      const card = document.createElement('div');
      card.className = 'pd-card pd-notes';
      card.id = 'pd-notes';
      card.innerHTML = '<h3>Area notes</h3>' +
        (EDIT && !D.present
          ? '<textarea id="pd-notetxt" placeholder="Notes for this area"></textarea><div class="pd-muted">Saved with this area.</div>'
          : '<div class="pd-muted present-show" id="pd-noteread">No notes.</div>') +
        '<h3 style="margin-top:14px">Photos</h3>' +
        (EDIT && !D.present
          ? '<div class="pd-row"><label class="pd-btn pd-cam">Camera or photo<input id="pd-cam" type="file" accept="image/*" capture="environment" style="display:none"></label></div>'
          : '') +
        '<div class="pd-phgrid" id="pd-phgrid"></div>' +
        '<div class="pd-muted" id="pd-phempty">No photos yet.</div>';
      const legend = $('pd-legend');
      if (legend && legend.parentNode) legend.parentNode.insertBefore(card, legend.nextSibling);
      else $('pd-side').appendChild(card);
    }
  })();

  function renderAreas(){
    const list = P.map((p,i) => {
      const n = areaCount(p.fn);
      const on = i===cur ? ' on' : '';
      return {p,i,n,on};
    });
    const desktop = $('pd-areas');
    if (desktop){
      desktop.innerHTML = '<h3>AREAS</h3><div class="pd-alist">' +
        list.map(x => '<button type="button" class="pd-area'+x.on+'" data-i="'+x.i+'"><span>'+esc(areaName(x.p))+'</span><b>'+x.n+'</b></button>').join('') +
        '</div>' +
        (EDIT && !D.present
          ? '<button class="pd-btn" id="pd-addarea" type="button">+ Add area</button>' +
            (P.length>1?'<button class="pd-btn" id="pd-moveareas" type="button">Move areas</button>':'')
          : '');
      desktop.querySelectorAll('.pd-area').forEach(b => b.onclick = () => go(+b.dataset.i));
      if ($('pd-addarea')) $('pd-addarea').onclick = addArea;
      if ($('pd-moveareas')) $('pd-moveareas').onclick = moveAreas;
    }
    const bar = $('pd-areabar');
    if (bar){
      bar.innerHTML = list.map(x => '<button type="button" class="pd-chip'+x.on+'" data-i="'+x.i+'">'+esc(areaName(x.p))+' <b>'+x.n+'</b></button>').join('') +
        (EDIT && !D.present ? '<button type="button" class="pd-chip" id="pd-addarea2">+ Add area</button>' : '');
      bar.querySelectorAll('.pd-chip[data-i]').forEach(b => b.onclick = () => go(+b.dataset.i));
      if ($('pd-addarea2')) $('pd-addarea2').onclick = addArea;
    }
  }

  function renderNotes(){
    const p = plan();
    const card = $('pd-notes');
    if (!card) return;
    card.classList.toggle('hidden', !p);
    if (!p) return;
    const notes = NOTES[p.fn] || '';
    if ($('pd-notetxt') && document.activeElement !== $('pd-notetxt')) $('pd-notetxt').value = notes;
    if ($('pd-noteread')) $('pd-noteread').textContent = notes || 'No notes.';
    const photos = PHOTOS[p.fn] || [];
    const grid = $('pd-phgrid');
    if (grid){
      grid.innerHTML = photos.map(ph => {
        const src = ph.url || ('/uploads/'+ph.filename);
        return '<div class="pd-ph"><img src="'+esc(src)+'" alt="">' +
          (EDIT && !D.present ? '<button type="button" data-id="'+ph.id+'" aria-label="Remove photo">&times;</button>' : '') +
          '</div>';
      }).join('');
      grid.querySelectorAll('button[data-id]').forEach(b => b.onclick = () => delPhoto(+b.dataset.id));
    }
    if ($('pd-phempty')) $('pd-phempty').classList.toggle('hidden', photos.length>0);
  }

  function renderScaleBtn(){
    const p = plan();
    const btn = $('pd-noscale');
    const name = $('pd-toolname');
    if (name) name.textContent = p ? areaName(p) : '';
    if (btn){
      const need = !!(p && !p.ppf && EDIT && !D.present);
      btn.classList.toggle('hidden', !need);
    }
    const wlab = $('pd-wlabel');
    if (wlab) wlab.textContent = p ? (areaName(p)+'  '+areaCount(p.fn)) : '';
    if ($('pd-wprev')) $('pd-wprev').disabled = cur<=0;
    if ($('pd-wnext')) {
      $('pd-wnext').textContent = cur>=P.length-1 ? 'Done' : 'Next area';
    }
  }

  const _tabs = tabs;
  tabs = function(){
    if (window.matchMedia && window.matchMedia('(min-width:900px)').matches){
      $('pd-tabs').innerHTML = '';
    } else {
      _tabs();
    }
    renderAreas();
  };

  function paintLegend(){
    const L = $('pd-legend');
    if (!L) return;
    const all = {}, here = {};
    DEV.forEach(d => all[d.type] = (all[d.type]||0)+1);
    planDevs().forEach(d => here[d.type] = (here[d.type]||0)+1);
    if (!DEV.length){ L.innerHTML = '<h3>Legend</h3><div class="pd-muted">No devices yet</div>'; return; }
    const canEdit = !!(EDIT && D.office && !D.present);
    L.innerHTML = '<h3>Legend</h3>' + TYPES.filter(t => all[t[0]]).map(t => {
      const n = here[t[0]] || 0;
      const edit = canEdit ? '<button type="button" class="pd-leg-edit" data-type="'+esc(t[0])+'" aria-label="Edit '+esc(t[0])+'">&#9998;</button>' : '';
      return '<div class="pd-leg">'+symHtml(t[0],26)+'<span>'+esc(t[0])+'</span><b>'+n+'</b>'+edit+'</div>';
    }).join('');
    L.querySelectorAll('.pd-leg-edit').forEach(b => b.onclick = ev => { ev.preventDefault(); ev.stopPropagation(); mapSymbol(b.dataset.type); });
  }
  const _legend = legend;
  legend = function(){ paintLegend(); };

  async function mapSymbol(typ){
    const items = D.catalog_items || [];
    const cur = (CMAP[typ] || {}).name || '';
    const names = items.map(c => c.name);
    const pick = prompt('Catalog item for '+typ+(cur?' (now '+cur+')':'' )+'\n\nLeave blank to clear the mapping.', cur || typ);
    if (pick === null) return;
    const hit = items.find(c => c.name.toLowerCase() === pick.trim().toLowerCase()) || items.find(c => (c.name||'').toLowerCase().indexOf(pick.trim().toLowerCase())>=0);
    const catalog_id = pick.trim() ? (hit ? hit.id : 0) : 0;
    if (pick.trim() && !catalog_id){ alert('No catalog item matches that name.'); return; }
    try{
      const d = await postJSON(API+'/areas/map-symbol', {type: typ, catalog_id: catalog_id}, 'Symbol map');
      if (d.catalog_map) Object.assign(CMAP, d.catalog_map);
      paintLegend();
    }catch(e){ alert(e.message||'Could not save the mapping'); }
  }

  const prevSide = window.PD_SIDE;
  window.PD_SIDE = function(){
    if (prevSide) prevSide();
    paintLegend();
    renderAreas();
    renderNotes();
    renderScaleBtn();
  };

  async function addArea(){
    const n = prompt('Area name (for example Lobby)', 'Area '+(P.length+1));
    if (n===null || !n.trim()) return;
    try{
      const d = await postJSON(API+'/areas', {name:n.trim()}, 'New area');
      applyPayload(d);
      if (d.queued){ status('Saved on this phone.'); return; }
      cur = Math.max(0, P.findIndex(p => p.fn===d.filename));
      if (cur<0) cur = P.length-1;
      vb=null; sel=null; render();
    }catch(e){ alert(e.message||'Could not add the area'); }
  }

  async function dupArea(){
    const p = plan(); if (!p) return;
    try{
      const d = await postJSON(API+'/areas/duplicate', {filename:p.fn}, 'Duplicate area');
      applyPayload(d);
      if (d.queued){ status('Saved on this phone.'); return; }
      cur = Math.max(0, P.findIndex(x => x.fn===d.filename));
      vb=null; render();
    }catch(e){ alert(e.message||'Could not duplicate'); }
  }

  function moveAreas(){
    const wrap = document.createElement('div');
    wrap.className = 'pd-move';
    wrap.innerHTML = '<div class="pd-sheet"><h3 style="margin:0 0 8px">Move areas</h3><ul id="pd-movelist"></ul><div class="pd-row" style="margin-top:12px"><button class="pd-btn gold" type="button" id="pd-movesave">Save order</button><button class="pd-btn" type="button" id="pd-movecancel">Cancel</button></div></div>';
    document.body.appendChild(wrap);
    let order = P.map(p => p.fn);
    function paint(){
      $('pd-movelist').innerHTML = order.map((fn,i) => {
        const p = P.find(x => x.fn===fn) || {name:fn};
        return '<li data-fn="'+esc(fn)+'"><span style="flex:1">'+esc(areaName(p))+'</span>' +
          '<button type="button" class="pd-btn" data-d="-1" '+(i===0?'disabled':'')+'>Up</button>' +
          '<button type="button" class="pd-btn" data-d="1" '+(i===order.length-1?'disabled':'')+'>Down</button></li>';
      }).join('');
      $('pd-movelist').querySelectorAll('button').forEach(b => b.onclick = () => {
        const li = b.closest('li');
        const i = order.indexOf(li.dataset.fn);
        const j = i + (+b.dataset.d);
        if (j<0 || j>=order.length) return;
        const t = order[i]; order[i]=order[j]; order[j]=t;
        paint();
      });
    }
    paint();
    $('pd-movecancel').onclick = () => wrap.remove();
    $('pd-movesave').onclick = async () => {
      try{
        const d = await postJSON(API+'/areas/reorder', {order:order}, 'Move areas');
        applyPayload(d);
        wrap.remove();
        const p = plan();
        cur = Math.max(0, P.findIndex(x => p && x.fn===p.fn));
        render();
      }catch(e){ alert(e.message||'Could not reorder'); }
    };
  }

  let noteT = null;
  document.addEventListener('input', e => {
    if (e.target && e.target.id==='pd-notetxt'){
      const p = plan(); if (!p) return;
      NOTES[p.fn] = e.target.value;
      clearTimeout(noteT);
      noteT = setTimeout(() => {
        postJSON(API+'/areas/notes', {filename:p.fn, notes:e.target.value}, 'Area notes').catch(()=>{});
      }, 400);
    }
  });

  document.addEventListener('change', async e => {
    if (!(e.target && e.target.id==='pd-cam')) return;
    const p = plan(); if (!p || !e.target.files || !e.target.files[0]) return;
    const fd = new FormData();
    fd.append('filename', p.fn);
    fd.append('photo', e.target.files[0], e.target.files[0].name || 'photo.jpg');
    const send = window.foSend || fetch;
    try{
      const r = await send(API+'/areas/photos', {method:'POST', body:fd}, 'Area photo');
      if (r.queued){ status('Photo saved on this phone.'); e.target.value=''; return; }
      const d = await r.json().catch(()=>({}));
      if (!r.ok){ alert(d.error||'Could not add the photo'); return; }
      applyPayload(d);
      renderNotes();
    }catch(err){ alert('Could not add the photo'); }
    e.target.value='';
  });

  async function delPhoto(id){
    if (!confirm('Remove this photo?')) return;
    try{
      const d = await postJSON(API+'/areas/photos/'+id+'/delete', {}, 'Remove photo');
      applyPayload(d);
      renderNotes();
    }catch(e){ alert(e.message||'Could not remove'); }
  }

  async function countIntoQuote(){
    const p = plan();
    if (!p){ alert('Add an area first.'); return; }
    const here = areaCount(p.fn), all = DEV.length;
    let scope = 'area';
    if (P.length>1 && all!==here){
      const pick = confirm('Count devices on '+areaName(p)+' ('+here+')?\n\nOK = this area only. Cancel opens a choice for all areas.');
      if (!pick){
        if (!confirm('Count all areas ('+all+' devices) into the quote?')) return;
        scope = 'all';
      }
    } else if (!confirm('Count '+here+' device'+(here===1?'':'s')+' on '+areaName(p)+' into the quote?')){
      return;
    }
    try{
      const d = await postJSON(API+'/areas/count-into-quote', {scope:scope, filename:p.fn}, 'Count into quote');
      if (d.queued){ status('Saved on this phone.'); return; }
      applyPayload(d);
      let msg = 'Quote updated. '+((d.lines||[]).length)+' line'+(d.lines&&d.lines.length===1?'':'s')+'.';
      if (d.unmapped && d.unmapped.length) msg += ' No catalog match for: '+d.unmapped.join(', ')+'.';
      if (d.quote_id) msg += ' Open the quote to review prices.';
      if (d.quote_id && confirm(msg+' Open it now?')) location.href='/quotes/'+d.quote_id;
      else alert(msg);
    }catch(e){ alert(e.message||'Could not count into the quote'); }
  }

  /* wrap device save so offline uses the existing outbox */
  if (typeof save === 'function'){
    const _save = save;
    save = async function(){
      if (window.foSend){
        const prev = window.fetch;
        window.fetch = function(url, opt){
          opt = opt || {};
          const m = (opt.method||'GET').toString().toUpperCase();
          if (m!=='GET' && m!=='HEAD'){
            return foSend(url, opt, 'Plan devices').then(r => {
              if (r.queued){
                return {ok:true, status:200, json: async()=>({ok:true,queued:true,devices:DEV})};
              }
              return r;
            });
          }
          return prev.apply(this, arguments);
        };
        try { return await _save(); }
        finally { window.fetch = prev; }
        return;
      }
      return _save();
    };
  }

  if ($('pd-noscale')) $('pd-noscale').onclick = () => scaleStart();
  if ($('pd-dup')) $('pd-dup').onclick = dupArea;
  if ($('pd-count')) $('pd-count').onclick = countIntoQuote;
  if ($('pd-wprev')) $('pd-wprev').onclick = () => { if (cur>0) go(cur-1); };
  if ($('pd-wnext')) $('pd-wnext').onclick = () => {
    if (cur < P.length-1) go(cur+1);
    else location.href = location.pathname;
  };

  /* hide leftover top tabs on wide screens after first render */
  renderAreas();
  renderNotes();
  renderScaleBtn();
  requestAnimationFrame(function(){ if (typeof fit==='function'){ vb=null; fit(); draw(); } });
})();
