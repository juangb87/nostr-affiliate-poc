const APP_I18N = (() => {
  const node = document.getElementById("app-i18n");
  if (!node) return {};
  try { return JSON.parse(node.textContent || "{}"); } catch (_) { return {}; }
})();

function tr(message) {
  if (typeof message !== "string" || !Object.keys(APP_I18N).length) return message;
  if (APP_I18N[message]) return APP_I18N[message];
  const patterns = [
    [/^(\d+) sats pagados$/, "$1 sats paid"],
    [/^(\d+) activos$/, "$1 active links"],
    [/^(\d+) activo$/, "$1 active link"],
    [/^(\d+) aprobadas$/, "$1 approved conversions"],
    [/^(\d+) aprobada$/, "$1 approved conversion"],
    [/^La solicitud falló \((\d+)\)$/, "The request failed ($1)"],
    [/^No se pudo cerrar sesión: (.+)$/, "Could not sign out: $1"],
    [/^Se requiere iniciar sesión con el rol (.+)\.$/, "$1 sign-in is required."],
    [/^Esta identidad Nostr no está autorizada para el rol (.+)\.$/, "This Nostr identity is not authorized for the $1 role."],
    [/^(.+) debe ser una URL válida\.$/, "$1 must be a valid URL."],
    [/^(.+) debe ser una URL (HTTPS|HTTP\(S\)) válida\.$/, "$1 must be a valid $2 URL."],
    [/^(.+) no debe contener credenciales\.$/, "$1 must not contain credentials."],
    [/^(.+) contiene un host no válido\.$/, "$1 contains an invalid host."],
    [/^El pago con estado (.+) no se puede liquidar manualmente\.$/, "A payout in $1 state cannot be settled manually."],
    [/^La inscripción del afiliado tiene el estado (.+)\.$/, "The affiliate enrollment is in $1 state."],
    [/^Invitación lista para (.+)\. Expira: (.+)\.$/, "Invitation ready for $1. Expires: $2."],
    [/^Expira: (.+)$/, "Expires: $1"],
    [/^Factura Lightning lista por ([\d.,]+) sats\. Generarlo no realiza el pago\.$/, "Lightning invoice for $1 sats is ready. Generating it does not make the payment."],
    [/^La solicitud ya tiene el estado (.+)\.$/, "The application is already in the $1 state."]
  ];
  for (const [pattern, replacement] of patterns) {
    if (pattern.test(message)) return message.replace(pattern, replacement);
  }
  return message;
}

const APP_LOCALE = document.documentElement.lang === "en" ? "en-US" : "es-AR";
const formatAppNumber = (value) => new Intl.NumberFormat(APP_LOCALE).format(Number(value));
const formatAppDateTime = (value) => new Intl.DateTimeFormat(APP_LOCALE, {
  dateStyle: "medium",
  timeStyle: "short"
}).format(new Date(value));

function translateUiNode(root) {
  if (!Object.keys(APP_I18N).length || !root) return;
  const translationHost = root.nodeType === Node.TEXT_NODE ? root.parentElement : root;
  if (translationHost?.closest?.("[data-i18n-ignore]")) return;
  if (root.nodeType === Node.TEXT_NODE) {
    const trimmed = root.nodeValue.trim();
    if (!trimmed) return;
    const translated = tr(trimmed);
    if (translated !== trimmed) root.nodeValue = root.nodeValue.replace(trimmed, translated);
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
  if (root.matches?.("script,style,code,pre,textarea")) return;
  for (const attr of ["aria-label", "placeholder", "title", "alt"]) {
    if (root.hasAttribute?.(attr)) root.setAttribute(attr, tr(root.getAttribute(attr)));
  }
  for (const child of root.childNodes || []) translateUiNode(child);
}

document.addEventListener("DOMContentLoaded", () => {
  translateUiNode(document.body);
  if (!Object.keys(APP_I18N).length) return;
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "characterData") translateUiNode(mutation.target);
      for (const node of mutation.addedNodes || []) translateUiNode(node);
    }
  }).observe(document.body, {subtree: true, childList: true, characterData: true});
});

async function jsonFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) {
    throw new Error(readableError(data.detail, `La solicitud falló (${response.status})`));
  }
  return data;
}

