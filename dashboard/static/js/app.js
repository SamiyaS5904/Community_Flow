/* ===========================================================================
   app.js — the dashboard's interaction layer
   ===========================================================================
   Every routine action used to be a form POST followed by a redirect and a
   full page reload: approving one post out of forty rebuilt the entire page,
   lost the scroll position, closed whatever was open, and took as long as the
   database round-trip plus the render of 1,500 lines of markup.

   This intercepts those same forms, sends them with fetch, and updates the one
   row that changed. The forms keep their `action` and `method`, so with
   JavaScript off, or if this file fails to load, every one of them still works
   exactly as before — this is an enhancement layer, not a replacement.

   No framework on purpose. What is needed here is a fetch wrapper, a toast
   stack and a poller; a framework would be more code to read, not less.
   =========================================================================== */

const CF = (() => {
  'use strict';

  /* ── toasts ────────────────────────────────────────────────────────────
     Bottom-right so they never cover the row the user just acted on. */

  const ICONS = {
    ok:    'bi-check-circle-fill',
    error: 'bi-exclamation-triangle-fill',
    info:  'bi-info-circle-fill',
  };

  function toastHost() {
    let host = document.getElementById('cf-toasts');
    if (!host) {
      host = document.createElement('div');
      host.id = 'cf-toasts';
      host.setAttribute('role', 'status');
      host.setAttribute('aria-live', 'polite');
      document.body.appendChild(host);
    }
    return host;
  }

  function toast(kind, title, detail, ms) {
    const el = document.createElement('div');
    el.className = `cf-toast cf-toast-${kind === 'ok' ? 'ok' : kind === 'error' ? 'error' : 'info'}`;
    el.innerHTML = `
      <i class="bi ${ICONS[kind] || ICONS.info}"></i>
      <div class="cf-toast-body">
        <div class="cf-toast-title"></div>
        ${detail ? '<div class="cf-toast-detail"></div>' : ''}
      </div>
      <button class="cf-toast-close" aria-label="Dismiss">&times;</button>`;
    // textContent, not innerHTML: these strings carry model output and API
    // error text, neither of which is ours to trust as markup.
    el.querySelector('.cf-toast-title').textContent = title;
    if (detail) el.querySelector('.cf-toast-detail').textContent = detail;

    const dismiss = () => {
      el.classList.add('is-leaving');
      el.addEventListener('animationend', () => el.remove(), { once: true });
    };
    el.querySelector('.cf-toast-close').addEventListener('click', dismiss);
    toastHost().appendChild(el);

    // Errors stay until dismissed. Something the user needs to read and act on
    // should not disappear while they are reading it.
    if (kind !== 'error') setTimeout(dismiss, ms || 4000);
    return el;
  }

  /* ── requests ──────────────────────────────────────────────────────────── */

  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      ...options,
      headers: { 'X-Requested-With': 'fetch', ...(options.headers || {}) },
    });

    // A session that expired mid-action returns the login page, not JSON.
    // Parsing that as JSON produced "Unexpected token <" and hid the fact that
    // the user simply needed to sign in again.
    if (response.redirected && /\/login/.test(response.url)) {
      window.location = response.url;
      throw new Error('Your session expired. Please sign in again.');
    }

    const type = response.headers.get('content-type') || '';
    const body = type.includes('application/json')
      ? await response.json()
      : { ok: response.ok, error: await response.text() };

    if (!response.ok || body.ok === false) {
      throw new Error(body.error || body.message || `Request failed (${response.status})`);
    }
    return body;
  }

  /* ── row helpers ───────────────────────────────────────────────────────── */

  function flash(el) {
    if (!el) return;
    el.classList.remove('is-updated');
    void el.offsetWidth;                 // restart the animation
    el.classList.add('is-updated');
  }

  function removeRow(el) {
    if (!el) return;
    el.classList.add('is-leaving');
    setTimeout(() => {
      const table = el.closest('table');
      el.remove();
      // A table that just lost its last row should say so rather than showing
      // a header over nothing.
      if (table && !table.querySelector('tbody tr')) {
        const empty = table.parentElement.querySelector('[data-cf-empty]');
        if (empty) { empty.hidden = false; table.hidden = true; }
      }
    }, 200);
  }

  function setChip(el, state, label) {
    if (!el) return;
    el.className = `chip chip-${state}`;
    el.textContent = label || state.replace(/_/g, ' ');
  }

  function busy(button, on) {
    if (!button) return;
    button.classList.toggle('is-busy', !!on);
    button.disabled = !!on;
  }

  /* ── declarative actions ───────────────────────────────────────────────
     Mark a form `data-cf-action` and it submits without leaving the page:

       <form method="post" action="/approve/123"
             data-cf-action
             data-cf-confirm="Publish this post?"
             data-cf-row="#post-123"
             data-cf-on-success="remove">

     The form still works with JavaScript disabled. */

  function bindActions(root = document) {
    root.querySelectorAll('form[data-cf-action]').forEach((form) => {
      if (form.dataset.cfBound) return;
      form.dataset.cfBound = '1';
      form.addEventListener('submit', onSubmit);
    });
  }

  async function onSubmit(event) {
    const form = event.currentTarget;
    const confirmText = form.dataset.cfConfirm;
    if (confirmText && !window.confirm(confirmText)) {
      event.preventDefault();
      return;
    }
    event.preventDefault();

    const button = form.querySelector('[type="submit"]') || event.submitter;
    const row = form.dataset.cfRow ? document.querySelector(form.dataset.cfRow) : form.closest('tr');

    busy(button, true);
    try {
      const result = await request(form.action, { method: 'POST', body: new FormData(form) });

      toast('ok', result.message || form.dataset.cfSuccess || 'Done.', result.detail);

      switch (form.dataset.cfOnSuccess) {
        case 'remove': removeRow(row); break;
        case 'reload': window.location.reload(); break;
        default:
          if (result.state) {
            setChip(row && row.querySelector('[data-cf-chip]'), result.state, result.state_label);
          }
          flash(row);
      }
      if (result.job_id) track(result.job_id, form.dataset.cfJobUrl);
      document.dispatchEvent(new CustomEvent('cf:action', { detail: { form, result } }));
    } catch (error) {
      toast('error', form.dataset.cfError || 'That did not work.', error.message);
    } finally {
      busy(button, false);
    }
  }

  /* ── background jobs ───────────────────────────────────────────────────
     Generation and rendering run in worker threads with a pollable status.
     This shows what step the worker last reported rather than a spinner that
     says nothing for ninety seconds. */

  const POLL_MS = 1200;

  function jobStrip(id) {
    let strip = document.getElementById(`cf-job-${id}`);
    if (strip) return strip;

    strip = document.createElement('div');
    strip.id = `cf-job-${id}`;
    strip.className = 'cf-job';
    strip.innerHTML = `
      <i class="bi bi-arrow-repeat"></i>
      <span class="cf-job-label">Starting…</span>
      <span class="cf-job-bar is-indeterminate"><span></span></span>`;

    const host = document.querySelector('[data-cf-jobs]');
    (host || document.querySelector('.main-content > .p-4') || document.body).prepend(strip);
    return strip;
  }

  function track(jobId, statusUrl) {
    const strip = jobStrip(jobId);
    const label = strip.querySelector('.cf-job-label');
    const bar = strip.querySelector('.cf-job-bar');
    const url = (statusUrl || '/api/status') + (statusUrl ? '' : '');

    const tick = async () => {
      let state;
      try {
        state = await request(`${url}/${jobId}`.replace(/([^:])\/\//g, '$1/'));
      } catch (error) {
        // A poll that fails is not the job failing. Keep polling; if the job
        // really died the next successful poll will say so.
        setTimeout(tick, POLL_MS * 2);
        return;
      }

      if (state.status === 'done' || state.done) {
        strip.remove();
        toast('ok', state.message || 'Finished.', state.detail);
        document.dispatchEvent(new CustomEvent('cf:job-done', { detail: { jobId, state } }));
        return;
      }
      if (state.status === 'error' || state.error) {
        strip.remove();
        toast('error', 'That job failed.', state.error || state.message);
        return;
      }

      label.textContent = state.message || state.step || 'Working…';
      if (typeof state.percent === 'number') {
        bar.classList.remove('is-indeterminate');
        bar.firstElementChild.style.width = `${Math.max(2, Math.min(100, state.percent))}%`;
      }
      setTimeout(tick, POLL_MS);
    };

    tick();
    return strip;
  }

  /* ── boot ──────────────────────────────────────────────────────────────── */

  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(() => {
    bindActions();

    // Server-rendered flash messages become toasts, so a redirect and a fetch
    // action report the same way in the same place.
    document.querySelectorAll('[data-cf-flash]').forEach((el) => {
      toast(el.dataset.cfFlash === 'danger' ? 'error'
          : el.dataset.cfFlash === 'success' ? 'ok' : 'info',
        el.textContent.trim());
      el.remove();
    });

    // Any job already running when the page loaded — a render started before a
    // reload used to simply vanish from the UI.
    document.querySelectorAll('[data-cf-track]').forEach((el) => {
      track(el.dataset.cfTrack, el.dataset.cfJobUrl);
    });
  });

  return { toast, request, track, flash, removeRow, setChip, busy, bindActions };
})();

window.CF = CF;
