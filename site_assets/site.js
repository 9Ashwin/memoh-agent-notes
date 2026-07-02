const progress = document.querySelector('.reading-progress');
const menu = document.querySelector('.mobile-menu');

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