function readableError(error, fallback = "No se pudo completar la operación. Intentá nuevamente.") {
  const seen = new Set();
  function messageFrom(value, depth = 0) {
    if (value == null || depth > 3) return "";
    if (typeof value === "string") return value === "[object Object]" ? "" : value;
    if (typeof value !== "object") return "";
    if (seen.has(value)) return "";
    seen.add(value);
    for (const key of ["message", "error", "detail", "reason"]) {
      const message = messageFrom(value[key], depth + 1);
      if (message) return message;
    }
    try {
      const serialized = JSON.stringify(value);
      return serialized && serialized !== "{}" ? serialized : "";
    } catch (_) {
      return "";
    }
  }
  return tr(messageFrom(error) || fallback);
}

function requireSignedNostrEvent(result) {
  if (typeof result === "string") {
    try { result = JSON.parse(result); } catch (_) { result = null; }
  }
  if (result && typeof result === "object" && result.event) {
    result = result.event;
  }
  if (result && typeof result === "object" && (result.error || result.message)) {
    throw new Error(readableError(result));
  }
  const required = ["id", "pubkey", "sig", "kind", "created_at", "tags", "content"];
  if (!result || typeof result !== "object" || required.some(key => !(key in result))) {
    throw new Error("NostrKey no devolvió un evento firmado. Verificá que esté desbloqueado y aprobá la solicitud de firma.");
  }
  return result;
}

async function waitForNostrConnect() {
  if (window.MeeratNostrConnect) return window.MeeratNostrConnect;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("No se pudo cargar la conexión con la app Nostr. Recargá la página e intentá nuevamente.")), 5000);
    window.addEventListener("meerat:nostr-connect-ready", () => {
      clearTimeout(timer);
      resolve();
    }, {once: true});
  });
  if (!window.MeeratNostrConnect) throw new Error("No se pudo cargar la conexión con la app Nostr.");
  return window.MeeratNostrConnect;
}

async function signWithNostr(unsignedEvent, requestedMethod = "auto") {
  const method = requestedMethod === "auto"
    ? (typeof window.nostr?.signEvent === "function" ? "nip07" : "nip46")
    : requestedMethod;
  if (method === "nip07") {
    if (typeof window.nostr?.signEvent !== "function") {
      throw new Error("No encontramos una extensión Nostr. Usá una app Nostr o el QR para continuar.");
    }
    return requireSignedNostrEvent(await window.nostr.signEvent(unsignedEvent));
  }
  if (method === "nip46") {
    const connector = await waitForNostrConnect();
    return requireSignedNostrEvent(await connector.signEvent(unsignedEvent));
  }
  throw new Error("El método de firma Nostr no es válido.");
}

let affiliateInvitationToken = null;
let affiliateInvitationEventKind = null;

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
  const errorStatus = page.querySelector("[data-invite-error-status]");
  const fail = (message) => {
    affiliateInvitationToken = null;
    page.classList.remove("is-loading");
    page.classList.add("is-error");
    status.textContent = message;
    status.classList.add("error");
    if (errorStatus) errorStatus.textContent = message;
  };
  const setAllText = (selector, value) => {
    page.querySelectorAll(selector).forEach((element) => { element.textContent = value; });
  };
  const params = new URLSearchParams(window.location.hash.slice(1));
  const fragmentToken = params.get("token");
  const historyToken = window.history.state?.inviteToken;
  const token = fragmentToken || (typeof historyToken === "string" ? historyToken : null);
  if (fragmentToken) {
    window.history.replaceState({inviteToken: fragmentToken}, "", "/invite");
  }
  if (!token) {
    fail("El enlace no contiene una invitación válida. Pedile uno nuevo al comerciante.");
    return;
  }
  affiliateInvitationToken = token;
  try {
    const result = await jsonFetch("/invite/resolve", {
      method: "POST", body: JSON.stringify({token})
    });
    affiliateInvitationEventKind = Number(result.auth_event_kind);
    if (!Number.isInteger(affiliateInvitationEventKind)) {
      throw new Error("La invitación no tiene un tipo de firma válido");
    }
    const merchant = result.merchant || {};
    const campaign = result.campaign || {};
    const displayName = merchant.display_name || result.campaign_name || "Comercio";
    const initials = merchant.initials || "₿";
    setAllText("[data-invite-merchant-name]", displayName);
    setAllText("[data-invite-initials]", initials);
    setAllText("[data-invite-tagline]", merchant.tagline || "Comunidad, recomendaciones y sats");
    setAllText("[data-invite-eyebrow]", campaign.invite_eyebrow || "Programa de afiliados · Valor por valor");
    setAllText("[data-invite-headline]", campaign.invite_headline || `Recomendá ${displayName}. Ganá sats.`);
    setAllText("[data-invite-description]", campaign.invite_description || `Sumate al programa de afiliados de ${displayName}.`);
    setAllText("[data-invite-commission]", campaign.commission_percent || result.commission_percent);
    if (merchant.logo_url) {
      page.querySelectorAll("[data-invite-logo]").forEach((image) => {
        image.alt = `Logo de ${displayName}`;
        image.addEventListener("error", () => {
          image.hidden = true;
          page.querySelectorAll("[data-invite-initials]").forEach((mark) => { mark.hidden = false; });
        }, {once: true});
        image.src = merchant.logo_url;
        image.hidden = false;
      });
      page.querySelectorAll("[data-invite-initials]").forEach((mark) => { mark.hidden = true; });
    }
    document.title = `${displayName} · Programa de afiliados`;
    page.querySelector("[data-invite-accept]").hidden = false;
    page.classList.remove("is-loading");
    status.textContent = "Usá la identidad Nostr que querés asociar a este programa.";
  } catch (error) {
    fail(readableError(error));
  }
}

