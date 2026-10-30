const panels = [...document.querySelectorAll('.workspace > section[id]')];
const links = [...document.querySelectorAll('.sections a')];
function showPanel() {
  const requested = location.hash.slice(1);
  const fallback = location.search.includes('q=') || location.search.includes('page_no=') ? 'members' : 'settings';
  const preset = document.body.dataset.panel;
  const active = panels.some(panel => panel.id === requested) ? requested : (panels.some(panel => panel.id === preset) ? preset : fallback);
  panels.forEach(panel => { panel.hidden = panel.id !== active; });
  links.forEach(link => {
    const selected = link.hash === '#' + active;
    link.classList.toggle('active', selected);
    if (selected) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}
window.addEventListener('hashchange', showPanel);
showPanel();

document.querySelectorAll('form[method="post"]').forEach(form => {
  form.addEventListener('submit', event => {
    let warning = '';
    const action = form.querySelector('input[name="action"]')?.value;
    if ((form.getAttribute('action') || '').endsWith('/code') || action === 'rotate') warning = '更换加入码后，旧码立即失效；现有成员保留。确认更换？';
    if (action === 'web_access') warning = '生成或重置网页账号后，旧网页密码立即失效。确认继续？';
    if (form.querySelector('[name="status"]')?.value === 'disabled') warning = '停用网络会终止未完成的联系申请。确认停用？';
    if (form.querySelector('[name="status"]')?.value === 'removed') warning = '移除成员会终止该成员未完成的联系申请。确认移除？';
    if (warning && !window.confirm(warning)) { event.preventDefault(); return; }
    const button = form.querySelector('button');
    if (button) { button.disabled = true; button.textContent = '正在保存…'; }
  });
});
