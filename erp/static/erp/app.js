(() => {
  const root = document.documentElement;
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch (_) {} };
  const navToggle = document.querySelector('#nav-toggle');
  const setNav = () => {
    if (matchMedia('(max-width: 760px)').matches) root.classList.toggle('nav-open');
    else { root.classList.toggle('nav-collapsed'); save('qimu-collapsed', root.classList.contains('nav-collapsed')); }
    navToggle?.setAttribute('aria-expanded', String(matchMedia('(max-width: 760px)').matches ? root.classList.contains('nav-open') : !root.classList.contains('nav-collapsed')));
  };
  navToggle?.addEventListener('click', setNav);
  document.querySelectorAll('[data-open-nav]').forEach(el => el.addEventListener('click', () => { root.classList.add('nav-open'); navToggle?.setAttribute('aria-expanded', 'true'); }));
  document.querySelectorAll('[data-close-nav]').forEach(el => el.addEventListener('click', () => { root.classList.remove('nav-open'); navToggle?.setAttribute('aria-expanded', 'false'); }));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') root.classList.remove('nav-open'); });
  document.querySelector('#theme-toggle')?.addEventListener('click', () => { root.dataset.bsTheme = root.dataset.bsTheme === 'dark' ? 'light' : 'dark'; save('qimu-theme', root.dataset.bsTheme); });
  document.querySelector('#density-toggle')?.addEventListener('click', () => { root.dataset.density = root.dataset.density === 'compact' ? 'standard' : 'compact'; save('qimu-density', root.dataset.density); });
  addEventListener('scroll', () => root.classList.toggle('has-scrolled', scrollY > 20), { passive: true });
  document.querySelectorAll('.dismiss').forEach(el => el.addEventListener('click', () => el.closest('.notice').remove()));
  document.querySelectorAll('[data-close-dialog]').forEach(el => el.addEventListener('click', () => el.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', e => { if (e.target === dialog) { const box = dialog.getBoundingClientRect(); if (e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom) dialog.close(); } }));
  const workflow = document.querySelector('#workflow-dialog');
  const openWorkflow = data => {
    document.querySelector('#workflow-form').action = data.url;
    document.querySelector('#workflow-action').value = data.action;
    document.querySelector('#workflow-version').value = data.version;
    document.querySelector('#workflow-parent-version').value = data.parentVersion || '';
    document.querySelector('#workflow-title').textContent = `确认${data.label}？`;
    document.querySelector('#workflow-copy').textContent = data.confirmCopy || '状态变更将同步到关联单据，并留下操作记录。';
    const required = ['fail', 'reject', 'cancel', 'close', 'resolve', 'complete', 'withdraw', 'stop_share'].includes(data.action);
    document.querySelector('#workflow-note').required = required;
    document.querySelector('#workflow-note').value = '';
    document.querySelector('#workflow-required').textContent = required ? ' *' : '（选填）';
    workflow.showModal();
  };
  document.querySelectorAll('[data-workflow]').forEach(el => el.addEventListener('click', () => openWorkflow(el.dataset)));
  let dragging = null;
  document.querySelectorAll('.kanban-card[draggable="true"]').forEach(card => {
    card.addEventListener('dragstart', e => { dragging = card; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', card.href); card.classList.add('is-dragging'); });
    card.addEventListener('dragend', () => { dragging = null; card.classList.remove('is-dragging'); document.querySelectorAll('.drop-ready').forEach(el => el.classList.remove('drop-ready')); });
  });
  document.querySelectorAll('[data-drop-status]').forEach(column => {
    column.addEventListener('dragover', e => { if (dragging && JSON.parse(dragging.dataset.transitions)[column.dataset.dropStatus]) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; column.classList.add('drop-ready'); } });
    column.addEventListener('dragleave', e => { if (!column.contains(e.relatedTarget)) column.classList.remove('drop-ready'); });
    column.addEventListener('drop', e => {
      e.preventDefault(); column.classList.remove('drop-ready'); if (!dragging) return;
      const next = JSON.parse(dragging.dataset.transitions)[column.dataset.dropStatus];
      if (next) openWorkflow({ ...next, url: dragging.dataset.url, version: dragging.dataset.version });
    });
  });
  const confirm = document.querySelector('#confirm-dialog');
  let pendingForm = null;
  document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', e => {
    if (form.dataset.confirmed === 'true') return;
    e.preventDefault();
    if (form.matches('[data-batch]') && !form.querySelector('input[name="ids"]:checked')) { document.querySelector('#selection-count').textContent = '请先选择要处理的记录'; return; }
    pendingForm = form;
    document.querySelector('#confirm-copy').textContent = form.dataset.confirm;
    confirm.showModal();
  }));
  document.querySelector('#confirm-submit')?.addEventListener('click', () => {
    if (!pendingForm) return;
    confirm.close(); pendingForm.dataset.confirmed = 'true'; pendingForm.requestSubmit(); pendingForm = null;
  });
  const updateSelection = () => {
    const batch = document.querySelector('[data-batch]');
    if (!batch) return;
    batch.querySelectorAll('[data-selected-version]').forEach(el => el.remove());
    const selected = batch.querySelectorAll('input[name="ids"]:checked');
    selected.forEach(el => { const input = document.createElement('input'); input.type = 'hidden'; input.name = 'versions'; input.value = el.dataset.version; input.dataset.selectedVersion = 'true'; batch.append(input); });
    const label = document.querySelector('#selection-count'); if (label) label.textContent = `已选 ${selected.length} 条`;
  };
  document.querySelector('#select-all')?.addEventListener('change', e => { document.querySelectorAll('input[name="ids"]').forEach(el => { el.checked = e.target.checked; }); updateSelection(); });
  document.querySelectorAll('input[name="ids"]').forEach(el => el.addEventListener('change', updateSelection));
  document.querySelectorAll('form').forEach(form => form.addEventListener('submit', () => {
    if (form.dataset.confirm && form.dataset.confirmed !== 'true') return;
    form.querySelectorAll('button[type="submit"]').forEach(button => { button.disabled = true; button.setAttribute('aria-busy', 'true'); button.classList.add('is-loading'); });
  }));
  document.querySelector('[data-org-select]')?.addEventListener('change', e => {
    const selected = e.target.value;
    if (selected && !location.pathname.includes('/edit/')) {
      // Switching business scope reloads FK choices. Only the organization selection is retained.
      const url = new URL(location.href); url.searchParams.set('organization', selected); location.assign(url);
    }
  });
})();

