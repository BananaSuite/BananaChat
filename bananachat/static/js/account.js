// Account, personalities and free-quota pages: live character counters for long text fields,
// and the limit-request form (shows the fields of the chosen kind of request).
import { el, pageData, t } from "./core.js";

/** <textarea data-counter maxlength="N"> shows "used / N" below itself (announced politely when near the limit). */
function initCounters() {
  for (const field of document.querySelectorAll("textarea[data-counter][maxlength]")) {
    const limit = Number(field.getAttribute("maxlength"));
    const counter = el("div", { class: "char-counter", "aria-live": "polite" });
    if (!field.id) continue;
    counter.id = `${field.id}-counter`;
    field.after(counter);
    const describedBy = field.getAttribute("aria-describedby");
    field.setAttribute("aria-describedby", describedBy ? `${describedBy} ${counter.id}` : counter.id);
    const update = () => {
      const used = field.value.length;
      counter.textContent = t("account_characters", { used, max: limit });
      counter.classList.toggle("is-over", used >= limit);
      // Only announce when the limit is close, to avoid chatter on every keystroke.
      counter.setAttribute("aria-live", used >= limit * 0.9 ? "polite" : "off");
    };
    field.addEventListener("input", update);
    update();
  }
}

/** A token amount as typed in the form: 60k, 1.5M, else the whole number (mirrors formatting.token_input). */
function tokenInput(value) {
  for (const [suffix, scale] of [["M", 1e6], ["k", 1e3]]) {
    const scaled = value / scale;
    if (value >= scale && Math.abs(scaled * 10 - Math.round(scaled * 10)) < 1e-9) {
      return `${Number(scaled.toFixed(1))}${suffix}`;
    }
  }
  return String(Math.round(value));
}

/** Suggested new value for a field, from the account's current limits in the chosen pool. */
function suggestion(name, current) {
  const value = Number(current[name] || 0);
  return tokenInput(Math.max(1000, Math.ceil(value * 2)));
}

function replaceOptions(select, options, selected) {
  select.replaceChildren(...options.map((option) => el("option", { value: option.value, text: option.label })));
  if (options.some((option) => option.value === selected)) select.value = selected;
}

/** The request form lists every field without JavaScript; with it, only the chosen kind's fields show. */
function initQuotaForm() {
  const kind = document.getElementById("quota_kind");
  const pool = document.getElementById("quota_pool");
  const data = pageData("quota-data");
  if (!kind || !pool || !data.pools_for) return;
  const poolField = document.querySelector(".quota-form [data-pool-field]");
  const ruleSelect = document.getElementById("quota_rate_per");
  const requests = document.getElementById("quota_rate");
  const effortModel = document.getElementById("quota_effort_model");
  const effortLevel = document.getElementById("quota_effort_level");
  const sync = () => {
    const allowed = data.pools_for[kind.value] || [];
    for (const option of pool.options) option.disabled = !allowed.includes(option.value);
    if (!allowed.includes(pool.value) && allowed.length) pool.value = allowed[0];
    if (poolField) poolField.hidden = kind.value === "effort";
    for (const set of document.querySelectorAll(".quota-form fieldset[data-kind]")) {
      const shown = set.dataset.kind === kind.value;
      set.hidden = !shown;
      set.disabled = !shown;
    }
    // Extra tokens only apply to services with a 5-hour limit; elsewhere only unlimited use can be asked for.
    const extra = document.getElementById("quota_extra");
    const unlimited = document.getElementById("quota_unlimited");
    const note = document.getElementById("quota_no_window");
    const noWindow = !(data.extra_pools || []).includes(pool.value);
    if (extra) extra.disabled = noWindow || Boolean(unlimited?.checked);
    if (noWindow && unlimited) unlimited.checked = true;
    if (note) note.hidden = !noWindow;
  };
  const suggestRequests = () => {
    const rules = (data.current[pool.value] || {}).rules || [];
    const rule = rules.find((item) => item.per === ruleSelect?.value);
    if (rule && requests) requests.value = Math.max(2, Math.ceil(rule.requests * 2));
  };
  const fill = () => {
    const current = data.current[pool.value];
    if (!current) return;
    for (const input of document.querySelectorAll(".quota-form [data-current]:not([data-current=requests])")) {
      input.value = suggestion(input.dataset.current, current);
    }
    if (ruleSelect) {
      replaceOptions(ruleSelect, (current.rules || []).map((rule) => ({ value: rule.per, label: rule.label })), ruleSelect.value);
      suggestRequests();
    }
  };
  const syncEffort = () => {
    const target = (data.efforts || []).find((item) => item.value === effortModel?.value);
    if (target && effortLevel) replaceOptions(effortLevel, target.levels, effortLevel.value);
  };
  kind.addEventListener("change", () => { const before = pool.value; sync(); if (pool.value !== before) fill(); });
  pool.addEventListener("change", () => { sync(); fill(); });
  ruleSelect?.addEventListener("change", suggestRequests);
  effortModel?.addEventListener("change", syncEffort);
  document.getElementById("quota_unlimited")?.addEventListener("change", sync);
  sync();
  // Arriving from "Request access" in the chat: bring the form into view.
  if (kind.value === "effort" && window.location.hash === "#quota") document.getElementById("quota_reason")?.focus();
}

// Keep the section navigator useful while reading a long account page.
function initSectionNavigation() {
  const links = [...document.querySelectorAll('.account-toc a[href^="#"]')];
  const sections = links.map((link) => document.getElementById(link.hash.slice(1)));
  if (!links.length) return;
  let scheduled = false;
  const update = () => {
    scheduled = false;
    const edge = (document.querySelector('.topbar')?.getBoundingClientRect().bottom || 0) + 32;
    let current = 0;
    sections.forEach((section, index) => {
      if (section && section.getBoundingClientRect().top <= edge) current = index;
    });
    // Short final sections cannot always reach the top edge of the viewport.
    if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) current = links.length - 1;
    links.forEach((link, index) => {
      if (index === current) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
  };
  const schedule = () => {
    if (!scheduled) { scheduled = true; requestAnimationFrame(update); }
  };
  window.addEventListener('scroll', schedule, { passive: true });
  window.addEventListener('resize', schedule);
  window.addEventListener('hashchange', schedule);
  update();
}

initSectionNavigation();
initCounters();
initQuotaForm();
