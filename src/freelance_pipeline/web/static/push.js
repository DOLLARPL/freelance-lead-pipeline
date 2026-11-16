/* Powiadomienia push: rejestracja subskrypcji i przyciski na stronie /notifications. */
(() => {
  "use strict";

  const page = document.body.dataset.page;
  if (page !== "notifications") return;

  const card = document.querySelector(".card");
  const stateEl = document.getElementById("push-state");
  const resultEl = document.getElementById("push-result");
  const enableBtn = document.getElementById("push-enable");
  const testBtn = document.getElementById("push-test");
  const disableBtn = document.getElementById("push-disable");
  const vapidKey = card ? card.dataset.vapid : "";

  function say(text, kind) {
    if (stateEl) {
      stateEl.textContent = text;
      stateEl.className = "notice" + (kind ? " " + kind : "");
    }
  }

  function note(text) {
    if (resultEl) resultEl.textContent = text;
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(base64);
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  function withTimeout(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error(label + " nie odpowiedział w " + ms / 1000 + " s")), ms)),
    ]);
  }

  async function registration() {
    if (!("serviceWorker" in navigator)) return null;
    // Stara rejestracja miala zasieg /static/ i blokowala "ready" na tej stronie.
    const olds = await navigator.serviceWorker.getRegistrations();
    await Promise.all(olds.filter((r) => r.scope.endsWith("/static/")).map((r) => r.unregister()));
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    await withTimeout(navigator.serviceWorker.ready, 15000, "service worker");
    return reg;
  }

  async function currentSubscription() {
    const reg = await navigator.serviceWorker.getRegistration();
    if (!reg) return null;
    return reg.pushManager.getSubscription();
  }

  async function refresh() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      say("Ta przeglądarka nie obsługuje powiadomień push. Na iPhone dodaj stronę do ekranu początkowego.", "err");
      return;
    }
    let scope = "";
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      scope = reg ? reg.scope : "";
    } catch (_) {}
    const sub = await currentSubscription();
    if (Notification.permission === "denied") {
      say("Powiadomienia są zablokowane w ustawieniach przeglądarki. Włącz je dla tej strony.", "err");
      return;
    }
    const diag = ` (zgoda: ${Notification.permission || "brak"}, service worker: ${scope || "brak"})`;
    say((sub ? "To urządzenie ma włączone powiadomienia." : "To urządzenie nie ma jeszcze powiadomień.") + diag, sub ? "ok" : "");
  }

  if (enableBtn) enableBtn.addEventListener("click", async () => {
    // Kolejnosc ma znaczenie: na Androidzie prosba o zgode musi isc natychmiast
    // w geście kliknięcia. Rejestracja service workera przed nia zjada aktywacje
    // uzytkownika i Chrome odrzuca prompt bez pytania (przycisk "milczy").
    if (!("Notification" in window)) {
      say("Ta przeglądarka nie ma powiadomień. Na Androidzie otwórz panel w Chrome i zainstaluj aplikację.", "err");
      return;
    }
    if (Notification.permission === "denied") {
      say("Powiadomienia są zablokowane dla tej strony. Wejdź w ustawienia Chrome, Uprawnienia witryny, Powiadomienia i zezwól dla netcup-hermes.tail44d55a.ts.net.", "err");
      return;
    }
    say("proszę o zgodę...", "");
    let permission = Notification.permission;
    try {
      if (permission !== "granted") {
        permission = await Notification.requestPermission();
      }
    } catch (error) {
      say("przeglądarka odrzuciła pytanie o zgodę: " + error.message, "err");
      return;
    }
    if (permission !== "granted") {
      say("Nie przyznałeś zgody na powiadomienia.", "err");
      return;
    }
    try {
      await registration();
      const reg = await navigator.serviceWorker.ready;
      const sub = await withTimeout(reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey),
      }), 20000, "subskrypcja");
      const response = await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      });
      if (!response.ok) throw new Error("serwer odrzucił subskrypcję: " + response.status);
      say("Gotowe, to urządzenie będzie dostawać powiadomienia.", "ok");
      note("Kliknij „Wyślij testowe”, żeby sprawdzić.");
    } catch (error) {
      say("Nie udało się włączyć: " + error.message, "err");
    }
  });

  if (testBtn) testBtn.addEventListener("click", async () => {
    note("wysyłam...");
    try {
      const response = await fetch("/api/push/test", { method: "POST" });
      const data = await response.json();
      note(`wysłane: ${data.sent}, błędy: ${data.failed}, usunięte: ${data.removed}` +
           (data.errors && data.errors.length ? ` (${data.errors.join("; ")})` : ""));
      if (!data.sent && !data.failed) note("brak zapisanych urządzeń, najpierw włącz powiadomienia.");
    } catch (error) {
      note("błąd: " + error.message);
    }
  });

  if (disableBtn) disableBtn.addEventListener("click", async () => {
    try {
      let scope = "";
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      scope = reg ? reg.scope : "";
    } catch (_) {}
    const sub = await currentSubscription();
      if (sub) {
        await fetch("/api/push/unsubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        });
        await sub.unsubscribe();
      }
      say("Powiadomienia wyłączone na tym urządzeniu.", "");
    } catch (error) {
      say("Nie udało się wyłączyć: " + error.message, "err");
    }
  });

  refresh();
})();
