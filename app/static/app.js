async function jsonFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

async function loginWithNostr(role, button) {
  const status = document.querySelector("[data-login-status]");
  if (!window.nostr) {
    status.textContent = "Necesitás una extensión Nostr compatible, como Alby, para iniciar sesión.";
    status.classList.add("error");
    return;
  }
  const previous = button.textContent;
  button.disabled = true;
  status.classList.remove("error");
  status.textContent = "Preparando desafío seguro…";
  try {
    const challenge = await jsonFetch("/auth/nostr/challenge", {
      method: "POST", body: JSON.stringify({role})
    });
    status.textContent = "Confirmá la firma en tu extensión Nostr…";
    const event = await window.nostr.signEvent({
      kind: challenge.kind,
      created_at: Math.floor(Date.now() / 1000),
      tags: [
        ["challenge", challenge.challenge],
        ["relay", challenge.relay],
        ["role", challenge.role]
      ],
      content: ""
    });
    status.textContent = "Validando identidad…";
    const result = await jsonFetch("/auth/nostr/verify", {
      method: "POST", body: JSON.stringify({event})
    });
    window.location.assign(result.redirect);
  } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
    button.disabled = false;
    button.textContent = previous;
  }
}

document.addEventListener("click", async (event) => {
  const login = event.target.closest("[data-login-role]");
  if (login) {
    document.querySelectorAll("[data-login-role]").forEach(el => el.classList.remove("selected"));
    login.classList.add("selected");
    await loginWithNostr(login.dataset.loginRole, login);
    return;
  }
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    const original = copy.textContent;
    const globalStatus = document.querySelector("[data-global-status]");
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      copy.textContent = "Copiado";
      if (globalStatus) globalStatus.textContent = "Enlace copiado";
    } catch (_) {
      copy.textContent = "Copiá manualmente";
      if (globalStatus) globalStatus.textContent = "No se pudo copiar el enlace automáticamente";
    }
    setTimeout(() => { copy.textContent = original; }, 1800);
    return;
  }
  const logout = event.target.closest("[data-logout]");
  if (logout) {
    const globalStatus = document.querySelector("[data-global-status]");
    logout.disabled = true;
    try {
      await jsonFetch("/auth/logout", {method: "POST", body: "{}"});
      window.location.assign("/app");
    } catch (error) {
      logout.disabled = false;
      if (globalStatus) globalStatus.textContent = `No se pudo cerrar sesión: ${error.message}`;
    }
  }
});
