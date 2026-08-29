/* ===========================================================================
   cycle.js — the Cycle Plan screen
   ===========================================================================
   Renders the plan as a calendar of days rather than a list of slots. The two
   questions an operator has about a cycle are "which days are still empty" and
   "what got assigned where", and both are shape questions — a table of 77 rows
   answers neither at a glance.
   =========================================================================== */

(() => {
  'use strict';

  const pane = document.getElementById('cycle');
  if (!pane) return;

  const body = document.getElementById('cycle-body');
  const url = pane.dataset.cfLazy;
  let loaded = false;

  const SOURCE_LABEL = { seed: 'imported', discovery: 'from the web', manual: 'added by hand' };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function empty(icon, title, detail) {
    const box = el('div', 'cf-empty');
    box.append(el('i', `bi ${icon}`), el('h6', null, title), el('p', null, detail));
    return box;
  }

  function summary(data) {
    const p = data.position;
    const plan = data.plan;
    const slots = (plan && plan.slots) || [];
    const planned = slots.filter((s) => s.topic).length;

    const row = el('div', 'row g-3 mb-4');
    const stats = [
      ['Cycle', `#${p.cycle_number}`, `Day ${p.day_in_cycle} of ${p.cycle_length}`],
      ['Runs', p.starts_on.slice(5), `through ${p.ends_on.slice(5)}`],
      ['Slots planned', plan ? `${planned}/${slots.length}` : '—',
        // An unplanned slot is not broken — it invents its own topic. But it is
        // the number worth watching, because it is what discovery fixes.
        plan ? (planned === slots.length ? 'Every slot has a topic'
                                         : 'The rest will invent their own')
             : 'Not planned yet'],
      ['Pool available', String(data.pool_available),
        data.pool_available ? 'Topics the next cycle can draw from'
                            : 'Run discovery — nothing left to plan from'],
    ];
    stats.forEach(([label, value, note]) => {
      const col = el('div', 'col-6 col-lg-3');
      const card = el('div', 'card h-100');
      const stat = el('div', 'cf-stat');
      stat.append(el('div', 'cf-stat-label', label),
                  el('div', 'cf-stat-value', value),
                  el('div', 'cf-stat-note', note));
      card.appendChild(stat);
      col.appendChild(card);
      row.appendChild(col);
    });
    return row;
  }

  function slotRow(slot, isToday) {
    const item = el('div', 'd-flex gap-3 align-items-start py-2');
    item.style.borderTop = '1px solid var(--cf-line-soft)';

    const time = el('div', 'font-mono', slot.time || '--:--');
    time.style.cssText = 'color: var(--cf-muted); font-size: var(--cf-text-xs); min-width: 3.2rem; padding-top: 2px;';

    const main = el('div');
    main.style.flex = '1';
    if (slot.topic) {
      const title = el('div', null, slot.topic);
      title.style.cssText = 'color: var(--cf-ink); font-size: var(--cf-text-sm); line-height: 1.45;';
      main.appendChild(title);

      const meta = [slot.category, SOURCE_LABEL[slot.source] || slot.source].filter(Boolean);
      if (meta.length) {
        const sub = el('div', null, meta.join(' · '));
        sub.style.cssText = 'color: var(--cf-muted); font-size: var(--cf-text-xs); margin-top: 2px;';
        main.appendChild(sub);
      }
    } else {
      // Deliberately not styled as an error. A slot with no pool topic still
      // produces a post; it just invents its own subject, which is exactly the
      // behaviour the pool exists to replace.
      const none = el('div', null, 'No topic assigned — this slot will invent one');
      none.style.cssText = 'color: var(--cf-faint); font-size: var(--cf-text-sm); font-style: italic;';
      main.appendChild(none);
    }

    const type = el('span', 'chip');
    type.textContent = slot.content_type || 'message';
    item.append(time, main, type);
    return item;
  }

  function calendar(data) {
    const plan = data.plan;
    if (!plan) {
      return empty('bi-calendar-x', 'This cycle has not been planned yet',
        'Plan it to assign pool topics to the rhythm the strategy declares. '
        + 'Until then each slot invents its own topic when it generates.');
    }

    const byDate = new Map();
    (plan.slots || []).forEach((slot) => {
      const key = slot.date || 'unscheduled';
      if (!byDate.has(key)) byDate.set(key, []);
      byDate.get(key).push(slot);
    });

    const wrapper = el('div', 'row g-3');
    [...byDate.entries()].forEach(([day, slots]) => {
      const isToday = day === data.today;
      const col = el('div', 'col-12 col-xl-6');
      const card = el('div', 'card h-100');
      if (isToday) card.style.borderColor = 'var(--cf-brand-border)';

      const header = el('div', 'card-header justify-content-between');
      const left = el('span');
      const when = day === 'unscheduled' ? 'Unscheduled'
        : new Date(`${day}T00:00:00`).toLocaleDateString([], {
            weekday: 'short', month: 'short', day: 'numeric' });
      left.textContent = when;
      header.appendChild(left);
      if (isToday) {
        const chip = el('span', 'chip chip-approved', 'today');
        header.appendChild(chip);
      } else {
        const count = slots.filter((s) => s.topic).length;
        header.appendChild(el('span', 'chip', `${count}/${slots.length}`));
      }

      const list = el('div', 'px-4 pb-3');
      slots.forEach((slot) => list.appendChild(slotRow(slot, isToday)));

      card.append(header, list);
      col.appendChild(card);
      wrapper.appendChild(col);
    });
    return wrapper;
  }

  async function load() {
    try {
      const data = await CF.request(url);
      body.replaceChildren();

      if (!data.has_strategy) {
        body.appendChild(empty('bi-file-earmark-text', 'No cycle plan for this community',
          data.message));
        loaded = true;
        return;
      }
      body.append(summary(data), calendar(data));
      loaded = true;
    } catch (error) {
      body.replaceChildren(empty('bi-exclamation-triangle',
        'The plan could not be read', error.message));
    }
  }

  document.querySelectorAll('.sidebar-link[data-bs-target="#cycle-tab"], [data-bs-target="#cycle"]')
    .forEach((trigger) => trigger.addEventListener('click', () => { if (!loaded) load(); }));

  // Planning finishes in a worker; reload so the calendar shows what it wrote.
  document.addEventListener('cf:job-done', () => { if (loaded) load(); });
})();
