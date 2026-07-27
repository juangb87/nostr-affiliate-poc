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

let affiliateInvitationToken = null;

function clearPreparedInvoice(form) {
  const panel = form.querySelector("[data-invoice-panel]");
  const image = panel.querySelector("[data-invoice-qr]");
  image.removeAttribute("src");
  panel.querySelector("[data-invoice-text]").textContent = "";
  const copy = panel.querySelector("[data-invoice-copy]");
  delete copy.dataset.copy;
  delete form.dataset.preparedPaymentHash;
  delete form.dataset.preparedExpiresAt;
  form.querySelector("[name='evidence']").value = "";
  form.querySelector("[name='confirmed']").checked = false;
  panel.hidden = true;
}

async function resolveAffiliateInvitation() {
  const page = document.querySelector("[data-invite-page]");
  if (!page) return;
  const status = page.querySelector("[data-invite-status]");
  const params = new URLSearchParams(window.location.hash.slice(1));
  const token = params.get("token");
  window.history.replaceState(null, "", "/invite");
  if (!token) {
    status.textContent = "El enlace no contiene una invitación válida. Pedile al Merchant uno nuevo.";
    status.classList.add("error");
    return;
  }
  affiliateInvitationToken = token;
  try {
    const result = await jsonFetch("/invite/resolve", {
      method: "POST", body: JSON.stringify({token})
    });
    page.querySelector("[data-invite-campaign]").textContent = result.campaign_name;
    page.querySelector("[data-invite-commission]").textContent = result.commission_percent;
    page.querySelector("[data-invite-window]").textContent = result.window_days;
    page.querySelector(".lede").textContent = `El Merchant te invitó a ${result.campaign_name}. Confirmá con tu identidad Nostr para crear tu link único.`;
    page.querySelector("[data-invite-details]").hidden = false;
    page.querySelector("[data-invite-accept]").hidden = false;
    status.textContent = "Usá la identidad Affiliate que querés asociar a esta campaña.";
  } catch (error) {
    affiliateInvitationToken = null;
    status.textContent = error.message;
    status.classList.add("error");
    page.querySelector(".lede").textContent = "Esta invitación ya no está disponible.";
  }
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
  const invite = event.target.closest("[data-invite-accept]");
  if (invite) {
    const status = document.querySelector("[data-invite-status]");
    if (!window.nostr) {
      status.textContent = "Necesitás una extensión Nostr compatible, como Alby, para aceptar.";
      status.classList.add("error");
      return;
    }
    invite.disabled = true;
    status.classList.remove("error");
    status.textContent = "Confirmá la aceptación en tu extensión Nostr…";
    try {
      const token = affiliateInvitationToken;
      if (!token) throw new Error("La invitación no está disponible");
      const eventToSign = {
        kind: 22242,
        created_at: Math.floor(Date.now() / 1000),
        tags: [
          ["challenge", token],
          ["relay", window.location.origin],
          ["role", "affiliate_invite"]
        ],
        content: ""
      };
      const signedEvent = await window.nostr.signEvent(eventToSign);
      status.textContent = "Creando tu enrollment y link único…";
      const result = await jsonFetch("/invite/accept", {
        method: "POST", body: JSON.stringify({token, event: signedEvent})
      });
      status.textContent = "Invitación aceptada. Abriendo tu workspace…";
      window.location.assign(result.redirect);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
      invite.disabled = false;
    }
    return;
  }
  const useInvoiceHash = event.target.closest("[data-use-invoice-hash]");
  if (useInvoiceHash) {
    const form = useInvoiceHash.closest("[data-manual-payout]");
    const status = form.querySelector("[data-invoice-status]");
    const paymentHash = form.dataset.preparedPaymentHash || "";
    const expiresAt = Date.parse(form.dataset.preparedExpiresAt || "");
    if (!/^[0-9a-f]{64}$/.test(paymentHash) || !Number.isFinite(expiresAt)) {
      status.textContent = "Generá nuevamente el invoice antes de registrar el pago.";
      status.classList.add("error");
      return;
    }
    if (Date.now() >= expiresAt) {
      status.textContent = "Este invoice expiró. Generá uno nuevo antes de pagar.";
      status.classList.add("error");
      return;
    }
    form.querySelector("[name='evidence_type']").value = "payment_hash";
    form.querySelector("[name='evidence']").value = paymentHash;
    status.classList.remove("error");
    status.textContent = "Hash del invoice cargado. Confirmá abajo únicamente si tu wallet mostró el pago exitoso.";
    form.querySelector("[name='confirmed']").focus();
    return;
  }
  const prepareInvoice = event.target.closest("[data-prepare-invoice]");
  if (prepareInvoice) {
    const form = prepareInvoice.closest("[data-manual-payout]");
    const panel = form.querySelector("[data-invoice-panel]");
    const status = form.querySelector("[data-invoice-status]");
    prepareInvoice.disabled = true;
    status.classList.remove("error");
    status.textContent = "Resolviendo Lightning Address y generando invoice…";
    clearPreparedInvoice(form);
    try {
      const result = await jsonFetch(`/app/merchant/payouts/${encodeURIComponent(form.dataset.manualPayout)}/prepare-invoice`, {
        method: "POST", body: "{}"
      });
      const expiresAt = Date.parse(result.expires_at);
      if (!/^lnbc[0-9a-z]+$/i.test(result.invoice) || !/^data:image\/svg\+xml;base64,/.test(result.qr_data_uri) || !/^[0-9a-f]{64}$/.test(result.payment_hash) || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
        throw new Error("El servidor devolvió un invoice inválido");
      }
      panel.querySelector("[data-invoice-qr]").src = result.qr_data_uri;
      panel.querySelector("[data-invoice-amount]").textContent = String(result.amount_sats);
      panel.querySelector("[data-invoice-destination]").textContent = result.lightning_address;
      panel.querySelector("[data-invoice-text]").textContent = result.invoice;
      const copyInvoice = panel.querySelector("[data-invoice-copy]");
      copyInvoice.dataset.copy = result.invoice;
      form.dataset.preparedPaymentHash = result.payment_hash;
      form.dataset.preparedExpiresAt = result.expires_at;
      panel.querySelector("[data-invoice-expiry]").textContent = `Expira: ${new Date(expiresAt).toLocaleString()}`;
      panel.hidden = false;
      prepareInvoice.textContent = "Regenerar invoice y QR";
      status.textContent = `Invoice listo por ${result.amount_sats} sats. Generarlo no realiza el pago.`;
    } catch (error) {
      clearPreparedInvoice(form);
      status.textContent = error.message;
      status.classList.add("error");
    } finally {
      prepareInvoice.disabled = false;
    }
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
  const lightningForm = event.target.closest("[data-affiliate-lightning-address]");
  if (lightningForm) {
    event.preventDefault();
    const status = lightningForm.querySelector("[data-lightning-status]");
    const button = lightningForm.querySelector("button[type='submit']");
    button.disabled = true;
    status.classList.remove("error");
    status.textContent = "Guardando destino…";
    try {
      const result = await jsonFetch("/app/affiliate/lightning-address", {
        method: "PUT", body: JSON.stringify({lightning_address: String(new FormData(lightningForm).get("lightning_address") || "").trim()})
      });
      status.textContent = `Destino guardado. ${result.updated_payouts} pago(s) pendiente(s) actualizado(s).`;
    } catch (error) {
      status.textContent = error.message; status.classList.add("error");
    } finally { button.disabled = false; }
    return;
  }

  const payoutForm = event.target.closest("[data-manual-payout]");
  if (payoutForm) {
    event.preventDefault();
    const status = payoutForm.querySelector("[data-manual-status]");
    const button = payoutForm.querySelector("button[type='submit']");
    const fields = new FormData(payoutForm);
    let evidence = String(fields.get("evidence") || "").trim().toLowerCase();
    button.disabled = true; status.classList.remove("error");
    try {
      if (!/^[0-9a-f]{64}$/.test(evidence)) throw new Error("Ingresá exactamente 64 caracteres hexadecimales.");
      if (fields.get("evidence_type") === "preimage") {
        const bytes = new Uint8Array(evidence.match(/.{2}/g).map(byte => parseInt(byte, 16)));
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        evidence = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      }
      payoutForm.querySelector("[name='evidence']").value = "";
      status.textContent = "Registrando atestación y publicando prueba…";
      await jsonFetch(`/app/merchant/payouts/${encodeURIComponent(payoutForm.dataset.manualPayout)}/manual-settlement`, {
        method: "POST", body: JSON.stringify({payment_hash: evidence})
      });
      status.textContent = "Pago registrado."; window.location.reload();
    } catch (error) {
      status.textContent = error.message; status.classList.add("error"); button.disabled = false;
    }
    return;
  }

  const form = event.target.closest("[data-merchant-invitation]");
  if (!form) return;
  event.preventDefault();
  const status = form.querySelector("[data-invitation-status]");
  const resultBox = form.querySelector("[data-invitation-result]");
  const link = form.querySelector("[data-invitation-link]");
  const copy = form.querySelector("[data-copy]");
  const button = form.querySelector("button[type='submit']");
  const fields = new FormData(form);
  const payload = {
    campaign_id: String(fields.get("campaign_id") || "").trim(),
    expires_days: Number(fields.get("expires_days") || 7)
  };
  button.disabled = true;
  status.classList.remove("error");
  status.textContent = "Generando enlace privado de un solo uso…";
  resultBox.hidden = true;
  try {
    const result = await jsonFetch("/app/merchant/invitations", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    const safeUrl = new URL(result.invite_url, window.location.origin);
    if (safeUrl.origin !== window.location.origin) throw new Error("El servidor devolvió un enlace inválido");
    link.href = safeUrl.href;
    link.textContent = safeUrl.href;
    copy.dataset.copy = safeUrl.href;
    resultBox.hidden = false;
    const expiresAt = new Date(result.expires_at).toLocaleString();
    status.textContent = `Invitación lista para ${result.campaign_name}. Expira: ${expiresAt}.`;
  } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
  } finally {
    button.disabled = false;
  }
});

document.addEventListener("DOMContentLoaded", resolveAffiliateInvitation);
