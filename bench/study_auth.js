const studyAuth = (() => {
  let refreshing = null;

  async function request(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      cache: 'no-store',
      ...(body === undefined ? {} : {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || 'The study server could not complete the request.');
      error.status = response.status;
      throw error;
    }
    return result;
  }

  async function api(path, body) {
    try {
      return await request(path, body);
    } catch (error) {
      if (error.status !== 401) throw error;
      refreshing ||= request('/api/study/refresh', {}).finally(() => { refreshing = null; });
      await refreshing;
      return request(path, body);
    }
  }

  function signIn(panel, title, onSignIn) {
    panel.innerHTML = `<div class="panel auth-card"><h2></h2>
      <form id="study-login">
        <label>Email<input id="study-email" type="email" autocomplete="username" required></label>
        <label>Password<input id="study-password" type="password" autocomplete="current-password" required></label>
        <button class="btn primary" type="submit">Sign in</button>
        <p class="err" role="alert"></p>
      </form></div>`;
    panel.querySelector('h2').textContent = title;
    const form = panel.querySelector('form');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button');
      const message = form.querySelector('[role="alert"]');
      button.disabled = true;
      message.textContent = '';
      try {
        const { member } = await request('/api/study/login', {
          email: form.querySelector('#study-email').value,
          password: form.querySelector('#study-password').value,
        });
        form.reset();
        await onSignIn(member);
      } catch (error) {
        message.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  }

  async function signOut() {
    await request('/api/study/logout', {});
    location.reload();
  }

  return { api, signIn, signOut };
})();
