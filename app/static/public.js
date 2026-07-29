(() => {
  const root = document.documentElement;
  const body = document.body;
  const safeStorage = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (_) {} }
  };
  const applyLanguage = (language) => {
    const next = language === "en" ? "en" : "es";
    root.dataset.lang = next;
    root.lang = next;
    const title = next === "en" ? body.dataset.titleEn : body.dataset.titleEs;
    if (title) document.title = title;
    document.querySelectorAll("[data-language]").forEach((item) => item.setAttribute("aria-pressed", String(item.dataset.language === next)));
    document.querySelectorAll("[data-label-es]").forEach((item) => item.setAttribute("aria-label", item.dataset[next === "en" ? "labelEn" : "labelEs"]));
    document.querySelectorAll("[data-text-es]").forEach((item) => { item.textContent = item.dataset[next === "en" ? "textEn" : "textEs"]; });
    safeStorage.set("meerat-language", next);
  };
  const applyTheme = (theme) => {
    const next = theme === "arena" ? "arena" : "night";
    root.dataset.theme = next;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", next === "arena" ? "#eef2ec" : "#141914");
    document.querySelectorAll("[data-theme-button]").forEach((item) => item.setAttribute("aria-pressed", String(item.dataset.themeButton === next)));
    safeStorage.set("meerat-theme", next);
  };
  applyLanguage(safeStorage.get("meerat-language") || "es");
  applyTheme(safeStorage.get("meerat-theme") || "night");
  document.addEventListener("click", async (event) => {
    const language = event.target.closest("[data-language]");
    if (language) applyLanguage(language.dataset.language);
    const theme = event.target.closest("[data-theme-button]");
    if (theme) applyTheme(theme.dataset.themeButton);
    const copy = event.target.closest("[data-copy]");
    if (!copy) return;
    const status = document.querySelector("[data-copy-status]");
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      if (status) status.textContent = root.lang === "en" ? "Copied." : "Copiado.";
    } catch (_) {
      if (status) status.textContent = root.lang === "en" ? "Copy unavailable; select it manually." : "No se pudo copiar; seleccionalo manualmente.";
    }
  });
})();
