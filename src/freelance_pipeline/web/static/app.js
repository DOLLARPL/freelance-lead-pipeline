/* Skroty klawiszowe, kopiowanie wiadomosci, licznik znakow, PWA. */
(() => {
  "use strict";

  // Na telefonie filtry startuja zwinięte, zeby lista zaczynala sie wyzej.
  const filters = document.querySelector(".filters-wrap");
  if (filters && window.matchMedia("(max-width: 860px)").matches) filters.removeAttribute("open");

  const page = document.body.dataset.page;
  const toastEl = document.getElementById("toast");

  function toast(text) {
    if (!toastEl) return;
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toastEl._timer);
    toastEl._timer = setTimeout(() => { toastEl.hidden = true; }, 2200);
  }

  function leadData() {
    const node = document.getElementById("lead-data");
    if (!node) return {};
    try { return JSON.parse(node.textContent || "{}"); } catch (_) { return {}; }
  }

  function draftText() {
    const area = document.getElementById("draft-body");
    return area ? area.value : "";
  }

  async function copyDraft() {
    const text = draftText();
    if (!text.trim()) { toast("brak wiadomości do skopiowania"); return false; }
    try {
      await navigator.clipboard.writeText(text);
      toast("skopiowane, " + text.length + " znaków");
      return true;
    } catch (_) {
      const area = document.getElementById("draft-body");
      if (area) { area.select(); document.execCommand("copy"); toast("skopiowane"); return true; }
      toast("nie udało się skopiować");
      return false;
    }
  }

  async function copyAndOpen() {
    const ok = await copyDraft();
    const { url } = leadData();
    if (ok && url) window.open(url, "_blank", "noopener");
  }

  function isTyping(event) {
    const el = event.target;
    if (!el) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  // ------------------------------------------------------------------ lista

  if (page === "queue") {
    let cursor = -1;
    const rows = () => Array.from(document.querySelectorAll(".row"));

    function focusRow(index) {
      const list = rows();
      if (!list.length) return;
      cursor = Math.max(0, Math.min(index, list.length - 1));
      list.forEach((row) => row.classList.remove("sel"));
      list[cursor].classList.add("sel");
      list[cursor].scrollIntoView({ block: "nearest" });
    }

    document.addEventListener("keydown", (event) => {
      if (isTyping(event) || event.metaKey || event.ctrlKey || event.altKey) return;
      const list = rows();
      const current = list[cursor >= 0 ? cursor : 0];
      switch (event.key) {
        case "j": focusRow(cursor + 1); event.preventDefault(); break;
        case "k": focusRow(cursor - 1); event.preventDefault(); break;
        case "Enter":
        case "o":
          if (current) window.location.href = current.getAttribute("href");
          event.preventDefault();
          break;
        case "/": {
          const input = document.querySelector("input[name=q]");
          if (input) { input.focus(); input.select(); }
          event.preventDefault();
          break;
        }
        case "?": toast("j/k ruch, Enter otwórz, / szukaj, na karcie r i x"); break;
        default: break;
      }
    });
  }

  // ------------------------------------------------------------------ karta

  if (page === "lead") {
    const copyBtn = document.getElementById("copy-btn");
    const copyBtn2 = document.getElementById("copy-btn2");
    const copyOpen = document.getElementById("copyopen-btn");
    if (copyBtn) copyBtn.addEventListener("click", copyDraft);
    if (copyBtn2) copyBtn2.addEventListener("click", copyDraft);
    const copyBtn3 = document.getElementById("copy-btn3");
    if (copyBtn3) copyBtn3.addEventListener("click", copyDraft);
    if (copyOpen) copyOpen.addEventListener("click", copyAndOpen);

    const area = document.getElementById("draft-body");
    const counter = document.getElementById("char-count");
    const autosize = () => {
      if (!area) return;
      // Telefon: draft rosnie, zeby nie bylo wewnetrznego scrolla. Desktop: stala wysokosc,
      // bo inaczej dlugi tekst wypycha przyciski Kopiuj/Odpisalem pod dolna krawedz ekranu.
      if (!window.matchMedia("(max-width: 860px)").matches) {
        area.style.height = "";
        return;
      }
      area.style.height = "auto";
      area.style.height = Math.max(220, area.scrollHeight + 4) + "px";
    };
    if (area && counter) {
      const update = () => {
        counter.textContent = area.value.length;
        autosize();
      };
      area.addEventListener("input", update);
      update();
      window.addEventListener("resize", autosize);
    }

    document.addEventListener("keydown", (event) => {
      if (isTyping(event) || event.metaKey || event.ctrlKey || event.altKey) {
        if (event.key === "Escape" && isTyping(event)) event.target.blur();
        return;
      }
      switch (event.key) {
        case "c": copyDraft(); event.preventDefault(); break;
        case "e": if (area) { area.focus(); } event.preventDefault(); break;
        case "v": {
          const open = document.getElementById("open-portal-btn");
          if (open) window.open(open.getAttribute("href"), "_blank", "noopener");
          event.preventDefault();
          break;
        }
        case "q": {
          const form = document.querySelector('form[action$="/quote"]');
          if (form) form.requestSubmit();
          event.preventDefault();
          break;
        }
        case "n": {
          const next = document.querySelector('.rail-item[href^="/lead/"]');
          if (next) window.location.href = next.getAttribute("href");
          event.preventDefault();
          break;
        }
        case "p": {
          const input = document.querySelector(".improve input[name=instruction]");
          if (input) input.focus();
          event.preventDefault();
          break;
        }
        case "r":
        case "1": {
          const form = document.querySelector('form[action$="/status"] input[value="replied"]');
          if (form) form.form.requestSubmit();
          event.preventDefault();
          break;
        }
        case "x":
        case "2": {
          const form = document.querySelector('form[action$="/status"] input[value="skipped"]');
          if (form) form.form.requestSubmit();
          event.preventDefault();
          break;
        }
        case "?": toast("c kopiuj, v portal, p popraw, r odpisałem, x odrzuć, n następna, q wycena, Esc lista"); event.preventDefault(); break;
        case "Escape": window.location.href = "/"; event.preventDefault(); break;
        default: break;
      }
    });
  }

  // --------------------------------------------------- wątek: pola treści
  // Podgląd wątku ma pola o wysokości dopasowanej do treści, żeby na telefonie
  // nie trzeba było przewijać każdego textarea osobno.
  function autosizePodglad(scope) {
    (scope || document).querySelectorAll(".pod-row textarea, .watek-paste textarea").forEach((area) => {
      area.style.height = "auto";
      area.style.height = Math.max(72, area.scrollHeight + 2) + "px";
    });
  }
  document.addEventListener("input", (event) => {
    if (event.target.matches && event.target.matches(".pod-row textarea, .watek-paste textarea")) {
      autosizePodglad(event.target.closest("#watek-podglad") || document);
    }
  });
  document.body.addEventListener("htmx:afterSwap", (event) => autosizePodglad(event.target));
  autosizePodglad(document);

  // ------------------------------------------------- wątek: usuwanie wiersza
  // Wiersz podglądu znika z DOM, więc jego pola nie lecą w formularzu przy
  // zapisie. Delegacja działa też po podmianie fragmentu przez htmx.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-del-row]");
    if (!button) return;
    const row = button.closest(".pod-row");
    if (row) row.remove();
    event.preventDefault();
  });

  // ------------------------------------------------------------------ PWA

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
    });
  }
})();