async function loginWithNostr(role, method) {
  const status = document.querySelector("[data-login-status]");
  const methodButtons = document.querySelectorAll("[data-login-method]");
  methodButtons.forEach((element) => { element.disabled = true; });
  status.classList.remove("error");
  status.textContent = tr("Preparando desafío seguro…");
  try {
    const challenge = await jsonFetch("/auth/nostr/challenge", {
      method: "POST", body: JSON.stringify({role})
    });
    status.textContent = tr(method === "nip07"
      ? "Confirmá la firma en tu extensión Nostr…"
      : "Conectá tu app Nostr y confirmá la firma…");
    const unsignedEvent = {
      kind: challenge.kind,
      created_at: Math.floor(Date.now() / 1000),
      tags: [
        ["challenge", challenge.challenge],
        ["relay", challenge.relay],
        ["role", challenge.role]
      ],
      content: ""
    };
    const event = await signWithNostr(unsignedEvent, method);
    status.textContent = tr("Validando identidad…");
    const result = await jsonFetch("/auth/nostr/verify", {
      method: "POST", body: JSON.stringify({event})
    });
    window.location.assign(result.redirect);
  } catch (error) {
    status.textContent = readableError(error);
    status.classList.add("error");
    methodButtons.forEach((element) => { element.disabled = false; });
  }
}

