/* A GET never stops the application. Only the explicit close action starts shutdown. */
(() => {
  'use strict';
  const root = document.getElementById('shutdown');
  const ids = ['operations', 'service', 'ai', 'server'];
  const dialog = document.getElementById('error-dialog');
  const button = document.getElementById('shutdown-button');
  const labels = {operations:'Finish current file operations',service:'Stop scanning and monitoring',ai:'Finish this AI session',server:'Exit the local API'};
  const checks = new Map(ids.map(id => [id,{id,state:'pending',detail:root.querySelector(`[data-check="${id}"] .detail`).textContent}]));
  let token = '';
  let phase = 'idle';
  let supported = false;
  let supportKnown = false;
  let began = false;
  let requested = false;
  let canFinish = false;
  let finishing = false;
  let accepted = false;
  let timer = 0;
  let generation = 0;
  const requests = new Set();

  async function read(path, method='GET') {
    const controller = new AbortController();
    requests.add(controller);
    const deadline = setTimeout(() => controller.abort(), 8000);
    try {
      const options = {method,signal:controller.signal,cache:'no-store',credentials:'same-origin',redirect:'error'};
      if (method === 'POST') {
        options.headers = {'Content-Type':'application/json','X-Code-Steward-Action':'shutdown'};
        options.body = JSON.stringify({token});
      }
      const response = await fetch(path,options);
      let data;
      try { data = await response.json(); } catch { throw new Error('The local service returned invalid data.'); }
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `The local service returned HTTP ${response.status}.`);
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The local service timed out. Shutdown has not been confirmed.');
      if (error instanceof TypeError) throw new Error('Cannot connect to the local service. Shutdown has not been confirmed.');
      throw error;
    } finally { clearTimeout(deadline); requests.delete(controller); }
  }

  function render(summary) {
    root.dataset.phase = phase;
    let completed = 0;
    ids.forEach(id => {
      const check = checks.get(id);
      const row = root.querySelector(`[data-check="${id}"]`);
      row.dataset.state = check.state;
      row.querySelector('.mark').textContent = check.state === 'ready' ? '✓' : check.state === 'error' ? '!' : '·';
      row.querySelector('.detail').textContent = check.detail;
      row.querySelector('.state').textContent = check.state === 'ready' ? 'Done' : check.state === 'error' ? 'Not ready' : began ? 'Waiting' : 'Waiting';
      if (check.state === 'ready') completed++;
    });
    document.getElementById('fraction').textContent = `${completed} / 4`;
    root.querySelector('.progress').setAttribute('aria-valuenow',String(completed));
    root.querySelector('.fill').style.width = `${completed/4*100}%`;
    document.getElementById('summary').textContent = summary;
    button.disabled = (!supported && (supportKnown || phase !== 'error')) || ['closing','ready','exiting','closed'].includes(phase);
    button.textContent = phase === 'closed' ? 'Application closed' : phase === 'error' ? supportKnown ? 'Retry shutdown' : 'Reconnect' : ['closing','ready','exiting'].includes(phase) ? 'Shutting down safely…' : 'Shut down Code Steward';
    document.getElementById('back').hidden = began;
    if (phase === 'closed') {
      document.getElementById('heading').textContent = 'All done. See you soon!';
      document.getElementById('subtitle').textContent = 'This session is complete. Code Steward has exited.';
      document.getElementById('note').textContent = 'You can close this page now. Ollama keeps its previous running state.';
    } else if (began) {
      document.getElementById('heading').textContent = 'Taking care of the finishing touches.';
      document.getElementById('subtitle').textContent = 'Please wait while current work finishes safely.';
      document.getElementById('note').textContent = 'If AI is processing a file, shutdown waits for its result to be saved. Ollama stays running.';
    }
  }

  function failure(message,action='The application has not been forcibly stopped. Check the runtime logs, then retry shutdown.') {
    clearTimeout(timer);
    phase = 'error';
    render('Shutdown has not been confirmed');
    document.getElementById('error-description').textContent = message;
    document.getElementById('error-action').textContent = action;
    document.getElementById('retry').disabled = supportKnown && !supported;
    document.getElementById('retry').textContent = supportKnown ? 'Retry shutdown' : 'Reconnect';
    if (!dialog.open) dialog.showModal();
  }

  function apply(data) {
    if (!data || typeof data.supported !== 'boolean' || typeof data.state !== 'string') throw new Error('The application returned an invalid shutdown status.');
    if (!['idle','closing','ready','exiting','error'].includes(data.state)) throw new Error('The application returned an unknown shutdown state.');
    supported = data.supported;
    supportKnown = true;
    if (typeof data.token === 'string') token = data.token;
    if (!supported) {
      render('This launch method does not support API shutdown');
      document.getElementById('note').textContent = data.detail || 'Start the application with venv\\Scripts\\python launch.py to enable shutdown.';
      return;
    }
    if (!Array.isArray(data.checks) || data.checks.length !== 3) throw new Error('The application did not return all shutdown checks.');
    const seen = new Set();
    data.checks.forEach(check => {
      if (!ids.slice(0,3).includes(check.id) || seen.has(check.id) || !['pending','ready','error'].includes(check.state) || typeof check.detail !== 'string') throw new Error('The application returned incomplete shutdown checks.');
      seen.add(check.id);
      checks.set(check.id,check);
    });
    phase = data.state;
    canFinish = data.can_finish === true;
    if (['ready','exiting'].includes(phase) && !ids.slice(0,3).every(id => checks.get(id).state === 'ready')) {
      throw new Error('The completion state does not match the background checks. Shutdown cannot be confirmed.');
    }
    if (phase !== 'idle') began = true;
    const issue = data.checks.find(check => check.state === 'error');
    if (phase === 'error' || issue) {
      failure(data.error || issue?.detail || 'The shutdown task reported an error.',issue?.action);
      return;
    }
    if (phase === 'idle') render('When you are ready, select Shut down below');
    else if (phase === 'ready' || phase === 'exiting') render('Background work is complete. Exiting the API…');
    else render(data.checks.find(check => check.state === 'pending')?.detail || 'Waiting for background work to finish…');
  }

  async function confirmExit(run, attempt=0) {
    if (run !== generation) return;
    const controller = new AbortController();
    requests.add(controller);
    const deadline = setTimeout(() => controller.abort(),2000);
    let disconnected = false;
    try {
      const response = await fetch('/system/health',{signal:controller.signal,cache:'no-store',credentials:'same-origin',redirect:'error'});
      if (!response.ok) throw new Error(`The API exit check returned HTTP ${response.status}. Exit has not been confirmed.`);
    } catch (error) {
      if (error instanceof TypeError) disconnected = true;
      else if (error.name !== 'AbortError') { if (run === generation) failure(error.message); return; }
    } finally { clearTimeout(deadline); requests.delete(controller); }
    if (run !== generation) return;
    if (disconnected && accepted) {
      checks.set('server',{id:'server',state:'ready',detail:'The application confirmed exit and the local API connection has closed.'});
      phase = 'closed';
      render('Safely closed. Thanks for your work today.');
    } else if (attempt >= 29) {
      failure('Background work is complete, but API exit has not been confirmed.','Check the runtime logs. Retrying reads the actual shutdown status again.');
    } else timer = setTimeout(() => confirmExit(run,attempt+1),500);
  }

  async function finish(run) {
    if (finishing || run !== generation) return;
    if (!requested || !canFinish || !ids.slice(0,3).every(id => checks.get(id).state === 'ready')) {
      failure('Background work is not fully confirmed complete. API exit has not been requested.');
      return;
    }
    finishing = true;
    try {
      const result = await read('/shutdown/finish','POST');
      if (run !== generation) return;
      if (result.accepted !== true) throw new Error('The application did not acknowledge the exit request. Shutdown is not confirmed.');
      accepted = true;
      phase = 'exiting';
      checks.set('server',{id:'server',state:'pending',detail:'Exit was acknowledged. Waiting for the API connection to close.'});
      render('Background work is complete. Exiting the API…');
      timer = setTimeout(() => confirmExit(run),300);
    } catch (error) { if (run === generation) failure(error.message); }
    finally { finishing = false; }
  }

  async function poll(run) {
    try {
      const data = await read('/shutdown/status');
      if (run !== generation) return;
      apply(data);
      if (!supported || phase === 'error' || phase === 'idle') return;
      if (phase === 'ready' && requested) { await finish(run); return; }
      if (phase === 'exiting') {
        if (!ids.slice(0,3).every(id => checks.get(id).state === 'ready')) throw new Error('The API is exiting, but background completion has not been confirmed.');
        accepted = true;
        await confirmExit(run);
        return;
      }
      timer = setTimeout(() => poll(run),800);
    } catch (error) { if (run === generation) failure(error.message); }
  }

  async function closeProgram() {
    if (!supported || ['closing','ready','exiting','closed'].includes(phase)) return;
    if (dialog.open) dialog.close();
    clearTimeout(timer);
    const run = ++generation;
    phase = 'closing'; began = true; requested = true;
    render('Requesting a safe shutdown…');
    try {
      const result = await read('/shutdown/start','POST');
      if (run !== generation) return;
      apply(result);
      if (phase === 'error') return;
      if (phase === 'idle') throw new Error('The application did not accept the shutdown request. Try again.');
      if (phase === 'ready') await finish(run);
      else if (phase === 'exiting') { accepted = true; await confirmExit(run); }
      else timer = setTimeout(() => poll(run),300);
    } catch (error) { if (run === generation) failure(error.message); }
  }
  function retryOrClose() {
    if (!supportKnown) {
      if (dialog.open) dialog.close();
      phase = 'idle';
      render('Reconnecting to the local application…');
      void poll(++generation);
    } else void closeProgram();
  }
  button.addEventListener('click',retryOrClose);
  document.getElementById('retry').addEventListener('click',retryOrClose);
  document.getElementById('close-dialog').addEventListener('click',() => dialog.close());
  window.addEventListener('pagehide',() => {generation++;clearTimeout(timer);requests.forEach(controller => controller.abort());});
  void poll(generation);
})();
