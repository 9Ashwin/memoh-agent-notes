const progress = document.querySelector('.reading-progress');
const menu = document.querySelector('.mobile-menu');
const themeButton = document.querySelector('.theme-button');
const searchButton = document.querySelector('.search-button');
const searchDialog = document.querySelector('.search-dialog');
const searchInput = searchDialog?.querySelector('input');
const searchResults = searchDialog?.querySelector('.search-results');
const searchStatus = searchDialog?.querySelector('.search-status');
let searchIndex = null;
let activeResult = -1;

function updateProgress() {
  if (!progress) return;
  const max = document.documentElement.scrollHeight - window.innerHeight;
  progress.style.width = `${max > 0 ? (window.scrollY / max) * 100 : 0}%`;
}

if (menu) {
  menu.addEventListener('click', () => {
    const open = document.body.classList.toggle('menu-open');
    menu.setAttribute('aria-expanded', String(open));
  });
  document.querySelectorAll('.chapter-sidebar a').forEach((link) => {
    link.addEventListener('click', () => document.body.classList.remove('menu-open'));
  });
}

const tocLinks = [...document.querySelectorAll('.toc-sidebar nav a')];
const sections = tocLinks
  .map((link) => document.getElementById(decodeURIComponent(link.hash.slice(1))))
  .filter(Boolean);
const observer = sections.length ? new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    tocLinks.forEach((link) => link.classList.toggle('is-active', link.hash === `#${entry.target.id}`));
  });
}, { rootMargin: '-15% 0px -75%' }) : null;

sections.forEach((section) => observer?.observe(section));
window.addEventListener('scroll', updateProgress, { passive: true });
updateProgress();

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('memoh-theme', theme);
  themeButton?.setAttribute('aria-label', theme === 'dark' ? '切换浅色模式' : '切换深色模式');
}

themeButton?.addEventListener('click', () => {
  setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});
setTheme(document.documentElement.dataset.theme || 'light');

function escapeHtml(value) {
  const node = document.createElement('span');
  node.textContent = value;
  return node.innerHTML;
}

function resultSnippet(content, query) {
  const lower = content.toLowerCase();
  const position = lower.indexOf(query.toLowerCase());
  const start = Math.max(0, position - 45);
  const end = Math.min(content.length, position + query.length + 85);
  return `${start ? '…' : ''}${content.slice(start, end)}${end < content.length ? '…' : ''}`;
}

function selectResult(index) {
  const results = [...searchResults.querySelectorAll('a')];
  if (!results.length) return;
  activeResult = Math.max(0, Math.min(index, results.length - 1));
  results.forEach((result, itemIndex) => result.classList.toggle('is-active', itemIndex === activeResult));
  results[activeResult].scrollIntoView({ block: 'nearest' });
}

function renderSearch(query) {
  if (!searchIndex || !searchResults || !searchStatus) return;
  const normalized = query.trim().toLowerCase();
  activeResult = -1;
  if (!normalized) {
    searchResults.innerHTML = '';
    searchStatus.textContent = '输入关键词，搜索全部 15 篇笔记';
    return;
  }
  const matches = searchIndex
    .map((item) => {
      const titleHit = item.title.toLowerCase().includes(normalized) ? 12 : 0;
      const descriptionHit = item.description.toLowerCase().includes(normalized) ? 6 : 0;
      const contentHit = item.content.toLowerCase().includes(normalized) ? 1 : 0;
      return { ...item, score: titleHit + descriptionHit + contentHit };
    })
    .filter((item) => item.score > 0)
    .sort((left, right) => right.score - left.score)
    .slice(0, 10);
  searchStatus.textContent = matches.length ? `找到 ${matches.length} 篇相关笔记` : '没有找到相关内容';
  searchResults.innerHTML = matches.map((item) => `
    <a href="${item.url}">
      <span class="result-group">${escapeHtml(item.group)}</span>
      <strong>${escapeHtml(item.title)}</strong>
      <small>${escapeHtml(resultSnippet(item.content, normalized))}</small>
      <i aria-hidden="true">→</i>
    </a>`).join('');
  if (matches.length) selectResult(0);
}

async function openSearch() {
  if (!searchDialog) return;
  searchDialog.showModal();
  searchInput?.focus();
  if (!searchIndex) {
    const response = await fetch('assets/search.json');
    searchIndex = await response.json();
  }
  renderSearch(searchInput?.value || '');
}

searchButton?.addEventListener('click', openSearch);
searchInput?.addEventListener('input', (event) => renderSearch(event.target.value));
searchInput?.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowDown') { event.preventDefault(); selectResult(activeResult + 1); }
  if (event.key === 'ArrowUp') { event.preventDefault(); selectResult(activeResult - 1); }
  if (event.key === 'Enter' && activeResult >= 0) {
    event.preventDefault();
    searchResults.querySelectorAll('a')[activeResult]?.click();
  }
});
document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    openSearch();
  }
});
searchDialog?.addEventListener('click', (event) => {
  if (event.target === searchDialog) searchDialog.close();
});