/* Picker uwag: Alt+klik (Option+klik) na elemencie, komentarz, zapis do /api/uwagi. */
(() => {
  if (window.__uwagiPicker) return;
  window.__uwagiPicker = true;
  const box = document.createElement('div');
  box.id = 'uwaga-box';
  box.hidden = true;
  box.innerHTML = `
    <div class="uwaga-head">Komentarz do elementu</div>
    <div class="uwaga-sel"></div>
    <textarea placeholder="Co poprawić?"></textarea>
    <div class="uwaga-actions">
      <button type="button" data-send>Wyślij</button>
      <button type="button" data-cancel>Anuluj</button>
    </div>`;
  document.body.appendChild(box);

  let target = null;
  const selector = (el) => {
    if (!el || el === document.body) return 'body';
    if (el.id) return '#' + el.id;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && parts.length < 5) {
      let p = cur.tagName.toLowerCase();
      if (cur.classList.length) p += '.' + [...cur.classList].slice(0, 3).join('.');
      const parent = cur.parentElement;
      if (parent) {
        const same = [...parent.children].filter((x) => x.tagName === cur.tagName);
        if (same.length > 1) p += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(p);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };

  document.addEventListener('click', (e) => {
    if (!e.altKey) return;
    e.preventDefault();
    e.stopPropagation();
    target = e.target;
    box.hidden = false;
    box.querySelector('.uwaga-sel').textContent = selector(target);
    box.querySelector('textarea').value = '';
    box.querySelector('textarea').focus();
  }, true);

  box.querySelector('[data-cancel]').addEventListener('click', () => {
    box.hidden = true;
    target = null;
  });

  box.querySelector('[data-send]').addEventListener('click', async () => {
    const komentarz = box.querySelector('textarea').value.trim();
    if (!komentarz || !target) return;
    const r = await fetch('/api/uwagi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        strona: location.pathname,
        selektor: selector(target),
        html: target.outerHTML.slice(0, 2000),
        komentarz,
      }),
    });
    if (r.ok) {
      box.hidden = true;
      target = null;
    } else {
      alert('Nie zapisano uwagi.');
    }
  });
})();
