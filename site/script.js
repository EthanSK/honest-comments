/* =================================================================
   honest-comments — landing page behaviour
   =================================================================
   Vanilla JS, no deps. Three small jobs:
     1. Copy-to-clipboard for the starter-prompt code block.
     2. Reveal-on-scroll (IntersectionObserver) for .reveal elements.
     3. Toggle a "stuck" class on the sticky nav once you scroll past the top
        so it gains a border/stronger background only when overlapping content.
   Everything is progressive-enhancement: if JS fails, the page is still fully
   readable and the prompt is still selectable for manual copy.
   ================================================================= */

(function () {
  "use strict";

  /* ---------------------------------------------------------------
     1. COPY BUTTON
     The button lives in .codeblock[data-copy-block]; the text to copy is
     inside the element marked [data-copy-source]. We read textContent (the
     raw, un-rendered prompt) rather than innerHTML so we never copy markup.
     Uses the async Clipboard API with a legacy execCommand fallback for
     older/locked-down browsers (e.g. some in-app webviews).
  --------------------------------------------------------------- */
  function initCopy() {
    const block = document.querySelector("[data-copy-block]");
    if (!block) return;

    const btn = block.querySelector("[data-copy-btn]");
    const source = block.querySelector("[data-copy-source]");
    if (!btn || !source) return;

    const label = btn.querySelector(".copy-btn__text");
    const defaultText = label ? label.textContent : "Copy";

    btn.addEventListener("click", async function () {
      const text = source.textContent;

      try {
        // Preferred path — modern async clipboard.
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          legacyCopy(text);
        }
        flash("Copied!");
      } catch (err) {
        // Clipboard API can reject in insecure contexts; fall back once.
        try {
          legacyCopy(text);
          flash("Copied!");
        } catch (e2) {
          flash("Press ⌘C"); // last resort: tell the user to copy manually
        }
      }
    });

    // Briefly swap the button label + state, then restore after ~1.6s.
    let restoreTimer = null;
    function flash(msg) {
      if (label) label.textContent = msg;
      btn.classList.add("is-copied");
      clearTimeout(restoreTimer);
      restoreTimer = setTimeout(function () {
        if (label) label.textContent = defaultText;
        btn.classList.remove("is-copied");
      }, 1600);
    }

    // Legacy fallback: stash text in a hidden textarea + execCommand('copy').
    function legacyCopy(text) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      // P2-5: capture execCommand's boolean return. It returns false when the
      // copy was NOT performed (unsupported / blocked context). If we ignore it
      // we'd flash "Copied!" on a failure; instead we throw so the caller's
      // outer catch surfaces the "Press ⌘C" manual-copy hint.
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (!ok) {
        throw new Error("execCommand('copy') returned false");
      }
    }
  }

  /* ---------------------------------------------------------------
     2. REVEAL ON SCROLL
     Each .reveal animates in once when it enters the viewport. We unobserve
     after the first reveal so it doesn't re-animate on scroll-up (feels
     calmer). If IntersectionObserver is missing, just show everything.
  --------------------------------------------------------------- */
  function initReveal() {
    const items = document.querySelectorAll(".reveal");
    if (!items.length) return;

    // No-JS-observer fallback OR reduced-motion: reveal immediately.
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !("IntersectionObserver" in window)) {
      items.forEach((el) => el.classList.add("is-visible"));
      return;
    }

    const io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            io.unobserve(entry.target); // one-shot
          }
        });
      },
      {
        // Trigger a little before the element is fully on screen so the
        // motion completes as it settles into view.
        rootMargin: "0px 0px -8% 0px",
        threshold: 0.08,
      }
    );

    items.forEach((el) => io.observe(el));
  }

  /* ---------------------------------------------------------------
     3. STICKY NAV STATE
     Add .is-stuck to the nav once the page has scrolled a bit, so the nav
     only grows its border/opaque bg when it's actually overlapping content.
     Throttled with requestAnimationFrame to stay cheap on scroll.
  --------------------------------------------------------------- */
  function initNav() {
    const nav = document.getElementById("nav");
    if (!nav) return;

    let ticking = false;
    function update() {
      nav.classList.toggle("is-stuck", window.scrollY > 12);
      ticking = false;
    }
    window.addEventListener(
      "scroll",
      function () {
        if (!ticking) {
          ticking = true;
          requestAnimationFrame(update);
        }
      },
      { passive: true }
    );
    update(); // set correct initial state (e.g. on reload mid-page)
  }

  /* ---------------------------------------------------------------
     Boot once the DOM is parsed. `defer` on the script tag already
     guarantees this, but the guard keeps it safe if moved inline.
  --------------------------------------------------------------- */
  function boot() {
    initCopy();
    initReveal();
    initNav();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
