// Cardvault client behavior — deliberately tiny, no framework.

(function () {
  // Auto-dismiss toasts after 6 seconds (keep max 3 on screen).
  const toasts = document.getElementById("toasts");
  if (toasts) {
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        m.addedNodes.forEach(function (node) {
          if (!(node instanceof HTMLElement) || !node.classList.contains("toast")) return;
          setTimeout(function () { node.remove(); }, 6000);
          const all = toasts.querySelectorAll(".toast");
          for (let i = 3; i < all.length; i++) all[i].remove();
        });
      });
    }).observe(toasts, { childList: true });
  }

  // Network/server errors: nothing was swapped, so the screen still shows DB truth.
  document.body.addEventListener("htmx:responseError", showErrorToast);
  document.body.addEventListener("htmx:sendError", showErrorToast);
  function showErrorToast() {
    if (!toasts) return;
    const div = document.createElement("div");
    div.className = "toast error";
    div.innerHTML =
      '<span>Couldn’t save — nothing was changed. Try again.</span>' +
      '<button type="button" class="close-btn" onclick="this.closest(\'.toast\').remove()">✕</button>';
    toasts.prepend(div);
  }

  // Keep row dimming honest: after a qty-cell swap, re-derive the row's
  // "unowned" state from the fresh input value. (afterSettle fires on the NEW
  // element for outerHTML swaps; afterSwap covers inner swaps.)
  function refreshRowDim(e) {
    const el = e.target;
    if (!(el instanceof HTMLElement)) return;
    const cell = el.classList.contains("qty-cell") ? el : el.querySelector?.(".qty-cell");
    if (!cell || !cell.isConnected) return;
    const row = cell.closest("tr");
    const input = cell.querySelector(".qty-input");
    if (row && input) row.classList.toggle("unowned", (parseInt(input.value, 10) || 0) === 0);
  }
  document.body.addEventListener("htmx:afterSwap", refreshRowDim);
  document.body.addEventListener("htmx:afterSettle", refreshRowDim);

  // "/" opens quick add; Escape closes any modal.
  document.body.addEventListener("keydown", function (e) {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) {
      const btn = document.getElementById("quick-add-btn");
      if (btn) { e.preventDefault(); btn.click(); }
    }
    // Escape is handled natively by <dialog>; only clear a stray non-modal.
    if (e.key === "Escape") {
      const modal = document.getElementById("modal");
      const dialog = modal && modal.querySelector("dialog");
      if (modal && !dialog && modal.innerHTML.trim()) modal.innerHTML = "";
    }
  });

  // Binder flow: Enter in a qty input saves (change event) and jumps to the
  // next row's qty input.
  document.body.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    const input = e.target;
    if (!(input instanceof HTMLInputElement) || !input.classList.contains("qty-input")) return;
    e.preventDefault();
    input.blur(); // fires change → htmx saves
    const rows = Array.from(document.querySelectorAll(".qty-table tbody tr"));
    const row = input.closest("tr");
    const next = rows[rows.indexOf(row) + 1];
    if (next) {
      const nextInput = next.querySelector(".qty-input");
      if (nextInput) { nextInput.focus(); nextInput.select(); }
    }
  });
})();

// -- shell: theme + mobile nav ------------------------------------------------
// Appended as a separate IIFE so the collection behaviour above stays untouched.
(function () {
  const root = document.documentElement;

  function label() {
    const el = document.querySelector("[data-theme-label]");
    if (el) el.textContent = root.dataset.theme === "light" ? "Dark mode" : "Light mode";
  }
  label();

  document.body.addEventListener("click", function (e) {
    const toggle = e.target.closest("[data-theme-toggle]");
    if (toggle) {
      const next = root.dataset.theme === "light" ? "dark" : "light";
      root.dataset.theme = next;
      // Cookie, not localStorage: the server renders data-theme on <html> so
      // there is no flash of the wrong palette on first paint.
      document.cookie = "theme=" + next + ";path=/;max-age=31536000;samesite=lax";
      label();
      return;
    }
    if (e.target.closest("[data-nav-toggle]") || e.target.classList.contains("nav-scrim")) {
      document.getElementById("app-shell")?.classList.toggle("nav-open");
      let scrim = document.querySelector(".nav-scrim");
      if (scrim) { scrim.remove(); return; }
      scrim = document.createElement("div");
      scrim.className = "nav-scrim";
      document.body.appendChild(scrim);
    }
  });
})();


// -- modals -------------------------------------------------------------------
// htmx swaps dialog markup into #modal; a <dialog> only becomes a real centred,
// backdropped, top-layer modal once showModal() is called on it.
(function () {
  const host = document.getElementById("modal");
  if (!host) return;

  function open() {
    const dialog = host.querySelector("dialog");
    if (!dialog || dialog.open) return;
    dialog.showModal();
    // Closing by Esc or backdrop must also clear the markup, or the next
    // open would find a stale dialog already in the DOM.
    dialog.addEventListener("close", function () { host.innerHTML = ""; }, { once: true });
    dialog.addEventListener("click", function (e) {
      if (e.target === dialog) dialog.close();   // clicked the backdrop
    });
    const focusable = dialog.querySelector("input, select, textarea, button");
    if (focusable) focusable.focus();
  }

  host.addEventListener("htmx:afterSwap", open);
  host.addEventListener("htmx:afterSettle", open);
})();


// -- package rule builder -----------------------------------------------------
// Presets are just field values; keeping them declarative in data-preset means
// the server never has to know about "bulk" or "duplicates" as concepts.
(function () {
  const form = document.getElementById("rule-form");
  if (!form) return;

  document.body.addEventListener("click", function (e) {
    const preset = e.target.closest("[data-preset]");
    if (!preset || !form.contains(preset)) return;
    const values = JSON.parse(preset.dataset.preset);
    Object.entries(values).forEach(function ([name, value]) {
      const field = form.querySelector("[name='" + name + "']");
      if (field) field.value = value;
    });
    form.querySelectorAll("[data-presets] .btn").forEach(function (b) {
      b.classList.toggle("btn-primary", b === preset);
    });
  });

  // The set picker posts radios named "expansion"; nothing else to wire up.
})();
