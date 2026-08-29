/* ===========================================================================
   studio.js — the Design Studio
   ===========================================================================
   The points on a list graphic are the substance of it, and they used to be
   exposed as one text input holding a raw JSON array: editing the third
   point's example meant hand-writing JSON, and one stray comma silently
   emptied the graphic. Each point is a row here, with its own fields.

   The style controls write design tokens (--accent, --text-display, and so on)
   through the same map the static renderer uses, so the preview and the export
   cannot disagree. They previously wrote CSS aimed at .motivation-text and
   .editorial-title — classes belonging to templates deleted two commits before
   — so none of them had any effect at all.
   =========================================================================== */

(() => {
  'use strict';

  const SCALE_TO = 1080;

  class Studio {
    constructor(root) {
      this.root    = root;
      this.postId  = root.dataset.postId;
      this.frame   = root.querySelector('[data-preview]');
      this.itemsEl = root.querySelector('[data-items]');
      this.fit     = root.querySelector('[data-fit]');
      this.items   = [];
      this.dirty   = false;

      // A ResizeObserver rather than a window resize listener: the studio is
      // built while its tab is still hidden, so the container measures 0 wide
      // and a scale computed then is 0 — an invisible canvas. This fires again
      // the moment the tab is shown and the box has a real width.
      this.observer = new ResizeObserver(() => this.scale());
      this.observer.observe(this.frame.parentElement);
      this.scale();

      this.bindFields();
      this.bindStyles();
      this.bindButtons();
      this.loadItems();

      window.addEventListener('message', (e) => {
        if (e.data && e.data.type === 'MEASURED') this.showFit(e.data);
      });
      this.frame.addEventListener('load', () => {
        this.pushStyles();
        this.measure();
      });
    }

    /* The iframe renders at the true canvas width and is scaled to whatever
       space the column has, so the preview is the real layout rather than a
       reflowed approximation of it. */
    scale() {
      const width = this.frame.parentElement.clientWidth;
      if (!width) return;                 // still hidden; the observer will call back
      this.frame.style.transform = `scale(${width / SCALE_TO})`;
    }

    send(message) {
      if (this.frame.contentWindow) this.frame.contentWindow.postMessage(message, '*');
    }

    measure() { this.send({ type: 'MEASURE' }); }

    showFit(result) {
      this.fit.hidden = false;
      if (result.overflow) {
        this.fit.className = 'chip chip-asset_failed';
        this.fit.textContent = `overflowing by ${result.by}px`;
        this.fit.title = 'The renderer will shrink the text to fit. Cut a point or shorten one.';
      } else {
        this.fit.className = 'chip chip-published';
        this.fit.textContent = 'fits';
        this.fit.title = 'Everything is inside the canvas at the current sizes.';
      }
    }

    /* ── plain fields ─────────────────────────────────────────────────── */

    bindFields() {
      this.root.querySelectorAll('[data-field]').forEach((input) => {
        input.addEventListener('input', () => {
          // TIP is rendered inside ITEMS_HTML rather than on its own, so it
          // has to go back through the item builder.
          if (input.dataset.field === 'TIP') this.pushItems();
          else this.send({ type: 'UPDATE_FIELD', key: input.dataset.field, value: input.value });
          this.touch();
        });
      });
    }

    /* ── the points ───────────────────────────────────────────────────── */

    loadItems() {
      const raw = this.root.dataset.itemJson;
      try {
        this.items = raw ? JSON.parse(raw) : [];
      } catch (error) {
        // A malformed array is worth saying out loud: silently starting from
        // an empty list would discard the model's work without telling anyone.
        CF.toast('error', 'The points could not be read.', 'Starting from an empty list.');
        this.items = [];
      }
      this.renderItems();
    }

    renderItems() {
      this.itemsEl.replaceChildren(...this.items.map((item, i) => this.itemRow(item, i)));
      this.pushItems();
    }

    itemRow(item, index) {
      const row = document.createElement('div');
      row.className = 'card mb-2';
      row.style.background = 'var(--cf-surface-2)';

      const head = document.createElement('div');
      head.className = 'd-flex align-items-center gap-2 px-3 pt-3';
      const badge = document.createElement('span');
      badge.className = 'chip';
      badge.textContent = item.number || String(index + 1).padStart(2, '0');
      head.appendChild(badge);
      head.appendChild(Object.assign(document.createElement('span'), {
        style: 'flex:1', textContent: '',
      }));

      const move = (delta, label, icon, disabled) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-ghost btn-sm';
        b.innerHTML = `<i class="bi ${icon}"></i>`;
        b.title = label;
        b.setAttribute('aria-label', label);
        b.disabled = disabled;
        b.addEventListener('click', () => this.move(index, delta));
        return b;
      };
      head.appendChild(move(-1, 'Move up', 'bi-arrow-up', index === 0));
      head.appendChild(move(1, 'Move down', 'bi-arrow-down', index === this.items.length - 1));

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn btn-ghost btn-sm';
      remove.innerHTML = '<i class="bi bi-trash"></i>';
      remove.title = 'Remove this point';
      remove.setAttribute('aria-label', 'Remove this point');
      remove.addEventListener('click', () => this.remove(index));
      head.appendChild(remove);

      const body = document.createElement('div');
      body.className = 'p-3 pt-2 d-flex flex-column gap-2';

      const field = (key, placeholder, rows) => {
        const el = rows ? document.createElement('textarea') : document.createElement('input');
        el.className = 'form-control';
        if (rows) el.rows = rows;
        el.placeholder = placeholder;
        el.value = item[key] || '';
        el.setAttribute('aria-label', placeholder);
        el.addEventListener('input', () => {
          this.items[index][key] = el.value;
          this.pushItems();
          this.touch();
        });
        return el;
      };

      body.append(
        field('title', 'The point, in a few words'),
        field('description', 'What it means, in one or two sentences', 2),
        // The example is what makes a card land, and it is the first thing the
        // model drops when it is running out of room.
        field('example', 'A situation the reader would recognise', 2),
      );

      row.append(head, body);
      return row;
    }

    move(index, delta) {
      const target = index + delta;
      if (target < 0 || target >= this.items.length) return;
      [this.items[index], this.items[target]] = [this.items[target], this.items[index]];
      this.renumber();
    }

    remove(index) {
      this.items.splice(index, 1);
      this.renumber();
    }

    add() {
      if (this.items.length >= 7) {
        CF.toast('info', 'That is as many as will fit.', 'Seven is the ceiling; four reads best.');
        return;
      }
      this.items.push({ title: '', description: '', example: '' });
      this.renumber();
    }

    renumber() {
      // Numbers are positional, so reordering or removing has to rewrite them
      // — otherwise the graphic shows 01, 03, 04.
      this.items.forEach((item, i) => { item.number = String(i + 1).padStart(2, '0'); });
      this.renderItems();
      this.touch();
    }

    /* Items are markup by the time they reach the canvas, so the preview is
       given the built HTML rather than the array. */
    pushItems() {
      // This has to match _generate_items_html in services/render_service.py
      // exactly. Any difference between them shows up as a preview that does
      // not look like the exported file, which is the one thing this panel
      // exists to prevent.
      const cards = this.items.map((item, i) => {
        const number = esc(item.number || String(i + 1).padStart(2, '0'));
        const title = esc(item.title || '');
        const description = esc(item.description || '');
        const body = (title && description) ? `<strong>${title}</strong> — ${description}`
                                            : (title || description);
        const example = item.example
          ? `<p class="card-example"><span class="example-label">In practice</span>${esc(item.example)}</p>`
          : '';
        return `<div class="card point-card">`
             + `<span class="point-number">${number}</span>`
             + `<div class="card-body"><p class="card-text">${body}</p>${example}</div>`
             + `</div>`;
      });

      // The tip is part of ITEMS_HTML on the render side, not a placeholder of
      // its own — so rebuilding the items without it made the Pro Tip card
      // vanish from the preview while the export still had it.
      const tipField = this.root.querySelector('[data-field="TIP"]');
      const tip = tipField && tipField.value.trim();
      if (tip) {
        cards.push(`<div class="card tip-card card-row">`
          + `<span class="badge-icon">★</span>`
          + `<div class="card-body"><span class="card-label">Pro Tip</span>`
          + `<p class="card-text">${esc(tip)}</p></div></div>`);
      }

      this.send({ type: 'UPDATE_FIELD', key: 'ITEMS_HTML', value: cards.join('') });
      clearTimeout(this._measureTimer);
      this._measureTimer = setTimeout(() => this.measure(), 350);
    }

    /* ── style ────────────────────────────────────────────────────────── */

    bindStyles() {
      this.root.querySelectorAll('[data-style]').forEach((control) => {
        control.addEventListener('input', () => {
          const readout = this.root.querySelector(`[data-readout="${control.dataset.style}"]`);
          if (readout) readout.textContent = `${control.value}px`;
          this.pushStyles();
          this.touch();
        });
        control.addEventListener('change', () => this.pushStyles());
      });

      this.root.querySelectorAll('[data-swatch]').forEach((button) => {
        button.addEventListener('click', () => {
          const picker = this.root.querySelector('[data-style="accentColor"]');
          picker.value = button.dataset.swatch;
          this.pushStyles();
          this.touch();
        });
      });
    }

    styles() {
      const out = {};
      this.root.querySelectorAll('[data-style]').forEach((control) => {
        if (control.value !== '') out[control.dataset.style] = control.value;
      });
      return out;
    }

    pushStyles() {
      this.send({ type: 'UPDATE_STYLE', ...this.styles() });
      clearTimeout(this._measureTimer);
      this._measureTimer = setTimeout(() => this.measure(), 350);
    }

    /* ── saving and rendering ─────────────────────────────────────────── */

    touch() {
      this.dirty = true;
      this.root.querySelector('[data-action="save"]').classList.add('btn-primary');
    }

    payload() {
      const placeholders = { items: this.items };
      this.root.querySelectorAll('[data-field]').forEach((input) => {
        placeholders[input.dataset.field] = input.value;
      });
      return {
        placeholders,
        visual_overrides: this.styles(),
        template_used: this.root.querySelector('[data-template]')?.value,
        asset_type: this.root.querySelector('[data-format]')?.value,
      };
    }

    async save(button) {
      CF.busy(button, true);
      try {
        await CF.request(`/api/save_asset_state/${this.postId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.payload()),
        });
        this.dirty = false;
        CF.toast('ok', 'Design saved.', 'Render to produce the file.');
      } catch (error) {
        CF.toast('error', 'The design could not be saved.', error.message);
      } finally {
        CF.busy(button, false);
      }
    }

    async render(button) {
      CF.busy(button, true);
      try {
        // Always save first. Rendering the last-saved state while the screen
        // showed unsaved edits is the single most confusing thing this panel
        // could do.
        await CF.request(`/api/save_asset_state/${this.postId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.payload()),
        });
        const started = await CF.request(`/api/render_asset/${this.postId}`, { method: 'POST' });
        this.dirty = false;
        CF.toast('info', 'Rendering.', 'This usually takes a second or two.');
        if (started.job_id) CF.track(started.job_id, '/api/render_asset_status');
      } catch (error) {
        CF.toast('error', 'That did not render.', error.message);
      } finally {
        CF.busy(button, false);
      }
    }

    bindButtons() {
      const on = (action, fn) => {
        const button = this.root.querySelector(`[data-action="${action}"]`);
        if (button) button.addEventListener('click', () => fn(button));
      };
      on('save',   (b) => this.save(b));
      on('render', (b) => this.render(b));
      on('add-item', () => this.add());
      on('reset-style', () => {
        this.root.querySelectorAll('[data-style]').forEach((control) => {
          control.value = control.defaultValue;
          const readout = this.root.querySelector(`[data-readout="${control.dataset.style}"]`);
          if (readout) readout.textContent = `${control.value}px`;
        });
        // An empty override set is what tells the renderer to fall back to the
        // group's own tokens, so reload rather than pushing the defaults back.
        this.frame.contentWindow.location.reload();
        CF.toast('info', 'Back to the community’s brand.');
      });

      // A template change re-renders from scratch: a different archetype has a
      // different contract, so the preview cannot be patched in place.
      const template = this.root.querySelector('[data-template]');
      if (template) {
        template.addEventListener('change', async () => {
          await this.save(this.root.querySelector('[data-action="save"]'));
          this.frame.src = `/render/preview/${this.postId}?t=${Date.now()}`;
        });
      }
    }
  }

  function esc(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
  }

  // Leaving with unsaved edits loses them; the browser's own prompt is the
  // only thing that can interrupt a navigation.
  const studios = [];
  document.querySelectorAll('[data-studio]').forEach((el) => studios.push(new Studio(el)));
  window.addEventListener('beforeunload', (e) => {
    if (studios.some((s) => s.dirty)) { e.preventDefault(); e.returnValue = ''; }
  });
})();
