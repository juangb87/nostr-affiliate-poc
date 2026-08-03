(() => {
  const root = document.documentElement;
  const safeStorage = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (_) {} }
  };
  const applyLanguage = (language) => {
    const next = language === "en" ? "en" : "es";
    root.dataset.lang = next;
    root.lang = next;
    document.querySelectorAll("[data-invite-language]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.inviteLanguage === next));
    });
    document.querySelectorAll("[data-status-es][data-status-en]").forEach((status) => {
      status.textContent = next === "en" ? status.dataset.statusEn : status.dataset.statusEs;
    });
    const merchantName = document.querySelector("[data-invite-merchant-name], .invite-brand-name")?.textContent?.trim();
    if (merchantName && merchantName !== "Programa de afiliados") {
      document.title = next === "en" ? `${merchantName} · Affiliate program` : `${merchantName} · Programa de afiliados`;
    }
    safeStorage.set("meerat-language", next);
    window.dispatchEvent(new CustomEvent("meerat-language-change", {detail: {language: next}}));
  };
  const requested = new URLSearchParams(window.location.search).get("lang");
  const initial = [requested, safeStorage.get("meerat-language"), root.dataset.lang, navigator.language?.slice(0, 2)]
    .find((value) => value === "es" || value === "en") || "es";
  applyLanguage(initial);
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-invite-language]");
    if (button) applyLanguage(button.dataset.inviteLanguage);
  });
})();
