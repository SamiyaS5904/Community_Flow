/* ===========================================================================
   topics.js — the Topic Pool screen
   ===========================================================================
   Loads on first open rather than with the page: the pool is a few hundred
   rows and most visits never look at it.
   =========================================================================== */

(() => {
  'use strict';

  const pane = document.getElementById('topics');
  if (!pane) return;

  const table   = document.getElementById('topic-table');
  const body    = table.querySelector('tbody');
  const empty   = document.getElementById('topic-empty');
  const filter  = document.getElementById('topic-filter');
  const status  = document.getElementById('topic-status');
  const url     = pane.dataset.cfLazy;

  const STATUS_LABEL = {
    candidate: 'available', scheduled: 'scheduled',
    used: 'published', retired: 'retired',
  };
  // The pool's own vocabulary mapped onto the post-state chips, so one colour
  // means the same thing everywhere in the dashboard.
  const STATUS_CHIP = {
    candidate: 'chip-needs_review', scheduled: 'chip-approved',
    used: 'chip-published', retired: 'chip-rejected',
  };
  const SOURCE_LABEL = { seed: 'Imported', discovery: 'Found on the web', manual: 'Added by hand' };

  let topics = [];
  let loaded = false;

  function when(iso) {
    if (!iso) return '—';
    const days = Math.floor((Date.now() - new Date(iso)) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 30) return `${days} days ago`;
    return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function row(t) {
    const tr = document.createElement('tr');
    tr.id = `topic-${t.id}`;

    const title = document.createElement('td');
    const strong = document.createElement('div');
    strong.className = 'cf-truncate';
    strong.style.color = 'var(--cf-ink)';
    strong.style.fontWeight = '550';
    strong.textContent = t.title;
    strong.title = t.title;
    title.appendChild(strong);

    // Two facts worth surfacing under the title: which category it will be
    // planned into, and — when discovery admitted it despite a near match —
    // what it was close to. That second one is the only place the "admitted
    // with a similarity flag" verdict is ever visible.
    const meta = [];
    if (t.category) meta.push(t.category);
    if (t.similar_to) meta.push(`close to: ${t.similar_to}`);
    if (meta.length) {
      const sub = document.createElement('div');
      sub.style.cssText = 'color: var(--cf-muted); font-size: var(--cf-text-xs); margin-top: 2px;';
      sub.textContent = meta.join(' · ');
      title.appendChild(sub);
    }

    const state = document.createElement('td');
    const chip = document.createElement('span');
    chip.className = `chip ${STATUS_CHIP[t.status] || ''}`;
    chip.textContent = STATUS_LABEL[t.status] || t.status;
    state.appendChild(chip);

    const source = document.createElement('td');
    source.style.color = 'var(--cf-muted)';
    if (t.source_url) {
      const link = document.createElement('a');
      link.href = t.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = SOURCE_LABEL[t.source] || t.source;
      link.title = t.source_url;
      source.appendChild(link);
    } else {
      source.textContent = SOURCE_LABEL[t.source] || t.source;
    }

    const added = document.createElement('td');
    added.style.color = 'var(--cf-muted)';
    added.textContent = when(t.created_at);

    const action = document.createElement('td');
    action.className = 'text-end pe-4';
    if (t.status !== 'retired') {
      const button = document.createElement('button');
      button.className = 'btn btn-ghost btn-sm';
      button.textContent = 'Retire';
      // Retired, not deleted: a used or scheduled topic is what future
      // candidates are checked against, so removing it would let the thing it
      // was blocking come straight back.
      button.title = 'Stop planning this topic. It still blocks duplicates.';
      button.addEventListener('click', () => retire(t, button));
      action.appendChild(button);
    }

    tr.append(title, state, source, added, action);
    return tr;
  }

  async function retire(topic, button) {
    CF.busy(button, true);
    try {
      const result = await CF.request(`/topics/${topic.id}/retire`, { method: 'POST' });
      topic.status = 'retired';
      CF.toast('ok', result.message, result.detail);
      render();
    } catch (error) {
      CF.toast('error', 'Could not retire that topic.', error.message);
    } finally {
      CF.busy(button, false);
    }
  }

  function render() {
    const needle = (filter.value || '').trim().toLowerCase();
    const wanted = status.value;
    const shown = topics.filter((t) =>
      (!wanted || t.status === wanted) &&
      (!needle || t.title.toLowerCase().includes(needle) ||
                  (t.category || '').toLowerCase().includes(needle)));

    body.replaceChildren(...shown.map(row));

    const nothing = shown.length === 0;
    table.hidden = nothing;
    empty.hidden = !nothing;
    if (nothing && topics.length) {
      // The pool is not empty, the filter is. Saying "no topics yet" here would
      // be wrong and would send someone off to run discovery for no reason.
      empty.querySelector('h6').textContent = 'Nothing matches that filter';
      empty.querySelector('p').textContent = 'Clear the search box or pick a different status.';
    }
  }

  function counts() {
    const tally = topics.reduce((acc, t) => (acc[t.status] = (acc[t.status] || 0) + 1, acc), {});
    document.querySelectorAll('#topic-counts [data-count]').forEach((el) => {
      el.textContent = tally[el.dataset.count] || 0;
    });
  }

  async function load() {
    try {
      const data = await CF.request(url);
      topics = data.topics || [];
      loaded = true;
      counts();
      render();
    } catch (error) {
      body.innerHTML = '';
      table.hidden = true;
      empty.hidden = false;
      empty.querySelector('h6').textContent = 'The pool could not be read';
      empty.querySelector('p').textContent = error.message;
    }
  }

  // Bootstrap fires this the first time the tab is shown.
  document.querySelectorAll('[data-bs-target="#topics"]').forEach((trigger) => {
    trigger.addEventListener('shown.bs.tab', () => { if (!loaded) load(); });
  });
  // The sidebar drives tabs through the Tab API rather than by delegation, so
  // shown.bs.tab does not always reach the button here.
  document.querySelectorAll('.sidebar-link[data-bs-target="#topics-tab"]').forEach((link) => {
    link.addEventListener('click', () => { if (!loaded) load(); });
  });

  filter.addEventListener('input', render);
  status.addEventListener('change', render);

  // Adding a topic and finishing a discovery run both change the pool.
  document.addEventListener('cf:action', (e) => {
    if (e.detail.form.dataset.cfOnSuccess === 'refresh-topics') load();
  });
  document.addEventListener('cf:job-done', () => { if (loaded) load(); });
})();
