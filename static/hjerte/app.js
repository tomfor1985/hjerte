(() => {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  const status = document.getElementById('connection-status');
  function connection() { if (status) status.hidden = navigator.onLine; }
  addEventListener('online', connection); addEventListener('offline', connection); connection();
  document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  }));
  document.querySelectorAll('[data-deadline]').forEach(element => {
    const deadline = new Date(element.dataset.deadline).getTime();
    const serverNow = new Date(element.dataset.serverNow).getTime();
    const clockOffset = Number.isFinite(serverNow) ? serverNow - Date.now() : 0;
    let expired = false;
    function tick() {
      const seconds = Math.max(0, Math.ceil((deadline - Date.now() - clockOffset) / 1000));
      element.textContent = `${Math.floor(seconds / 60).toString().padStart(2,'0')}:${(seconds % 60).toString().padStart(2,'0')}`;
      element.classList.toggle('urgent', seconds < 300);
      if (!seconds && !expired) { expired = true; setTimeout(() => location.reload(), 1000); }
    }
    tick(); setInterval(tick, 1000); document.addEventListener('visibilitychange', tick);
  });
  let installPrompt;
  const install = document.getElementById('install-app');
  addEventListener('beforeinstallprompt', event => {
    event.preventDefault(); installPrompt = event; if(install) install.hidden = false;
  });
  if (install) install.addEventListener('click', async () => {
    if (!installPrompt) return;
    await installPrompt.prompt(); installPrompt = null; install.hidden = true;
  });
  addEventListener('appinstalled', () => { if(install) install.hidden = true; });
})();

// Optional agent access uses the same visible controls and authenticated actions.
(() => {
  const context = document.modelContext;
  const form = document.querySelector('.question-footer form');
  if (!context?.registerTool || !form) return;
  const lifecycle = new AbortController();
  let busy = false;
  const empty = input => {
    if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length) throw new Error('Expected an empty object.');
  };
  const register = tool => {
    try { Promise.resolve(context.registerTool(tool, {signal:lifecycle.signal})).catch(() => {}); } catch (_) { /* Optional browser capability. */ }
  };
  register({name:'read_current_study_question',title:'Read current study question',
    description:'Read the question and choices currently shown. Does not reveal hidden answers.',
    inputSchema:{type:'object',properties:{},additionalProperties:false},
    annotations:{readOnlyHint:true,untrustedContentHint:true},
    execute(input) {
      empty(input);
      return {question:document.querySelector('.question-stem')?.textContent.trim(),
        choices:Array.from(document.querySelectorAll('.options .option')).map(x => x.textContent.trim()),
        learningFlag:form.querySelector('button').textContent.includes('Remove learning flag')};
    }});
  register({name:'toggle_question_learning_flag',title:'Toggle learning flag',
    description:'Save or remove the current question learning flag for future practice, updating the visible button.',
    inputSchema:{type:'object',properties:{},additionalProperties:false},
    annotations:{readOnlyHint:false,untrustedContentHint:false},
    async execute(input) {
      empty(input); if (busy) throw new Error('A flag update is already in progress.');
      busy = true;
      try {
        const response = await fetch(form.action,{method:'POST',body:new FormData(form),credentials:'same-origin'});
        if (!response.ok || new URL(response.url).pathname === '/login/') throw new Error('Flag could not be saved.');
        const page = new DOMParser().parseFromString(await response.text(),'text/html');
        const button = page.querySelector('.question-footer form button');
        if (!button) throw new Error('Refresh to inspect the saved flag.');
        form.querySelector('button').textContent = button.textContent;
        return {saved:true,learningFlag:button.textContent.includes('Remove learning flag')};
      } finally { busy = false; }
    }});
  addEventListener('pagehide',() => lifecycle.abort(),{once:true});
})();

// Open a collapsed Studio section when following its in-page link.
(() => {
  const reveal = hash => {
    const section = document.getElementById(hash.slice(1));
    if (section?.tagName === 'DETAILS') section.open = true;
  };
  reveal(location.hash);
  addEventListener('hashchange', () => reveal(location.hash));
  document.querySelectorAll('.studio-links a[href^="#"], .studio-next a[href^="#"]').forEach(link => {
    link.addEventListener('click', () => reveal(link.hash));
  });
})();
