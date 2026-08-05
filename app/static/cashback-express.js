(() => {
  const selector = document.getElementById('cashback-language');
  const form = document.getElementById('cashback-claim');
  const input = document.getElementById('lightning-address');
  const status = document.getElementById('claim-status');
  const setLanguage = (language) => {
    const lang = language === 'en' ? 'en' : 'es';
    document.documentElement.lang = lang;
    selector.value = lang;
    document.cookie = `meerat_lang=${lang}; path=/; max-age=31536000; SameSite=Lax`;
  };
  selector.addEventListener('change', () => setLanguage(selector.value));
  setLanguage(selector.value || 'es');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const claimUrl = new URL(form.action, window.location.href);
      claimUrl.searchParams.set('response', 'json');
      const response = await fetch(claimUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lightning_address: input.value}),
        credentials: 'same-origin', referrerPolicy: 'no-referrer'
      });
      if (!response.ok) throw new Error('invalid');
      const result = await response.json();
      if (!result.redirect_url) throw new Error('invalid');
      window.location.assign(result.redirect_url);
    } catch (_) {
      status.textContent = document.documentElement.lang === 'en' ? 'Check your Lightning Address and try again.' : 'Revisá tu Dirección Lightning e intentá nuevamente.';
      button.disabled = false;
    }
  });
})();