// Source product snapshots prefill the change form; the server independently
// checks the target, dimensions, Decimal totals and all authorization.
(()=>{const form=document.querySelector('[data-change-line]'),source=document.getElementById('change-source-lines');if(!form||!source)return;
const lines=JSON.parse(source.textContent),operation=form.elements.operation,target=form.elements.order_line;
const update=()=>{const remove=operation.value==='remove';for(const field of form.querySelectorAll('.form-field')){const input=field.querySelector('input,select,textarea');if(!input)continue;const pricing=!['operation','order_line'].includes(input.name);field.hidden=remove&&pricing;input.disabled=remove&&pricing;}target.required=operation.value!=='add';};
const prefill=()=>{if(operation.value!=='update')return;const values=lines[target.value];if(!values)return;for(const [name,value] of Object.entries(values)){const input=form.elements[name];if(input)input.value=value;}};
operation.addEventListener('change',()=>{prefill();update();});target.addEventListener('change',prefill);update();})();

(()=>{const order=document.querySelector('[data-plan-url]'),plan=document.getElementById('id_plan'),direction=document.getElementById('id_direction');if(!order||!plan)return;let controller,requestNumber=0;
const load=async()=>{const current=++requestNumber;controller?.abort();plan.replaceChildren(new Option('不指定：按顺序分摊',''));plan.disabled=direction?.value!=='in';if(plan.disabled||!order.value)return;controller=new AbortController();try{const response=await fetch(`${order.dataset.planUrl}?order=${encodeURIComponent(order.value)}`,{signal:controller.signal,headers:{'Accept':'application/json'}});if(!response.ok)throw new Error('load');const data=await response.json();if(current!==requestNumber)return;for(const row of data.plans)plan.add(new Option(row.label,row.id));}catch(error){if(error.name!=='AbortError'&&current===requestNumber)plan.options[0].textContent='期次加载失败，可刷新重试或选择顺序分摊';}};
order.addEventListener('change',load);direction?.addEventListener('change',load);})();
