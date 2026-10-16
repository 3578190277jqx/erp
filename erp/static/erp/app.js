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
  document.querySelectorAll('[data-workflow]').forEach(el => el.addEventListener('click', () => {
    document.querySelector('#workflow-form').action = el.dataset.url;
    document.querySelector('#workflow-action').value = el.dataset.action;
    document.querySelector('#workflow-version').value = el.dataset.version;
    document.querySelector('#workflow-title').textContent = `确认${el.dataset.label}？`;
    const required = ['fail', 'reject', 'cancel', 'close', 'resolve', 'complete'].includes(el.dataset.action);
    document.querySelector('#workflow-note').required = required;
    document.querySelector('#workflow-note').value = '';
    document.querySelector('#workflow-required').textContent = required ? ' *' : '（选填）';
    workflow.showModal();
  }));
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
