(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const spanLabels = row => {
    // Cells with rowSpan from earlier sibling rows also label this row
    // (e.g. a section title spanning a block of field rows).
    const body = row.parentElement;
    const rows = body ? [...body.children].filter(c => /^(TR)$/.test(c.tagName)) : [];
    const idx = rows.indexOf(row), out = [];
    if (idx < 0) return out;
    for (let i = 0; i < idx; i++)
      for (const c of rows[i].children) {
        if (!/^(TH|TD)$/.test(c.tagName) || i + (c.rowSpan || 1) - 1 < idx) continue;
        if (c.querySelector('button,input,select,textarea,a')) continue;
        const t = (c.innerText || '').replace(/\s+/g, ' ').trim();
        if (t && !out.includes(t)) out.push(t.slice(0, 60));
      }
    return out;
  };
  const rowLabel = e => {
    // Walk nested rows outward, outermost section label first, innermost row
    // label last, so repeated fields keep their enclosing section as context.
    const parts = [];
    for (let row = e.closest('tr,[role="row"]'); row; row = row.parentElement && row.parentElement.closest('tr,[role="row"]')) {
      const cells = [...row.children].filter(c =>
        /^(TH|TD)$/.test(c.tagName) && !c.contains(e) &&
        !c.querySelector('button,input,select,textarea,a'));
      const own = cells.map(c => (c.innerText || '').replace(/\s+/g, ' ').trim())
        .filter(Boolean).join(' ');
      const t = [...spanLabels(row), own].filter(Boolean).join(' / ');
      if (t && !parts.includes(t)) parts.unshift(t.slice(0, 80));
      if (parts.join(' / ').length > 90) break;
    }
    const group = e.closest('fieldset,[role="group"]');
    const gt = group && ((group.querySelector('legend') || {}).innerText ||
      group.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (gt) parts.unshift(gt.slice(0, 40));
    return parts.join(' / ').slice(0, 90);
  };
  const labelOf = e => {
    const row = rowLabel(e), n = name(e);
    if (n && n.length <= 60) return row ? row + ' / ' + n : n;
    return row || n || '';
  };
  // Validation failures surface mechanically: aria-invalid / errormessage
  // attributes, alert-role or *error*-classed nodes near the field, and error
  // wording embedded in the resolved label (this site inlines messages in the
  // label cell). A bare 必須 marker is not an error.
  const errorOf = (e, label) => {
    if (e.getAttribute('aria-invalid')==='true' || e.getAttribute('aria-errormessage')) return true;
    if (e.matches('[class*="error" i],[class*="invalid" i]')) return true;
    const scope = e.closest('tr,[role="row"],li,dd,div') || e.parentElement;
    const err = scope && scope.querySelector('[role="alert"],[class*="error" i],[class*="invalid" i]');
    if (err && (err.innerText||'').trim()) return true;
    return /誤り|エラー|できません|正しく|を入力して下さい|を入力してください|を選択して下さい|を選択してください|invalid|error/i.test(label||'');
  };
  const dialogRisk = e => {
    // True when a click handler invokes a native dialog (confirm/alert/prompt),
    // including one indirection through a page-defined function like
    // onclick="if(rtnMainMenu()==false)...". Library-qualified calls
    // (PrimeFaces.bcn etc.) don't resolve on window and are skipped.
    const src = e.getAttribute('onclick') || '';
    if (!src) return false;
    if (/\b(confirm|alert|prompt)\s*\(/.test(src)) return true;
    for (const m of src.matchAll(/\b([A-Za-z_$][\w$]*)\s*\(/g)) {
      const fn = m[1];
      if (['if','for','while','switch','catch','return','function','typeof'].includes(fn)) continue;
      try {
        const f = e.ownerDocument.defaultView[fn];
        if (typeof f === 'function' && f.toString().length < 5000 &&
            /\b(confirm|alert|prompt)\s*\(/.test(f.toString())) return true;
      } catch (_) {}
    }
    return false;
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const actions=[];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    // Bare table cells are layout, not controls — each row's real input/button
    // is already its own action. Clicking a cell only invites dead loops.
    if (e.tagName==='TD' || e.tagName==='TH') continue;
    // <label> elements only focus their control — the control itself is the
    // real fill target, so offering the label as a click invites dead loops.
    if (e.tagName==='LABEL') continue;
    const base={node:identity(e),role:rname,label:labelOf(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (dialogRisk(e)) { base.dialog = true; base.label += ' (ダイアログ有)'; }
    // Collapsible-region triggers (fieldset legends, aria-expanded controls,
    // toggle/collapse handlers) flip visibility back and forth — flag them so
    // the policy can park a ping-pong loop.
    if (e.tagName==='LEGEND' || e.closest('legend') ||
        e.getAttribute('aria-expanded')!==null ||
        /\b(toggle|collaps|expand|slide)/i.test(e.getAttribute('onclick')||'')) {
      base.toggle = true;
    }
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (e.required || e.getAttribute('aria-required')==='true' ||
        /必須/.test(base.label)) base.required=true;
    if (e.tagName==='INPUT') {
      if (e.maxLength>0 && e.maxLength<100000) base.maxlength=e.maxLength;
      const pat=e.getAttribute('pattern');
      if (pat) base.pattern=pat.slice(0,40);
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (errorOf(e, base.label)) { base.error = true; }
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable && rname==='combobox')
        actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  // Same-label fill targets (split inputs like phone/postal boxes) get a
  // position marker so each box can be told apart and filled with its part.
  const fillByLabel = {};
  for (const a of actions) if (a.kind === 'fill') (fillByLabel[a.label] ||= []).push(a);
  for (const list of Object.values(fillByLabel)) if (list.length > 1)
    list.forEach((a, i) => a.label += ` (${i + 1}/${list.length})`);
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000);
  // Pages may scroll inside an inner container (e.g. fixed-layout apps) while
  // the document itself does not scroll. Track the largest scrollable region.
  let scroller=null, spare=0;
  for (const e of document.body.querySelectorAll('*')) {
    const room=e.scrollHeight-e.clientHeight;
    if (room>spare && e.clientHeight>200 &&
        ['auto','scroll'].includes(getComputedStyle(e).overflowY)) { spare=room; scroller=e; }
  }
  const sy=scroller?scroller.scrollTop:scrollY,
    height=scroller?scroller.scrollHeight:document.documentElement.scrollHeight,
    vh=scroller?scroller.clientHeight:innerHeight;
  // Locked/confirmed controls (disabled selects, readonly inputs) carry no
  // action but tell the model which fields are already settled.
  const locked=[];
  for (const e of document.querySelectorAll('input,textarea,select')) {
    if (!safe(e) || !visible(e)) continue;
    const isLocked=e.matches(':disabled') || e.readOnly ||
      e.getAttribute('aria-readonly')==='true' || e.closest('[aria-disabled="true"]');
    if (!isLocked) continue;
    const v=e.tagName==='SELECT'
      ? [...e.selectedOptions].map(o=>o.label).join(', ') : String(e.value||'');
    if (!v) continue;
    locked.push({label:(labelOf(e)||role(e)||'').slice(0,90),value:v.slice(0,60)});
    if (locked.length>=40) break;
  }
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,sy,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  // Per-field status table — the same view the rendered form gives a human:
  // one row per control with its label, required flag, current value, locked
  // and error state, plus the action id(s) that operate it. Connects "this
  // field is required and empty" to "this action fills it" in one hop.
  const byNode={};
  for (const a of actions) if (a.node!==undefined) (byNode[a.node] ||= []).push(a.id);
  const fields=[];
  const fieldEls=[];
  const pending=[];
  let nextMarked=false;
  for (const e of document.querySelectorAll('input,textarea,select')) {
    if (!safe(e) || !visible(e)) continue;
    const isLocked=e.matches(':disabled') || e.readOnly ||
      e.getAttribute('aria-readonly')==='true' || e.closest('[aria-disabled="true"]');
    const lab=(labelOf(e)||e.name||e.id||'?').slice(0,60);
    const req=!!(e.required || e.getAttribute('aria-required')==='true' || /必須/.test(lab));
    const f={label:lab};
    if (req) f.required=true;
    if (isLocked) f.locked=true;
    // "Unfinished" is per-kind: a radio/checkbox is empty when no member of
    // its group is checked (its own .value is the option, not the answer);
    // a select is empty when nothing with a value is selected; text is empty
    // when value is blank.
    let v, unfinished;
    if (e.type==='radio' || e.type==='checkbox') {
      const grp=e.name?[...document.getElementsByName(e.name)]:[e];
      unfinished=!grp.some(g=>g.checked);
      v=e.value||'on';
      if (e.checked) f.checked=true;
      if (!unfinished) f.value=v.slice(0,40);
      f.group=true;
    } else {
      v=e.tagName==='SELECT'
        ? [...e.selectedOptions].map(o=>o.label).join(', ') : String(e.value||'');
      if (e.tagName==='SELECT') {
        unfinished=!((e.selectedOptions[0]||{}).value||'').trim();
        f.options=e.options.length;
      } else {
        unfinished=!String(e.value||'').trim();
      }
      if (v) f.value=v.slice(0,40);
    }
    // A locked (disabled + valued) control cannot be re-edited, so error text
    // near it is residue from a previous submit, not a live failure — flagging
    // it would tell the model every confirmed field is broken. If the error is
    // real it resurfaces on the next submit attempt.
    if (!isLocked && errorOf(e,lab)) f.error=true;
    const aids=byNode[identity(e)];
    if (aids?.length) f.actions=aids;
    else if (req && unfinished && !isLocked) {
      // Defer: a radio/checkbox group needs no reveal when any sibling option
      // is already actionable on screen.
      pending.push({e,f});
    }
    // The first required-but-unfinished field in page order — the top-down
    // sweep pointer that keeps the fill sequence ordered and complete.
    if (req && unfinished && !isLocked && !nextMarked) { f.next=true; nextMarked=true; }
    fields.push(f);
    fieldEls.push(e);
    if (fields.length>=60) break;
  }
  // Resolve deferred reveals: skip when the field's own group already has an
  // actionable member on screen (clicking the visible option is enough).
  const actedGroups=new Set();
  fieldEls.forEach((e,i)=>{
    if ((e.type==='radio'||e.type==='checkbox') && e.name && fields[i].actions)
      actedGroups.add(e.name);
  });
  const revealed=new Set();
  for (const {e,f} of pending) {
    if (e.name && (actedGroups.has(e.name) || revealed.has(e.name))) continue;
    if (e.name) revealed.add(e.name);
    const rid='reveal'+(fields.indexOf(f)+1);
    actions.push({id:rid,kind:'reveal',node:identity(e),label:'必須欄を表示: '+f.label});
    f.actions=[rid];
  }
  // Stuck blocking overlay: a fixed full-viewport element that swallows every
  // hit test while no dialog is open (e.g. a loading mask left after an ajax
  // error). It makes every click stale, so expose it as an observed fact plus
  // one deterministic dismiss action.
  let overlay=null;
  const openDialog=[...document.querySelectorAll('.ui-dialog,[role="dialog"],.ui-modal')]
    .some(d=>{const r=d.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(d).visibility!=='hidden';});
  if (!openDialog) {
    const hits=new Map();
    for (const [px,py] of [[0.5,0.5],[0.25,0.25],[0.75,0.25],[0.25,0.75],[0.75,0.75]]) {
      const t=document.elementFromPoint(innerWidth*px,innerHeight*py);
      if (t) hits.set(t,(hits.get(t)||0)+1);
    }
    for (const [el,n] of hits) {
      if (n<4) continue;
      const r=el.getBoundingClientRect(), cs=getComputedStyle(el);
      if (cs.position!=='fixed'&&cs.position!=='absolute') continue;
      if (r.width>=innerWidth*0.8&&r.height>=innerHeight*0.8&&+cs.zIndex>=100) {
        overlay={label:(el.id||el.className||el.tagName).toString().slice(0,60),node:identity(el)};
        break;
      }
    }
  }
  if (overlay) {
    actions.push({id:'dismiss_overlay',kind:'dismiss',node:overlay.node,
      label:'閉じる: 画面を覆うオーバーレイ'});
  }
  if (sy+vh<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (sy>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  // Required-but-empty controls anywhere on the page, including off-screen —
  // the model cannot see them, so expose the count and their labels as a
  // derived fact that says "there is unfinished required work; scroll for it".
  const required_empty=[];
  const seenGroups=new Set();
  for (const e of document.querySelectorAll('input,textarea,select')) {
    if (!safe(e) || e.matches(':disabled') || e.readOnly ||
        e.closest('[aria-disabled="true"],[aria-hidden="true"]')) continue;
    const req = e.required || e.getAttribute('aria-required')==='true' ||
      /必須/.test(labelOf(e)||'');
    if (!req) continue;
    let empty;
    if (e.type==='radio' || e.type==='checkbox') {
      // A group's emptiness is shared — report it once under the group name.
      const key=e.name||e.id;
      if (seenGroups.has(key)) continue;
      seenGroups.add(key);
      const grp=e.name?[...document.getElementsByName(e.name)]:[e];
      empty=!grp.some(g=>g.checked);
    } else {
      empty = e.tagName==='SELECT'
        ? !((e.selectedOptions[0]||{}).value||'').trim() : !String(e.value||'').trim();
    }
    if (empty) required_empty.push((labelOf(e)||e.name||e.id||'?').slice(0,60));
    if (required_empty.length>=30) break;
  }
  // Errored controls anywhere in the DOM — off-screen errors still block
  // submit, so the model must know they exist even when it cannot see them.
  const errors=[...new Set(actions.filter(a=>a.error).map(a=>a.label))];
  for (const e of document.querySelectorAll('input,textarea,select')) {
    if (!safe(e)) continue;
    // Same residue rule as fields: a locked control's error text is stale.
    const lockedCtrl=e.matches(':disabled') || e.readOnly ||
      e.getAttribute('aria-readonly')==='true' || e.closest('[aria-disabled="true"]');
    if (!lockedCtrl && errorOf(e, labelOf(e)||''))
      errors.push((labelOf(e)||e.name||'?').slice(0,80));
    if (errors.length>=30) break;
  }
  const errorsDedup=[...new Set(errors)].slice(0,20);
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:sy,height},actions,locked,fields,errors:errorsDedup,required_empty,marker,page_key,guards,omitted_actions,
    ...(overlay?{overlay:overlay.label}:{})};
})()
