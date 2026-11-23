// Mist MPSK Mobile - small progressive enhancements. Everything works without it.
(function () {
  "use strict";

  // Service worker: needs HTTPS (or localhost). On plain HTTP the app still works.
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }

  // Live filter for site / PSK lists.
  document.querySelectorAll("[data-filter]").forEach(function (input) {
    var list = document.querySelector(input.dataset.filter);
    if (!list) return;
    var rows = list.querySelectorAll("[data-text]");
    var none = list.querySelector("[data-no-match]");
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (r) {
        var hit = !q || r.dataset.text.indexOf(q) !== -1;
        r.hidden = !hit;
        if (hit) shown++;
      });
      if (none) none.hidden = shown !== 0 || rows.length === 0;
      document.querySelectorAll("[data-hide-when-filtering]").forEach(function (el) { el.hidden = !!q; });
    });
  });

  // Generate a new passphrase in the browser from the config's alphabet.
  document.querySelectorAll("[data-regen]").forEach(function (btn) {
    var input = document.querySelector(btn.dataset.regen);
    btn.addEventListener("click", function () {
      var alphabet = input.dataset.alphabet, length = parseInt(input.dataset.length, 10);
      var out = "";
      while (out.length < length) {
        var buf = new Uint32Array(length * 2);
        crypto.getRandomValues(buf);
        for (var i = 0; i < buf.length && out.length < length; i++) {
          // rejection sampling keeps every character equally likely
          var limit = Math.floor(4294967296 / alphabet.length) * alphabet.length;
          if (buf[i] < limit) out += alphabet[buf[i] % alphabet.length];
        }
        if (out[0] === " " || out[out.length - 1] === " ") out = "";
      }
      input.value = out;
    });
  });

  // Expiry: show the date picker only for "On a date...".
  var expiry = document.getElementById("expiry"), expDate = document.getElementById("expiry-date");
  if (expiry && expDate) {
    var sync = function () {
      var on = expiry.value === "date";
      expDate.hidden = !on;
      expDate.required = on;
    };
    expiry.addEventListener("change", sync);
    sync();
  }

  // Copy to clipboard (falls back to selecting the text on plain HTTP).
  function copyText(text, btn) {
    var done = function () { var t = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.textContent = t; }, 1500); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text) && done(); });
    } else if (legacyCopy(text)) {
      done();
    }
  }
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select(); ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    return ok;
  }
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var el = document.querySelector(btn.dataset.copy);
    btn.addEventListener("click", function () { copyText(el.textContent.trim(), btn); });
  });

  // Share: the QR image + text where the phone supports it, else text only, else open the image.
  var share = document.getElementById("share");
  if (share) {
    var text = share.dataset.title + "\nWi-Fi: " + share.dataset.ssid + "\nPassword: " + share.dataset.pass;
    if (!navigator.share) {
      share.textContent = "Copy Wi-Fi details";
      share.addEventListener("click", function () { copyText(text, share); });
    } else {
      share.addEventListener("click", function () {
        fetch(share.dataset.png, { credentials: "same-origin" })
          .then(function (r) { if (!r.ok) throw new Error(); return r.blob(); })
          .then(function (blob) {
            var file = new File([blob], "wifi-qr.png", { type: "image/png" });
            var data = { title: share.dataset.title, text: text, files: [file] };
            if (navigator.canShare && navigator.canShare(data)) return navigator.share(data);
            return navigator.share({ title: share.dataset.title, text: text });
          })
          .catch(function (e) {
            if (e && e.name === "AbortError") return;   // user closed the share sheet
            navigator.share({ title: share.dataset.title, text: text }).catch(function () {});
          });
      });
    }
  }

  // Avoid double-submits on slow mobile connections.
  document.querySelectorAll("form").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button:not([type=button])");
      if (b) setTimeout(function () { b.disabled = true; }, 0);
    });
  });
})();

// Users: confirm deletes, role-dependent fields, reload sites when the org changes.
(function () {
  "use strict";
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });
  var form = document.getElementById("user-form");
  if (!form) return;
  var radios = form.querySelectorAll("input[name=role]");
  var scope = form.querySelector("[data-scope]");
  var siteSel = document.getElementById("site-select");
  function syncRole() {
    var checked = form.querySelector("input[name=role]:checked");
    var role = checked ? checked.value : "guest";
    form.querySelectorAll("[data-role-hint]").forEach(function (h) { h.hidden = h.dataset.roleHint !== role; });
    if (scope) scope.hidden = role === "admin";
    form.querySelectorAll("[data-role-only]").forEach(function (el) { el.hidden = el.dataset.roleOnly !== role; });
    var ssidScope = form.querySelector("[data-ssid-scope]");
    if (ssidScope) ssidScope.hidden = !(role === "guest" || role === "self_service");
    var ssidSel = document.getElementById("ssid-select");
    if (ssidSel) ssidSel.required = role === "guest" || role === "self_service";
    if (siteSel) {
      siteSel.required = role !== "admin";
      var all = siteSel.querySelector("[data-guest-only]");
      if (all) {
        all.hidden = all.disabled = role !== "guest";
        if (role !== "guest" && siteSel.value === all.value) siteSel.value = "";
      }
    }
  }
  radios.forEach(function (r) { r.addEventListener("change", syncRole); });
  syncRole();
  // Org or site changed: reload so the site / SSID lists match (never sends the password).
  form.querySelectorAll("[data-reload-on-change]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      var checked = form.querySelector("input[name=role]:checked");
      var q = new URLSearchParams({ name: form.elements.name.value, role: checked ? checked.value : "guest" });
      if (form.elements.config) q.set("config", form.elements.config.value);
      if (form.elements.site_id && sel.name !== "config") q.set("site_id", form.elements.site_id.value);
      if (form.elements.key_expiry) q.set("key_expiry", form.elements.key_expiry.value);
      location.href = form.getAttribute("action") + "?" + q.toString();
    });
  });
})();
