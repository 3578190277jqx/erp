(() => {
  try {
    const theme = localStorage.getItem('qimu-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.bsTheme = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.dataset.density = localStorage.getItem('qimu-density') === 'compact' ? 'compact' : 'standard';
    if (localStorage.getItem('qimu-collapsed') === 'true') document.documentElement.classList.add('nav-collapsed');
  } catch (_) { /* Preferences are optional when browser storage is disabled. */ }
})();
