(function() {
  var theme = localStorage.getItem('theme') || 'light';
  document.documentElement.setAttribute('data-theme', theme);
  function toggle() {
    var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    updateIcon(next);
  }
  function updateIcon(t) {
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = t === 'dark' ? '☀️' : '🌙';
  }
  document.addEventListener('DOMContentLoaded', function() {
    updateIcon(theme);
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', toggle);
  });
})();