document.addEventListener("click", async (event) => {
  const roleChoice = event.target.closest("[data-login-role]");
  if (roleChoice) {
    document.querySelectorAll("[data-login-role]").forEach((element) => {
      const selected = element === roleChoice;
      element.classList.toggle("selected", selected);
      element.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    const status = document.querySelector("[data-login-status]");
    status?.classList.remove("error");
    if (status) status.textContent = tr("Ahora continuá con tu signer Nostr.");
    return;
  }
  const loginMethod = event.target.closest("[data-login-method]");
  if (loginMethod) {
    const selectedRole = document.querySelector("[data-login-role][aria-pressed='true']");
    const status = document.querySelector("[data-login-status]");
    if (!selectedRole) {
      status.textContent = tr("Elegí primero si entrás como comerciante o afiliado.");
      status.classList.add("error");
      document.querySelector("[data-login-role]")?.focus();
      return;
    }
    await loginWithNostr(selectedRole.dataset.loginRole, loginMethod.dataset.loginMethod);
    return;
  }
  const invite = event.target.closest("[data-invite-accept]");
  if (invite) {
    const status = document.querySelector("[data-invite-status]");
    invite.disabled = true;
    status.classList.remove("error");
    status.textContent = "Conectá tu app Nostr y confirmá la aceptación…";
    try {
      const token = affiliateInvitationToken;
      if (!token) throw new Error("La invitación no está disponible");
      const eventToSign = {
        kind: affiliateInvitationEventKind,
        created_at: Math.floor(Date.now() / 1000),
        tags: [
          ["challenge", token],
          ["relay", window.location.origin],
          ["role", "affiliate_invite"]
        ],
        content: ""
      };
      const signedEvent = await signWithNostr(eventToSign, "auto");
      status.textContent = "Creando tu inscripción y tu enlace único…";
      const result = await jsonFetch("/invite/accept", {
        method: "POST", body: JSON.stringify({token, event: signedEvent})
      });
      status.textContent = "Invitación aceptada. Abriendo tu espacio de trabajo…";
      window.history.replaceState(null, "", "/invite");
      window.location.assign(result.redirect);
    } catch (error) {
      status.textContent = readableError(error);
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
      status.textContent = "Generá nuevamente la factura Lightning antes de registrar el pago.";
      status.classList.add("error");
      return;
    }
    if (Date.now() >= expiresAt) {
      status.textContent = "Esta factura Lightning expiró. Generá una nueva antes de pagar.";
      status.classList.add("error");
      return;
    }
    form.querySelector("[name='evidence_type']").value = "payment_hash";
    form.querySelector("[name='evidence']").value = paymentHash;
    status.classList.remove("error");
    status.textContent = "Hash de la factura cargado. Confirmá abajo únicamente si tu billetera mostró que el pago fue exitoso.";
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
    status.textContent = "Resolviendo la dirección Lightning y generando la factura…";
    clearPreparedInvoice(form);
    try {
      const result = await jsonFetch(`/app/merchant/payouts/${encodeURIComponent(form.dataset.manualPayout)}/prepare-invoice`, {
        method: "POST", body: "{}"
      });
      const expiresAt = Date.parse(result.expires_at);
      if (!/^lnbc[0-9a-z]+$/i.test(result.invoice) || !/^data:image\/svg\+xml;base64,/.test(result.qr_data_uri) || !/^[0-9a-f]{64}$/.test(result.payment_hash) || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
        throw new Error("El servidor devolvió una factura Lightning inválida");
      }
      panel.querySelector("[data-invoice-qr]").src = result.qr_data_uri;
      panel.querySelector("[data-invoice-amount]").textContent = formatAppNumber(result.amount_sats);
      panel.querySelector("[data-invoice-destination]").textContent = result.lightning_address;
      panel.querySelector("[data-invoice-text]").textContent = result.invoice;
      const copyInvoice = panel.querySelector("[data-invoice-copy]");
      copyInvoice.dataset.copy = result.invoice;
      form.dataset.preparedPaymentHash = result.payment_hash;
      form.dataset.preparedExpiresAt = result.expires_at;
      panel.querySelector("[data-invoice-expiry]").textContent = tr(`Expira: ${formatAppDateTime(expiresAt)}`);
      panel.hidden = false;
      prepareInvoice.textContent = tr("Regenerar factura Lightning y QR");
      status.textContent = tr(`Factura Lightning lista por ${formatAppNumber(result.amount_sats)} sats. Generarlo no realiza el pago.`);
    } catch (error) {
      clearPreparedInvoice(form);
      status.textContent = readableError(error);
      status.classList.add("error");
    } finally {
      prepareInvoice.disabled = false;
    }
    return;
  }
  const copy = event.target.closest("[data-copy], [data-copy-target]");
  if (copy) {
    const original = copy.textContent;
    const globalStatus = document.querySelector("[data-global-status]");
    const target = copy.dataset.copyTarget ? document.querySelector(copy.dataset.copyTarget) : null;
    const copyValue = target ? target.textContent : copy.dataset.copy;
    try {
      if (!copyValue) throw new Error("No hay contenido para copiar");
      await navigator.clipboard.writeText(copyValue);
      copy.textContent = "Copiado";
      if (globalStatus) globalStatus.textContent = "Contenido copiado";
    } catch (_) {
      copy.textContent = "Copiá manualmente";
      if (globalStatus) globalStatus.textContent = "No se pudo copiar automáticamente";
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
      if (globalStatus) globalStatus.textContent = `No se pudo cerrar sesión: ${readableError(error)}`;
    }
  }
});

const setMerchantOnboardingStep = (form, requestedStep) => {
  const steps = [...form.querySelectorAll("[data-onboarding-step]")];
  const maximum = steps.length;
  const step = Math.max(1, Math.min(maximum, Number(requestedStep) || 1));
  steps.forEach((panel) => { panel.hidden = Number(panel.dataset.onboardingStep) !== step; });
  document.querySelectorAll("[data-onboarding-progress]").forEach((item) => {
    const itemStep = Number(item.dataset.onboardingProgress);
    item.classList.toggle("active", itemStep === step);
    item.classList.toggle("complete", itemStep < step);
    if (itemStep === step) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
  });
  form.dataset.currentStep = String(step);
  steps.find((panel) => Number(panel.dataset.onboardingStep) === step)?.querySelector("input:not([type='hidden']), select, textarea")?.focus({preventScroll: true});
};

document.addEventListener("click", (event) => {
  const control = event.target.closest("[data-wizard-next], [data-wizard-back]");
  if (!control) return;
  const form = control.closest("[data-merchant-onboarding-wizard]");
  if (!form) return;
  const current = Number(form.dataset.currentStep || 1);
  if (control.hasAttribute("data-wizard-next")) {
    const panel = form.querySelector(`[data-onboarding-step="${current}"]`);
    const fields = [...panel.querySelectorAll("input, select, textarea")];
    const invalid = fields.find((field) => !field.checkValidity());
    if (invalid) { invalid.reportValidity(); return; }
    setMerchantOnboardingStep(form, current + 1);
  } else {
    setMerchantOnboardingStep(form, current - 1);
  }
});

document.querySelectorAll("[data-merchant-onboarding-wizard]").forEach((form) => setMerchantOnboardingStep(form, 1));

document.addEventListener("submit", async (event) => {
  const modeForm = event.target.closest("[data-enrollment-mode]");
  if (modeForm) {
    event.preventDefault();
    const status = modeForm.querySelector("[data-mode-status]");
    const button = modeForm.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      await jsonFetch(`/app/merchant/campaigns/${encodeURIComponent(modeForm.dataset.enrollmentMode)}/enrollment-mode`, {method: "PUT", body: JSON.stringify({enrollment_mode: new FormData(modeForm).get("enrollment_mode")})});
      status.textContent = "Modo actualizado.";
      window.location.reload();
    } catch (error) { status.textContent = readableError(error); status.classList.add("error"); button.disabled = false; }
    return;
  }

  const decisionForm = event.target.closest("[data-enrollment-decision]");
  if (decisionForm) {
    event.preventDefault();
    const status = decisionForm.querySelector("[data-decision-status]");
    const buttons = [...decisionForm.querySelectorAll("button")];
    const decision = event.submitter?.value;
    buttons.forEach((button) => { button.disabled = true; });
    try {
      await jsonFetch(`/app/merchant/enrollments/${encodeURIComponent(decisionForm.dataset.enrollmentDecision)}/decision`, {method: "POST", body: JSON.stringify({status: decision})});
      status.textContent = decision === "approved" ? "Affiliate aprobado." : "Solicitud rechazada.";
      window.location.reload();
    } catch (error) { status.textContent = readableError(error); status.classList.add("error"); buttons.forEach((button) => { button.disabled = false; }); }
    return;
  }

  const bootstrapForm = event.target.closest("[data-merchant-bootstrap]");
  if (bootstrapForm) {
    event.preventDefault();
    const isOnboarding = bootstrapForm.hasAttribute("data-merchant-onboarding-wizard");
    if (isOnboarding) {
      const currentStep = Number(bootstrapForm.dataset.currentStep || 1);
      if (currentStep < 3) {
        const currentPanel = bootstrapForm.querySelector(`[data-onboarding-step="${currentStep}"]`);
        const controls = [...currentPanel.querySelectorAll("input, select, textarea")];
        if (!controls.every((control) => control.reportValidity())) return;
        setMerchantOnboardingStep(bootstrapForm, currentStep + 1);
        return;
      }
    }
    const status = bootstrapForm.querySelector("[data-bootstrap-status]");
    const button = bootstrapForm.querySelector("button[type='submit']");
    const fields = new FormData(bootstrapForm);
    const payload = {
      merchant_pubkey: String(fields.get("merchant_pubkey") || "").trim(),
      program_name: String(fields.get("program_name") || "").trim(),
      commission_percent: String(fields.get("commission_percent") || "").trim(),
      attribution_window_days: Number(fields.get("attribution_window_days") || 0),
      destination_url: String(fields.get("destination_url") || "").trim(),
      terms_url: String(fields.get("terms_url") || "").trim(),
      logo_url: String(fields.get("logo_url") || "").trim() || null,
      enrollment_mode: String(fields.get("enrollment_mode") || "private")
    };
    if (isOnboarding) {
      Object.assign(payload, {
        display_name: String(fields.get("display_name") || "").trim(),
        tagline: String(fields.get("tagline") || "").trim() || null,
        invite_eyebrow: String(fields.get("invite_eyebrow") || "").trim() || null,
        invite_headline: String(fields.get("invite_headline") || "").trim() || null,
        invite_description: String(fields.get("invite_description") || "").trim() || null
      });
    }
    button.disabled = true;
    status.classList.remove("error");
    status.textContent = "Creando programa y guardando su prueba Nostr…";
    try {
      if (isOnboarding) {
        status.textContent = "Guardando marca, programa e invitación…";
        await jsonFetch("/app/merchant/onboarding", {
          method: "POST", body: JSON.stringify(payload)
        });
        status.textContent = "Programa listo. Abriendo tu resumen…";
        window.location.assign("/app/merchant");
      } else {
        await jsonFetch("/app/merchant/bootstrap", {
          method: "POST", body: JSON.stringify(payload)
        });
        status.textContent = "Programa creado. Cargando condiciones…";
        window.location.reload();
      }
    } catch (error) {
      status.textContent = readableError(error);
      status.classList.add("error");
      button.disabled = false;
    }
    return;
  }

  const profileForm = event.target.closest("[data-merchant-profile]");
  if (profileForm) {
    event.preventDefault();
    const status = profileForm.querySelector("[data-profile-status]");
    const button = profileForm.querySelector("button[type='submit']");
    const fields = new FormData(profileForm);
    button.disabled = true;
    status.classList.remove("error");
    status.textContent = "Guardando identidad de marca…";
    try {
      await jsonFetch("/app/merchant/profile", {
        method: "PUT",
        body: JSON.stringify({
          merchant_pubkey: String(fields.get("merchant_pubkey") || "").trim(),
          display_name: String(fields.get("display_name") || "").trim() || null,
          tagline: String(fields.get("tagline") || "").trim() || null,
          logo_url: String(fields.get("logo_url") || "").trim() || null
        })
      });
      status.textContent = "Guardando el texto de la invitación…";
      await jsonFetch("/app/merchant/campaign-invite", {
        method: "PUT",
        body: JSON.stringify({
          campaign_id: String(fields.get("campaign_id") || "").trim(),
          invite_eyebrow: String(fields.get("invite_eyebrow") || "").trim() || null,
          invite_headline: String(fields.get("invite_headline") || "").trim() || null,
          invite_description: String(fields.get("invite_description") || "").trim() || null
        })
      });
      status.textContent = "Marca e invitación guardadas. Actualizando la vista…";
      window.location.reload();
    } catch (error) {
      status.textContent = readableError(error);
      status.classList.add("error");
      button.disabled = false;
    }
    return;
  }

  const lightningForm = event.target.closest("[data-affiliate-lightning-address]");
  if (lightningForm) {
    event.preventDefault();
    const status = lightningForm.querySelector("[data-lightning-status]");
    const button = lightningForm.querySelector("button[type='submit']");
    button.disabled = true;
    status.classList.remove("error");
    status.textContent = "Verificando la dirección Lightning…";
    try {
      const result = await jsonFetch("/app/affiliate/lightning-address", {
        method: "PUT", body: JSON.stringify({lightning_address: String(new FormData(lightningForm).get("lightning_address") || "").trim()})
      });
      status.textContent = `Destino verificado y guardado. ${result.updated_payouts} pago(s) pendiente(s) actualizado(s).`;
      if (lightningForm.dataset.onboarding === "true") {
        status.textContent = "Destino verificado. Preparando tu panel…";
        window.location.assign(result.redirect || "/app/affiliate");
      }
    } catch (error) {
      status.textContent = readableError(error); status.classList.add("error");
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
      if (!/^[0-9a-f]{64}$/.test(evidence)) {
        throw new Error("Ingresá el hash de pago Lightning: exactamente 64 caracteres hexadecimales, sin guiones. El ID UUID de Strike no es un hash de pago.");
      }
      if (fields.get("confirmed") !== "on") {
        throw new Error("Marcá la confirmación únicamente si tu billetera mostró que el pago fue exitoso.");
      }
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
      status.textContent = readableError(error); status.classList.add("error"); button.disabled = false;
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
    const inviteOrigin = new URL(form.dataset.inviteOrigin || window.location.origin, window.location.origin).origin;
    const allowedOrigin = safeUrl.origin === window.location.origin || safeUrl.origin === inviteOrigin;
    if (!allowedOrigin || safeUrl.pathname !== "/invite" || !safeUrl.hash.startsWith("#token=")) {
      throw new Error("El servidor devolvió un enlace inválido");
    }
    link.href = safeUrl.href;
    link.textContent = safeUrl.href;
    copy.dataset.copy = safeUrl.href;
    resultBox.hidden = false;
    const expiresAt = formatAppDateTime(result.expires_at);
    status.textContent = tr(`Invitación lista para ${result.campaign_name}. Expira: ${expiresAt}.`);
  } catch (error) {
    status.textContent = readableError(error);
    status.classList.add("error");
  } finally {
    button.disabled = false;
  }
});

document.addEventListener("DOMContentLoaded", resolveAffiliateInvitation);
