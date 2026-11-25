(function () {
  'use strict';
  var btn = document.getElementById('go');
  var msg = document.getElementById('msg');
  if (!btn || !msg) return;

  function poll(sec) {
    setTimeout(function () {
      fetch('/api/signin/poll', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.state === 'signed-in') { location.reload(); return; }
          if (d.state === 'pending') { poll(sec); return; }
          msg.textContent = 'Sign-in ' + (d.state || 'failed');
        })
        .catch(function () { msg.textContent = 'Sign-in error'; });
    }, sec * 1000);
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    msg.textContent = 'Starting sign-in...';
    fetch('/api/signin', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.user_code) {
          msg.textContent = 'Sign-in unavailable';
          btn.disabled = false;
          return;
        }
        msg.textContent = 'Enter code ' + d.user_code + ' at ' + d.verification_uri;
        poll(Math.max(1, Number(d.interval) || 5));
      })
      .catch(function () {
        msg.textContent = 'Sign-in failed';
        btn.disabled = false;
      });
  });
})();
