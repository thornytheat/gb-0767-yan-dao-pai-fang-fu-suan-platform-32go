const form = document.querySelector('#monitor-form');
const sample = document.querySelector('#sample');
const feedback = document.querySelector('#feedback');
const locationFor = (name, run) => {
  const url = new URL(location.href);
  url.searchParams.set('sample', name);
  if (run) url.searchParams.set('run', run);
  else url.searchParams.delete('run');
  return url;
};
sample.addEventListener('change', () => location.assign(locationFor(sample.value)));
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  feedback.hidden = false;
  feedback.dataset.failed = 'false';
  feedback.textContent = '正在复算与归档';
  try {
    const response = await fetch('/api/runs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sample: sample.value}),
    });
    const record = await response.json();
    if (!response.ok) throw new Error(record.detail);
    const target = locationFor(sample.value, record.run_id);
    if (target.href === location.href) feedback.textContent = '已复用原归档';
    else location.assign(target);
  } catch (error) {
    feedback.dataset.failed = 'true';
    feedback.textContent = error.message || '归档失败，请重试';
  } finally {
    button.disabled = false;
  }
});
const filter = document.querySelector('#state-filter');
filter?.addEventListener('change', () => {
  for (const row of document.querySelectorAll('#verdict-table tbody tr')) {
    row.hidden = filter.value !== 'all' && (filter.value === 'late' ? row.dataset.late !== 'True' : row.dataset.state !== filter.value);
  }
});

const spotPanel = document.querySelector('#spot-panel');
spotPanel?.addEventListener('click', async event => {
  const button = event.target.closest('.spot-action');
  if (!button) return;
  const feedback = spotPanel.querySelector('.spot-feedback');
  const tell = (text, failed = false) => {
    feedback.hidden = false;
    feedback.dataset.failed = String(failed);
    feedback.textContent = text;
  };
  const action = button.dataset.action;
  const routes = {
    open: {url: '/api/spotcheck/shifts', body: {run_id: button.dataset.run}},
    reconcile: {url: `/api/spotcheck/shifts/${button.dataset.shift}/reconcile`, body: {run_id: button.dataset.run}},
    claim: {url: `/api/spotcheck/shifts/${button.dataset.shift}/claim`, body: {hour: button.dataset.hour}},
    close: {url: `/api/spotcheck/shifts/${button.dataset.shift}/close`},
  };
  const route = routes[action];
  button.disabled = true;
  tell('正在处理抽查凭证');
  try {
    const response = await fetch(route.url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(route.body || {}),
    });
    const record = await response.json();
    if (!response.ok) throw new Error(record.detail);
    const target = new URL(location.href);
    target.searchParams.set('shift', record.shift.id);
    if (action === 'open' || action === 'reconcile') target.searchParams.set('run', record.shift.run_id);
    location.assign(target);
  } catch (error) {
    tell(error.message || '操作失败，请重试', true);
    button.disabled = false;
  }
});
