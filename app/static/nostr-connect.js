import { BunkerSigner, createNostrConnectURI } from "./vendor/nostr-tools-nip46-2.24.1.mjs";
import { generateSecretKey, getPublicKey } from "./vendor/nostr-tools-pure-2.24.1.mjs";

const COPY = {
  es: {
    title: "Conectá tu app Nostr",
    intro: "Abrí tu signer en este teléfono o escaneá el QR desde otro dispositivo.",
    open: "Abrir app Nostr",
    copy: "Copiar conexión",
    copied: "Conexión copiada",
    cancel: "Cancelar",
    waiting: "Esperando aprobación en tu app Nostr…",
    connected: "Identidad conectada. Confirmá la firma…",
    auth: "Tu signer necesita una autorización adicional.",
    authOpen: "Abrir autorización",
    unavailable: "No se pudo iniciar la conexión Nostr.",
    connectFailed: "No se pudo conectar con la app Nostr. Volvé a abrir Primal y aprobá la conexión.",
    shareIdentityFailed: "La app Nostr se conectó, pero no pudo compartir la identidad seleccionada.",
    signingFailed: "La app Nostr no pudo firmar con la cuenta elegida. Verificá que Primal tenga la clave de esa identidad y no sea una cuenta de solo lectura.",
    identityMismatch: "La app Nostr conectó una identidad, pero firmó con otra. Seleccioná la misma cuenta en Primal e intentá nuevamente.",
    cancelled: "Cancelaste la conexión Nostr.",
    timeout: "La conexión expiró. Intentá nuevamente.",
    qrAlt: "QR temporal para conectar una app Nostr",
  },
  en: {
    title: "Connect your Nostr app",
    intro: "Open your signer on this phone or scan the QR from another device.",
    open: "Open Nostr app",
    copy: "Copy connection",
    copied: "Connection copied",
    cancel: "Cancel",
    waiting: "Waiting for approval in your Nostr app…",
    connected: "Identity connected. Confirm the signature…",
    auth: "Your signer needs additional authorization.",
    authOpen: "Open authorization",
    unavailable: "Could not start the Nostr connection.",
    connectFailed: "Could not connect to the Nostr app. Reopen Primal and approve the connection.",
    shareIdentityFailed: "The Nostr app connected but could not share the selected identity.",
    signingFailed: "The Nostr app could not sign with the selected account. Check that Primal holds the key for this identity and that it is not a read-only account.",
    identityMismatch: "The Nostr app connected one identity but signed with another. Select the same account in Primal and try again.",
    cancelled: "You cancelled the Nostr connection.",
    timeout: "The connection expired. Try again.",
    qrAlt: "Temporary QR code to connect a Nostr app",
  },
};

let activeAttempt = null;
let dialog = null;

function languageCopy() {
  return COPY[document.documentElement.lang === "en" ? "en" : "es"];
}

function readConfig() {
  const node = document.getElementById("nostr-connect-config");
  if (!node) throw new Error(languageCopy().unavailable);
  let config;
  try {
    config = JSON.parse(node.textContent || "{}");
  } catch (_) {
    throw new Error(languageCopy().unavailable);
  }
  const relays = Array.isArray(config.relays)
    ? config.relays.filter((relay) => typeof relay === "string" && /^wss:\/\/[^\s]+$/i.test(relay)).slice(0, 3)
    : [];
  if (!relays.length) throw new Error(languageCopy().unavailable);
  const timeoutMs = Math.max(30_000, Math.min(300_000, Number(config.timeout_ms) || 180_000));
  return { relays, timeoutMs };
}

function randomHex(bytes = 32) {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return Array.from(value, (item) => item.toString(16).padStart(2, "0")).join("");
}

function ensureDialog() {
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.className = "nostr-connect-dialog";
  dialog.setAttribute("aria-labelledby", "nostr-connect-title");
  dialog.innerHTML = `
    <div class="nostr-connect-card">
      <button type="button" class="nostr-connect-close" data-nostr-connect-cancel aria-label="Cerrar">×</button>
      <p class="eyebrow">Nostr Connect · NIP-46</p>
      <h2 id="nostr-connect-title" data-nostr-connect-title></h2>
      <p class="nostr-connect-intro" data-nostr-connect-intro></p>
      <div class="nostr-connect-qr-frame"><img data-nostr-connect-qr width="264" height="264"></div>
      <div class="nostr-connect-actions">
        <a class="button" data-nostr-connect-open></a>
        <button class="button secondary" type="button" data-nostr-connect-copy></button>
      </div>
      <a class="nostr-connect-auth" data-nostr-connect-auth hidden rel="noopener noreferrer"></a>
      <p class="login-status" data-nostr-connect-status aria-live="polite"></p>
      <button class="button ghost" type="button" data-nostr-connect-cancel></button>
    </div>`;
  document.body.appendChild(dialog);
  dialog.querySelectorAll("[data-nostr-connect-cancel]").forEach((button) => {
    button.addEventListener("click", () => cancelAttempt("user"));
  });
  dialog.querySelector("[data-nostr-connect-copy]").addEventListener("click", async (event) => {
    if (!activeAttempt?.uri) return;
    const copy = languageCopy();
    try {
      await navigator.clipboard.writeText(activeAttempt.uri);
      event.currentTarget.textContent = copy.copied;
      setTimeout(() => {
        if (event.currentTarget.isConnected) event.currentTarget.textContent = copy.copy;
      }, 1600);
    } catch (_) {
      event.currentTarget.textContent = copy.copy;
    }
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelAttempt("user");
  });
  return dialog;
}

