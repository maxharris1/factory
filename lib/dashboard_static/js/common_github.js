
  // The #563 GitHub sign-in every page with actions shares (ticket page, board,
  // settings): present only when the server answers /api/session. The session
  // is an HttpOnly cookie; the page only ever holds the CSRF value.
  var GH = {started: false, on: false, login: null, csrf: null, flow: null, note: '', panels: {},
            accountSignin: false};

  function ghFetch(path, method, body, csrf) {
    var headers = body === null ? {} : {'Content-Type': 'application/json'};
    if (csrf) headers['X-Factory-CSRF'] = GH.csrf || '';
    return fetch(path, {method: method, credentials: 'same-origin', cache: 'no-store', headers: headers,
                        body: body === null ? undefined : JSON.stringify(body)})
      .then(function (res) {
        return res.json().then(function (data) { return data; }, function () { return null; }).then(function (data) {
          data = data && typeof data === 'object' ? data : {};
          return {status: res.status, ok: res.ok, data: data, error: res.ok ? null : String(data.error || 'http-' + res.status)};
        });
      }, function () { return {status: 0, ok: false, data: {}, error: 'network'}; });
  }
  function ghPost(path, body, csrf) { return ghFetch(path, 'POST', body, csrf); }
  function ghGet(path) { return ghFetch(path, 'GET', null, true); }
  function ghSet(signedIn, data) {
    GH.login = signedIn ? String(data.login || '') : null;
    GH.csrf = signedIn ? String(data.csrf || '') : null;
    if (latest) render(latest);
    else if (GH.login) poll();
  }
  var SHA = /^[0-9a-f]{40}$/;
  function ghResult(out, res, text) {
    out.replaceChildren();
    var url = res.ok ? String(res.data.url || '') : '';
    if (/^https:\/\/github\.com\//.test(url)) {
      add(out, document.createTextNode(text + ' '));
      var a = add(out, link(null, url));
      a.setAttribute('rel', 'noopener noreferrer');
      a.setAttribute('target', '_blank');
      a.textContent = url;
    } else if (!res.ok && typeof res.data.github_status === 'number') {
      out.textContent = 'error: github ' + res.data.github_status +
        (typeof res.data.hint === 'string' ? ' (' + res.data.hint + ')' : '');
    } else out.textContent = res.ok ? text : 'error: ' + res.error;
  }
  function ghStart() {
    var request;
    try { request = fetch('/api/session', {cache: 'no-store', credentials: 'same-origin'}); } catch (err) { return; }
    request.then(function (res) {
      if (res.status !== 200) return null;
      return res.json();
    }).then(function (data) {
      if (!data || typeof data.signed_in !== 'boolean') return;
      GH.on = true;
      GH.accountSignin = data.account_signin === true;
      ghSet(data.signed_in, data);
    }, function () {});
  }
  function ghSignIn() {
    GH.note = '';
    ghPost('/api/signin', {}, false).then(function (res) {
      if (!res.ok) { GH.note = 'sign-in failed: ' + res.error; ghSet(false, {}); return; }
      var d = res.data;
      GH.flow = {code: String(d.user_code || ''), uri: String(d.verification_uri || ''),
                 interval: Math.max(1, Number(d.interval) || 5)};
      ghSet(false, {});
      setTimeout(ghPoll, GH.flow.interval * 1000);
    });
  }
  function ghPoll() {
    if (!GH.flow) return;
    ghPost('/api/signin/poll', {}, false).then(function (res) {
      var state = res.ok ? res.data.state : null;
      if (state === 'pending') { setTimeout(ghPoll, GH.flow.interval * 1000); return; }
      GH.flow = null;
      GH.note = state === 'signed-in' ? '' : 'sign-in ' + (state || 'failed: ' + res.error);
      ghSet(state === 'signed-in', res.data);
    });
  }
  function ghSignOut() {
    ghPost('/api/signout', {}, true).then(function () { GH.note = ''; ghSet(false, {}); });
  }
  function ghButton(parent, text, onClick, cls) {
    var b = add(parent, el('button', cls || null, text));
    b.setAttribute('type', 'button');
    b.addEventListener('click', onClick);
    return b;
  }
  function ghAuth(row) {
    row.replaceChildren();
    if (GH.login) {
      add(row, el('span', null, 'signed in as ' + GH.login));
      ghButton(row, 'Sign out', ghSignOut);
    } else if (GH.flow) {
      add(row, el('span', null, 'Code'));
      add(row, el('code', null, GH.flow.code));
      if (/^https:\/\/github\.com\//.test(GH.flow.uri)) {
        var a = add(row, link(null, GH.flow.uri));
        a.setAttribute('rel', 'noopener noreferrer');
        a.setAttribute('target', '_blank');
        a.textContent = GH.flow.uri;
      }
      add(row, el('span', null, 'Enter only this code on github.com.'));
    } else ghButton(row, 'Sign in with GitHub', ghSignIn);
    if (GH.note) add(row, el('span', 'warn', GH.note));
  }
