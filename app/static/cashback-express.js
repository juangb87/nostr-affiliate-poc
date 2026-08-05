(() => {
  const selector = document.getElementById('cashback-language');
  const pageLanguage = () => document.documentElement.lang === 'en' ? 'en' : 'es';
  const tokenPattern = /^[A-Za-z0-9_-]{40,200}$/;
  const storageKey = (code) => `meerat_cashback_status:${code}`;
  let rerender = null;

  const readTokenHistory = (code) => {
    try {
      const raw = window.localStorage.getItem(storageKey(code));
      if (!raw) return [];
      if (tokenPattern.test(raw)) return [raw]; // Upgrade the original single-token format.
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.filter((token, index) => tokenPattern.test(token) && parsed.indexOf(token) === index).slice(0, 10);
    } catch (_) {
      return [];
    }
  };

  const rememberToken = (code, token) => {
    if (!tokenPattern.test(token)) return;
    try {
      const history = [token, ...readTokenHistory(code).filter((item) => item !== token)].slice(0, 10);
      window.localStorage.setItem(storageKey(code), JSON.stringify(history));
    } catch (_) {
      // The campaign-scoped HttpOnly cookie remains the same-device fallback.
    }
  };

  const setLanguage = (language) => {
    const lang = language === 'en' ? 'en' : 'es';
    document.documentElement.lang = lang;
    if (selector) selector.value = lang;
    document.cookie = `meerat_lang=${lang}; path=/; max-age=31536000; SameSite=Lax`;
    if (rerender) rerender();
  };
  if (selector) {
    selector.addEventListener('change', () => setLanguage(selector.value));
    setLanguage(selector.value || 'es');
  }

  const claimForm = document.getElementById('cashback-claim');
  if (claimForm) {
    const input = document.getElementById('lightning-address');
    const status = document.getElementById('claim-status');
    claimForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = claimForm.querySelector('button');
      button.disabled = true;
      try {
        const claimUrl = new URL(claimForm.action, window.location.href);
        claimUrl.searchParams.set('response', 'json');
        const response = await fetch(claimUrl, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({lightning_address: input.value}),
          credentials: 'same-origin', referrerPolicy: 'no-referrer'
        });
        if (!response.ok) throw new Error('invalid');
        const result = await response.json();
        if (!result.redirect_url || !tokenPattern.test(result.status_token) || !result.status_path) {
          throw new Error('invalid');
        }
        const code = result.status_path.split('/').filter(Boolean)[1];
        rememberToken(code, result.status_token);
        status.textContent = pageLanguage() === 'en'
          ? 'Cashback activated. Taking you to the store…'
          : 'Cashback activado. Te llevamos a la tienda…';
        window.location.assign(result.redirect_url);
      } catch (_) {
        status.textContent = pageLanguage() === 'en'
          ? 'Check your Lightning Address and try again.'
          : 'Revisá tu Dirección Lightning e intentá nuevamente.';
        button.disabled = false;
      }
    });
  }

  const statusCard = document.querySelector('[data-status-endpoint]');
  if (!statusCard) return;

  const endpoint = statusCard.dataset.statusEndpoint;
  const code = endpoint.split('/').filter(Boolean)[1];
  const loading = document.getElementById('status-loading');
  const resultPanel = document.getElementById('status-result');
  const emptyPanel = document.getElementById('status-empty');
  const lookupForm = document.getElementById('status-lookup');
  const tokenInput = document.getElementById('status-token');
  const error = document.getElementById('status-error');
  const copyButton = document.getElementById('copy-status-link');
  const copyFeedback = document.getElementById('copy-status-feedback');
  const historyPanel = document.getElementById('status-history');
  let activeToken = '';
  let currentResult = null;
  let lookupFailed = false;
  let copied = false;
  let lookupGeneration = 0;

  const messages = {
    es: {
      tracking: ['Seguimiento activo', 'Registramos tu cashback. Todavía estamos esperando una compra pagada asociada.'],
      confirmed: ['Compra confirmada', 'La tienda confirmó la compra y estamos preparando la recompensa.'],
      pending: ['Cashback pendiente', 'La compra fue confirmada y el comerciante todavía debe completar el pago.'],
      paid: ['Cashback pagado', 'El comerciante registró el pago de tu recompensa.'],
      expired: ['Seguimiento expirado', 'La ventana de atribución terminó sin una compra confirmada.'],
      icons: {tracking: '✓', confirmed: '✓', pending: '₿', paid: '✓', expired: '–'},
      noReward: 'Pendiente de confirmación',
      sats: (value) => `${value} sats`,
      paidDate: 'Pagado',
      confirmedDate: 'Compra confirmada',
      windowDate: 'Ventana hasta',
      evidence: (hash) => `Pago informado por el comerciante${hash ? ` · hash ${hash}` : ''}.`,
      unavailable: 'No encontramos un seguimiento con ese código privado.',
      copied: 'Enlace privado copiado.',
      history: 'Otros seguimientos guardados',
      historyItem: (index) => `Seguimiento ${index + 1}`
    },
    en: {
      tracking: ['Tracking active', 'We recorded your cashback. We are still waiting for a matching paid purchase.'],
      confirmed: ['Purchase confirmed', 'The store confirmed the purchase and the reward is being prepared.'],
      pending: ['Cashback pending', 'The purchase was confirmed and the merchant still needs to complete payment.'],
      paid: ['Cashback paid', 'The merchant recorded payment of your reward.'],
      expired: ['Tracking expired', 'The attribution window ended without a confirmed purchase.'],
      icons: {tracking: '✓', confirmed: '✓', pending: '₿', paid: '✓', expired: '–'},
      noReward: 'Awaiting confirmation',
      sats: (value) => `${value} sats`,
      paidDate: 'Paid',
      confirmedDate: 'Purchase confirmed',
      windowDate: 'Window until',
      evidence: (hash) => `Payment reported by the merchant${hash ? ` · hash ${hash}` : ''}.`,
      unavailable: 'We could not find tracking for that private code.',
      copied: 'Private link copied.',
      history: 'Other saved tracking',
      historyItem: (index) => `Tracking ${index + 1}`
    }
  };

  const formatDate = (value) => {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat(pageLanguage() === 'en' ? 'en-US' : 'es-AR', {
      dateStyle: 'medium', timeStyle: 'short'
    }).format(date);
  };

  const setDateLabel = (key) => {
    const target = document.getElementById('status-date-label');
    target.querySelector('[lang="es"]').textContent = messages.es[key];
    target.querySelector('[lang="en"]').textContent = messages.en[key];
  };

  const renderHistory = () => {
    const tokens = readTokenHistory(code);
    historyPanel.replaceChildren();
    historyPanel.hidden = tokens.length < 2;
    if (tokens.length < 2) return;
    const label = document.createElement('strong');
    label.textContent = messages[pageLanguage()].history;
    historyPanel.append(label);
    const list = document.createElement('div');
    tokens.forEach((token, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'history-button';
      button.textContent = messages[pageLanguage()].historyItem(index);
      button.disabled = token === activeToken;
      button.addEventListener('click', () => loadCandidates([token], false, true));
      list.append(button);
    });
    historyPanel.append(list);
  };

  const render = () => {
    const copy = messages[pageLanguage()];
    error.textContent = lookupFailed ? copy.unavailable : '';
    copyFeedback.textContent = copied ? copy.copied : '';
    if (!currentResult) return;
    const state = currentResult.status;
    const stateCopy = copy[state] || copy.tracking;
    resultPanel.dataset.state = state;
    document.getElementById('status-badge').textContent = copy.icons[state] || '•';
    document.getElementById('status-title').textContent = stateCopy[0];
    document.getElementById('status-copy').textContent = stateCopy[1];
    document.getElementById('status-address').textContent = currentResult.lightning_address_masked;
    document.getElementById('status-reward').textContent = currentResult.reward_sats == null
      ? copy.noReward : copy.sats(currentResult.reward_sats);
    document.getElementById('status-created').textContent = formatDate(currentResult.created_at);

    let dateValue = currentResult.expires_at;
    let dateKey = 'windowDate';
    if (currentResult.paid_at) {
      dateValue = currentResult.paid_at;
      dateKey = 'paidDate';
    } else if (currentResult.purchase_confirmed_at) {
      dateValue = currentResult.purchase_confirmed_at;
      dateKey = 'confirmedDate';
    }
    setDateLabel(dateKey);
    document.getElementById('status-date').textContent = formatDate(dateValue);

    const evidence = document.getElementById('status-evidence');
    if (currentResult.payment_evidence === 'merchant_attested') {
      evidence.textContent = copy.evidence(currentResult.payment_hash_short);
      evidence.hidden = false;
    } else {
      evidence.hidden = true;
    }
    copyButton.hidden = !activeToken;
    renderHistory();
  };
  rerender = render;

  const fetchStatus = async (token) => {
    const response = await fetch(endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(token ? {token} : {}),
      credentials: 'same-origin', referrerPolicy: 'no-referrer'
    });
    if (!response.ok) return null;
    return response.json();
  };

  const loadCandidates = async (tokens, allowCookieFallback, showError) => {
    const generation = ++lookupGeneration;
    loading.hidden = false;
    resultPanel.hidden = true;
    emptyPanel.hidden = true;
    lookupFailed = false;
    copied = false;
    const candidates = tokens.filter((token, index) => tokenPattern.test(token) && tokens.indexOf(token) === index);
    if (allowCookieFallback) candidates.push('');
    for (const token of candidates) {
      try {
        const found = await fetchStatus(token);
        if (generation !== lookupGeneration) return false;
        if (!found) continue;
        currentResult = found;
        activeToken = token;
        if (token) rememberToken(code, token); // Persist only after server verification.
        render();
        resultPanel.hidden = false;
        loading.hidden = true;
        return true;
      } catch (_) {
        if (generation !== lookupGeneration) return false;
        // Try the next locally-held capability or the HttpOnly cookie.
      }
    }
    if (generation !== lookupGeneration) return false;
    currentResult = null;
    activeToken = '';
    lookupFailed = showError;
    render();
    emptyPanel.hidden = false;
    loading.hidden = true;
    return false;
  };

  const fragment = window.location.hash.replace(/^#/, '').trim();
  if (fragment) window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  const initialTokens = [];
  if (tokenPattern.test(fragment)) initialTokens.push(fragment);
  initialTokens.push(...readTokenHistory(code));
  loadCandidates(initialTokens, true, false);

  lookupForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const token = tokenInput.value.trim();
    if (!tokenPattern.test(token)) {
      lookupGeneration += 1;
      loading.hidden = true;
      lookupFailed = true;
      render();
      return;
    }
    loadCandidates([token], false, true);
  });

  copyButton.addEventListener('click', async () => {
    if (!activeToken) return;
    const privateUrl = `${window.location.origin}${endpoint}#${activeToken}`;
    try {
      await navigator.clipboard.writeText(privateUrl);
      copied = true;
      render();
      window.setTimeout(() => { copied = false; render(); }, 1600);
    } catch (_) {
      tokenInput.value = activeToken;
      emptyPanel.hidden = false;
      tokenInput.focus();
      tokenInput.select();
    }
  });
})();