function renderAttempt(uri) {
  const copy = languageCopy();
  const panel = ensureDialog();
  panel.querySelector("[data-nostr-connect-title]").textContent = copy.title;
  panel.querySelector("[data-nostr-connect-intro]").textContent = copy.intro;
  panel.querySelector("[data-nostr-connect-status]").textContent = copy.waiting;
  panel.querySelector("[data-nostr-connect-copy]").textContent = copy.copy;
  panel.querySelector("[data-nostr-connect-cancel]:last-child").textContent = copy.cancel;
  const open = panel.querySelector("[data-nostr-connect-open]");
  open.textContent = copy.open;
  open.href = uri;
  const qr = window.qrcode?.(0, "M");
  if (!qr) throw new Error(copy.unavailable);
  qr.addData(uri, "Byte");
  qr.make();
  const image = panel.querySelector("[data-nostr-connect-qr]");
  image.alt = copy.qrAlt;
  image.src = qr.createDataURL(6, 12);
  const auth = panel.querySelector("[data-nostr-connect-auth]");
  auth.hidden = true;
  auth.removeAttribute("href");
  if (typeof panel.showModal === "function") panel.showModal();
  else panel.setAttribute("open", "");
}

function showSignerAuthorization(rawUrl) {
  if (!activeAttempt) return;
  let url;
  try { url = new URL(rawUrl); } catch (_) { return; }
  if (url.protocol !== "https:") return;
  const copy = languageCopy();
  const auth = ensureDialog().querySelector("[data-nostr-connect-auth]");
  auth.href = url.toString();
  auth.textContent = copy.authOpen;
  auth.hidden = false;
  ensureDialog().querySelector("[data-nostr-connect-status]").textContent = copy.auth;
}

function clearDialog() {
  if (!dialog) return;
  const image = dialog.querySelector("[data-nostr-connect-qr]");
  image.removeAttribute("src");
  const open = dialog.querySelector("[data-nostr-connect-open]");
  open.removeAttribute("href");
  const auth = dialog.querySelector("[data-nostr-connect-auth]");
  auth.removeAttribute("href");
  auth.hidden = true;
  if (dialog.open && typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function cancelAttempt(reason = "cancelled") {
  const attempt = activeAttempt;
  if (!attempt) {
    clearDialog();
    return;
  }
  attempt.cancelReason = reason;
  attempt.controller.abort(reason);
  attempt.signer?.close().catch(() => {});
  clearDialog();
}

function raceWithAbort(promise, signal) {
  if (signal.aborted) return Promise.reject(new Error("Nostr connection aborted"));
  return new Promise((resolve, reject) => {
    const aborted = () => reject(new Error("Nostr connection aborted"));
    signal.addEventListener("abort", aborted, {once: true});
    Promise.resolve(promise).then(
      (value) => {
        signal.removeEventListener("abort", aborted);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener("abort", aborted);
        reject(error);
      },
    );
  });
}

async function signEvent(unsignedEvent) {
  cancelAttempt("replaced");
  const copy = languageCopy();
  const { relays, timeoutMs } = readConfig();
  const controller = new AbortController();
  const clientSecretKey = generateSecretKey();
  const clientPubkey = getPublicKey(clientSecretKey);
  const uri = createNostrConnectURI({
    clientPubkey,
    relays,
    secret: randomHex(),
    perms: ["get_public_key", "sign_event:22242"],
    name: "Meerat",
    url: window.location.origin,
  });
  const attempt = { controller, signer: null, uri, cancelReason: null };
  activeAttempt = attempt;
  let timer;
  try {
    renderAttempt(uri);
    timer = setTimeout(() => {
      if (activeAttempt === attempt) cancelAttempt("timeout");
    }, timeoutMs);
    let signer;
    try {
      signer = await raceWithAbort(
        BunkerSigner.fromURI(
          clientSecretKey,
          uri,
          {
            skipSwitchRelays: true,
            onauth: (url) => {
              if (activeAttempt === attempt) showSignerAuthorization(url);
            },
          },
          controller.signal,
        ),
        controller.signal,
      );
    } catch (error) {
      if (controller.signal.aborted) throw error;
      throw new Error(copy.connectFailed);
    }
    if (activeAttempt !== attempt) throw new Error(copy.cancelled);
    attempt.signer = signer;
    ensureDialog().querySelector("[data-nostr-connect-status]").textContent = copy.connected;
    let expectedPubkey;
    try {
      expectedPubkey = await raceWithAbort(signer.getPublicKey(), controller.signal);
    } catch (error) {
      if (controller.signal.aborted) throw error;
      throw new Error(copy.shareIdentityFailed);
    }
    let signed;
    try {
      signed = await raceWithAbort(signer.signEvent(unsignedEvent), controller.signal);
    } catch (error) {
      if (controller.signal.aborted) throw error;
      throw new Error(copy.signingFailed);
    }
    if (!signed || signed.pubkey !== expectedPubkey) throw new Error(copy.identityMismatch);
    return signed;
  } catch (error) {
    if (attempt.cancelReason === "timeout") throw new Error(copy.timeout);
    if (attempt.cancelReason) throw new Error(copy.cancelled);
    throw error instanceof Error ? error : new Error(copy.unavailable);
  } finally {
    clearTimeout(timer);
    attempt.uri = null;
    await attempt.signer?.close().catch(() => {});
    if (activeAttempt === attempt) {
      activeAttempt = null;
      clearDialog();
    }
    clientSecretKey.fill(0);
  }
}

window.MeeratNostrConnect = { signEvent, cancel: cancelAttempt };
window.dispatchEvent(new Event("meerat:nostr-connect-ready"));
