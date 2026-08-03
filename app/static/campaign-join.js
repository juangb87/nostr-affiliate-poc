(() => {
  const page = document.querySelector("[data-campaign-join]");
  if (!page) return;
  const status = document.querySelector("[data-join-status]");
  const buttons = [...document.querySelectorAll("[data-join-method]")];
  const campaignId = page.dataset.campaignId;
  const mode = page.dataset.enrollmentMode;
  const message = (es, en) => document.documentElement.dataset.lang === "en" ? en : es;
  const signerError = (error) => {
    const raw = readableError(error);
    if (document.documentElement.dataset.lang !== "en") return raw;
    const translations = new Map([
      ["No encontramos una extensión Nostr. Usá una app Nostr o el QR para continuar.", "No Nostr extension was found. Use a Nostr app or the QR code to continue."],
      ["No se pudo cargar la conexión con la app Nostr. Recargá la página e intentá nuevamente.", "The Nostr app connection could not be loaded. Reload the page and try again."],
      ["No se pudo cargar la conexión con la app Nostr.", "The Nostr app connection could not be loaded."],
      ["El método de firma Nostr no es válido.", "The Nostr signing method is invalid."],
      ["NostrKey no devolvió un evento firmado. Verificá que esté desbloqueado y aprobá la solicitud de firma.", "Your Nostr signer did not return a signed event. Make sure it is unlocked and approve the signing request."]
    ]);
    return translations.get(raw) || "The enrollment could not be completed. Check your Nostr signer and try again.";
  };
  const setStatus = (es, en, error = false) => {
    status.textContent = message(es, en);
    status.classList.toggle("error", error);
  };

  async function join(method) {
    buttons.forEach(button => { button.disabled = true; });
    setStatus("Preparando una solicitud segura…", "Preparing a secure request…");
    try {
      const challenge = await jsonFetch(`/campaigns/${encodeURIComponent(campaignId)}/join/challenge`, {method: "POST", body: "{}"});
      setStatus("Confirmá la firma en tu signer Nostr…", "Confirm the signature in your Nostr signer…");
      const unsignedEvent = {
        kind: Number(challenge.kind),
        created_at: Math.floor(Date.now() / 1000),
        tags: [["challenge", challenge.challenge], ["role", challenge.role], ["relay", challenge.relay]],
        content: ""
      };
      const signedEvent = await signWithNostr(unsignedEvent, method);
      const result = await jsonFetch(`/campaigns/${encodeURIComponent(campaignId)}/join`, {method: "POST", body: JSON.stringify({event: signedEvent})});
      if (result.status === "pending" || mode === "approval") {
        setStatus("Solicitud enviada. El Merchant debe aprobarla antes de que tu enlace quede activo.", "Request sent. The Merchant must approve it before your link becomes active.");
        buttons.forEach(button => { button.hidden = true; });
        return;
      }
      setStatus("Inscripción creada. Ahora configurá dónde recibir tus sats.", "Enrollment created. Now choose where to receive your sats.");
      window.setTimeout(() => window.location.assign(result.redirect || "/app/affiliate?view=links"), 900);
    } catch (error) {
      const localizedError = signerError(error);
      setStatus(localizedError, localizedError, true);
      buttons.forEach(button => { button.disabled = false; });
    }
  }

  buttons.forEach(button => button.addEventListener("click", () => join(button.dataset.joinMethod || "auto")));
})();
