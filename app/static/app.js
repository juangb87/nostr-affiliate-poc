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

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-merchant-enrollment]");
  if (!form) return;
  event.preventDefault();
  const status = form.querySelector("[data-enrollment-status]");
  const link = form.querySelector("[data-enrollment-link]");
  const button = form.querySelector("button[type='submit']");
  const fields = new FormData(form);
  const payload = {
    campaign_id: String(fields.get("campaign_id") || "").trim(),
    affiliate_pubkey: String(fields.get("affiliate_pubkey") || "").trim(),
    lightning_address: String(fields.get("lightning_address") || "").trim() || null
  };
  button.disabled = true;
  status.classList.remove("error");
  status.textContent = "Creando enrollment y publicando la prueba Nostr…";
  link.hidden = true;
  try {
    const result = await jsonFetch("/app/merchant/enrollments", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    const safeUrl = new URL(result.ref_url, window.location.origin);
    if (safeUrl.origin !== window.location.origin) throw new Error("El servidor devolvió un enlace inválido");
    if (result.duplicate) {
      status.textContent = "Esta identidad ya estaba inscripta y continúa aprobada.";
    } else if (result.nostr_status === "published") {
      status.textContent = "Afiliado aprobado y prueba Nostr publicada. Ya puede iniciar sesión como Affiliate.";
    } else {
      status.textContent = "Afiliado aprobado. La prueba Nostr quedó pendiente de publicación; el login Affiliate ya está habilitado.";
    }
    link.href = safeUrl.href;
    link.textContent = safeUrl.href;
    link.hidden = false;
    if (!result.duplicate) form.reset();
  } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
  } finally {
    button.disabled = false;
  }
});
