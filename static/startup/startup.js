/* Real checks run independently; each completed result is presented for one second.
   The decorative scene loops in CSS independently of check progress. */
(() => {
  'use strict';
  const root = document.getElementById('startup');
  const ids = ['api', 'service', 'storage', 'watcher', 'scan', 'ai'];
  const coreIds = ids.slice(0, -1);
  const states = new Set(['ready', 'pending', 'error', 'skipped']);
  const labels = {api:'API connection',service:'Background service',storage:'File index',watcher:'File monitoring',scan:'Initial scan',ai:'Local AI'};
  const marks = {ready:'✓',pending:'·',error:'!',skipped:'–'};
  const names = {ready:'Ready',pending:'Checking',error:'Unavailable',skipped:'Disabled'};
  const dialog = document.getElementById('error-dialog');
  const enter = document.getElementById('enter');
  const continueBasic = document.getElementById('continue-basic');
  const HOLD_MS = 1000;
  let checks = new Map();
  let actual = new Map();
  let generation = 0;
  let pollTimer = 0;
  let presentationTimer = 0;
  let began = 0;
  let index = 0;
  let holding = false;
  let finished = false;
  let announced = false;
  const requests = new Set();

  function validate(check) {
    if (!check || !ids.includes(check.id) || !states.has(check.state) || typeof check.detail !== 'string'
        || (check.state === 'skipped' && check.id !== 'ai')) {
      throw new Error('The startup check returned invalid data. Check the runtime logs.');
    }
    return {...check,label:labels[check.id]};
  }

  async function read(path, timeout) {
    const controller = new AbortController();
    requests.add(controller);
    const deadline = setTimeout(() => controller.abort(),timeout);
    try {
      const response = await fetch(path,{signal:controller.signal,cache:'no-store',credentials:'same-origin',redirect:'error'});
      if (!response.ok) throw new Error(`The local service returned HTTP ${response.status}. Check the runtime logs.`);
      return await response.json();
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The local service timed out. Confirm that Code Steward is running, then try again.');
      if (error instanceof TypeError) throw new Error('Cannot connect to the local service. Restart Code Steward, then try again.');
      throw error;
    } finally {clearTimeout(deadline);requests.delete(controller);}
  }

  function errorItems(errors) {
    const list = document.getElementById('error-list');
    list.replaceChildren();
    errors.forEach(check => {
      const item = document.createElement('li');
      const title = document.createElement('strong');
      title.textContent = check.label;
      const detail = document.createElement('p');
      detail.textContent = check.detail;
      item.append(title,detail);
      if (check.action) {
        const fix = document.createElement('p');
        fix.className = 'fix';
        fix.textContent = check.action;
        item.append(fix);
      }
      list.append(item);
    });
  }

  function render() {
    let confirmed = 0;
    ids.forEach((id,position) => {
      const check = checks.get(id);
      const row = root.querySelector(`[data-check="${id}"]`);
      row.dataset.state = check.state;
      row.dataset.active = String(position === index && !finished);
      row.querySelector('.check-mark').textContent = marks[check.state];
      row.querySelector('.check-detail').textContent = check.detail;
      row.querySelector('.check-state').textContent = check.state === 'pending' && position > index ? 'Waiting' : names[check.state];
      if (check.state === 'ready' || check.state === 'skipped') confirmed++;
    });
    const coreReady = coreIds.every(id => checks.get(id).state === 'ready');
    const ai = checks.get('ai');
    const errors = ids.map(id => checks.get(id)).filter(check => check.state === 'error');
    const complete = finished && confirmed === ids.length;
    root.classList.toggle('complete',complete);
    root.querySelector('.progress').setAttribute('aria-valuenow',String(confirmed));
    root.querySelector('.fill').style.width = `${confirmed/ids.length*100}%`;
    document.getElementById('fraction').textContent = `${confirmed} / ${ids.length}`;
    let summary;
    if (!finished) {
      const current = checks.get(ids[index]);
      summary = holding ? `${labels[ids[index]]}: ${names[current.state]}` : `Checking ${labels[ids[index]].toLowerCase()}…`;
    } else if (complete) summary = ai.state === 'skipped' ? 'Core features ready · AI is disabled' : 'Startup checks complete. Welcome back.';
    else summary = coreReady ? 'Core features ready · AI is unavailable' : 'Some startup checks need attention';
    document.getElementById('summary').textContent = summary;
    const usable = finished && (complete || (coreReady && ai.state === 'error'));
    enter.setAttribute('aria-disabled',String(!usable));
    enter.textContent = complete ? 'Open workspace' : usable ? 'Open core features' : 'Checking startup…';
    continueBasic.hidden = !(finished && coreReady && ai.state === 'error');
    if (finished && errors.length) {
      errorItems(errors);
      document.getElementById('error-title').textContent = coreReady ? 'Core features ready. AI is unavailable.' : 'Some checks are not ready';
      document.getElementById('error-description').textContent = coreReady ? 'You can use scanning, file lists and the API while AI analysis is unavailable.' : 'Follow the suggestions below, then run the checks again.';
      if (!announced) {announced=true;if (!dialog.open) dialog.showModal();}
    } else if (dialog.open) dialog.close();
  }

  function present(run) {
    if (run !== generation || holding || finished) return;
    if (index >= ids.length) {finished=true;render();return;}
    const id = ids[index];
    const result = actual.get(id);
    if (!result || result.state === 'pending') {
      checks.set(id,result || {id,label:labels[id],state:'pending',detail:'Waiting for the actual check result…'});
      render();
      return;
    }
    checks.set(id,result);
    holding=true;
    render();
    presentationTimer=setTimeout(() => {
      if (run !== generation) return;
      holding=false;
      index++;
      present(run);
    },HOLD_MS);
  }

  async function core(run) {
    try {
      const result = await read('/startup/status',8000);
      if (run !== generation) return;
      if (!Array.isArray(result.checks) || result.checks.length !== coreIds.length) throw new Error('The service did not return all startup checks. Restart it, then try again.');
      const incoming = result.checks.map(validate);
      if (new Set(incoming.map(check => check.id)).size !== coreIds.length || incoming.some(check => !coreIds.includes(check.id))) throw new Error('The service returned incomplete startup checks.');
      incoming.forEach(check => {
        if (check.state === 'pending' && check.deferred_by_resources === true) began = Date.now();
        const value = check.state === 'pending' && Date.now()-began >= 120000
          ? {...check,state:'error',detail:'This check is still pending after two minutes.',action:'Large folders can take longer. Review the runtime logs, then run the checks again.'}
          : check;
        actual.set(check.id,value);
      });
      present(run);
      if (coreIds.some(id => actual.get(id).state === 'pending')) pollTimer=setTimeout(() => core(run),1000);
    } catch (error) {
      if (run !== generation) return;
      actual.set('api',{id:'api',label:labels.api,state:'error',detail:error.message,action:'Confirm that Code Steward is running, then select Check again.'});
      coreIds.slice(1).forEach(id => actual.set(id,{id,label:labels[id],state:'error',detail:'This check could not be verified because the API is unavailable.',action:'Restore the local API connection, then run the checks again.'}));
      present(run);
    }
  }

  async function ai(run) {
    try {
      const result = validate(await read('/startup/ai',18000));
      if (run !== generation) return;
      if (result.id !== 'ai' || result.state === 'pending') throw new Error('The service did not return a valid local AI check.');
      actual.set('ai',result);
    } catch (error) {
      if (run !== generation) return;
      actual.set('ai',{id:'ai',label:labels.ai,state:'error',detail:error.message,action:'Confirm that Code Steward and Ollama are running, then select Check again.'});
    }
    present(run);
  }

  function start() {
    generation++;
    requests.forEach(controller => controller.abort());
    requests.clear();
    clearTimeout(pollTimer);
    clearTimeout(presentationTimer);
    began=Date.now(); index=0; holding=false; finished=false; announced=false;
    actual=new Map();
    if (dialog.open) dialog.close();
    checks=new Map(ids.map(id => [id,{id,label:labels[id],state:'pending',detail:'Waiting for the previous check…'}]));
    const run=generation;
    present(run);
    void core(run);
    void ai(run);
  }
  document.getElementById('retry').addEventListener('click',start);
  document.getElementById('retry-dialog').addEventListener('click',start);
  document.getElementById('close-dialog').addEventListener('click',() => dialog.close());
  enter.addEventListener('click',event => {if (enter.getAttribute('aria-disabled') === 'true') event.preventDefault();});
  window.addEventListener('pagehide',() => {generation++;clearTimeout(pollTimer);clearTimeout(presentationTimer);requests.forEach(controller => controller.abort());});
  start();
})();
