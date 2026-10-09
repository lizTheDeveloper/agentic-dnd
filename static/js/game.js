// Running a campaign: enter it, poll /state, render everything, and the
// right-hand controls (next turn, autoplay, x-ray, pause/end, new).
'use strict';

// ----------------------------------------------------------------
// Enter campaign mode
// ----------------------------------------------------------------
function enterCampaign() {
  $('#setup-screen').classList.add('hidden');
  $('#top-bar').style.display = '';
  $('#mobile-tabs').style.display = '';
  $('#app').classList.add('active');
  initMobileTabs();
  renderedLogCount = 0;
  lastSeq = -1;

  // Restore preferences
  $('#xray-check').checked = xrayMode;
  $('#chk-autoplay').checked = autoplayOn;
  $('#rng-speed').value = autoplaySpeed;
  $('#speed-val').textContent = autoplaySpeed + 's';

  pollState();
  pollTimer = setInterval(pollState, 1000);
  if (autoplayOn) startAutoplay();
}

// ----------------------------------------------------------------
// Mobile tab switching
// ----------------------------------------------------------------
function initMobileTabs() {
  const btns = $$('#mobile-tabs button');
  btns.forEach(btn => {
    btn.addEventListener('click', () => {
      btns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const panel = btn.dataset.panel;
      $('#panel-roster').classList.toggle('mobile-active', panel === 'roster');
      $('#panel-campaign').classList.toggle('mobile-active', panel === 'campaign');
      $('#panel-controls').classList.toggle('mobile-active', panel === 'controls');
    });
  });
}

// ----------------------------------------------------------------
// Poll state
// ----------------------------------------------------------------
async function pollState() {
  if (!code) return;
  try {
    const url = BASE + '/' + code + '/state' + (lastSeq >= 0 ? '?since=' + lastSeq : '');
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    if (data.nochange) return;
    lastSeq = data.seq;
    campaignState = data;
    renderAll(data);
  } catch (e) { /* silent */ }
}

// ----------------------------------------------------------------
// Render everything
// ----------------------------------------------------------------
function renderAll(state) {
  renderTopBar(state);
  renderRoster(state);
  renderLog(state);
  renderMemory(state);
  renderArchitecture(state);
  renderControls(state);
}

// ---- Controls Panel ----
function renderControls(state) {
  const status = state.status || 'active';
  const isActive = status === 'active';
  const isPaused = status === 'paused';
  const isEnded = status === 'ended';

  // Next turn button
  const nextBtn = $('#btn-next');
  nextBtn.disabled = !isActive || advancing;
  nextBtn.textContent = advancing ? 'Generating...' : 'Next Turn';
  nextBtn.className = 'next-turn-btn' + (advancing ? ' advancing' : '');

  // Campaign info
  $('#info-round').textContent = state.current_round;
  $('#info-agents').textContent = (state.agent_order || []).length;
  $('#info-turns').textContent = (state.log || []).length;
  $('#info-status').textContent = status.charAt(0).toUpperCase() + status.slice(1);

  // Inference meter
  let totalCost = 0;
  let totalTokens = 0;
  (state.log || []).forEach(entry => {
    totalCost += entry.cost_cents || 0;
    totalTokens += (entry.tokens_in || 0) + (entry.tokens_out || 0);
  });
  const maxBudget = 100; // $1.00 budget for the meter
  const costPct = Math.min(100, Math.round((totalCost / maxBudget) * 100));
  const inferFill = $('#inference-fill');
  inferFill.style.width = costPct + '%';
  inferFill.className = 'inference-meter__fill' + (costPct > 80 ? ' danger' : costPct > 50 ? ' warn' : '');
  $('#inference-cost').textContent = '$' + (totalCost / 100).toFixed(2);
  $('#inference-tokens').textContent = fmtNum(totalTokens) + ' tokens';

  // Action buttons
  const actionRow = $('#action-row');
  actionRow.innerHTML = '';
  if (isActive) {
    const pauseBtn = document.createElement('button');
    pauseBtn.className = 'btn-pause';
    pauseBtn.textContent = 'Pause';
    pauseBtn.addEventListener('click', () => setStatus('paused'));
    actionRow.appendChild(pauseBtn);

    const endBtn = document.createElement('button');
    endBtn.className = 'btn-end';
    endBtn.textContent = 'End Campaign';
    endBtn.addEventListener('click', () => {
      if (confirm('End this campaign? This cannot be undone.')) setStatus('ended');
    });
    actionRow.appendChild(endBtn);
  } else if (isPaused) {
    const resumeBtn = document.createElement('button');
    resumeBtn.className = 'btn-resume';
    resumeBtn.textContent = 'Resume';
    resumeBtn.addEventListener('click', () => setStatus('active'));
    actionRow.appendChild(resumeBtn);

    const endBtn = document.createElement('button');
    endBtn.className = 'btn-end';
    endBtn.textContent = 'End Campaign';
    endBtn.addEventListener('click', () => {
      if (confirm('End this campaign? This cannot be undone.')) setStatus('ended');
    });
    actionRow.appendChild(endBtn);
  } else if (isEnded) {
    const msg = document.createElement('span');
    msg.style.cssText = 'font-size:0.8rem;color:var(--ink-dim);padding:0.4rem 0;';
    msg.textContent = 'Campaign ended.';
    actionRow.appendChild(msg);
  }
}

// ---- Status change ----
async function setStatus(newStatus) {
  if (!code) return;
  try {
    const res = await fetch(BASE + '/' + code + '/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus }),
    });
    if (res.ok) {
      if (newStatus === 'paused') stopAutoplay();
      pollState();
    } else {
      const data = await res.json();
      toast(data.error || 'Failed');
    }
  } catch (e) { toast('Network error'); }
}

// ---- Advance turn ----
$('#btn-next').addEventListener('click', advanceTurn);

async function advanceTurn() {
  if (!code || advancing) return;
  if (campaignState && campaignState.status !== 'active') return;

  advancing = true;
  renderControls(campaignState);

  try {
    const res = await fetch(BASE + '/' + code + '/advance', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      toast(data.error || 'Error advancing turn');
    }
    await pollState();
  } catch (e) {
    toast('Network error');
  } finally {
    advancing = false;
    if (campaignState) renderControls(campaignState);
  }
}

// ---- Auto-play ----
$('#chk-autoplay').addEventListener('change', function() {
  autoplayOn = this.checked;
  localStorage.setItem('dnd-autoplay', autoplayOn);
  if (autoplayOn) startAutoplay();
  else stopAutoplay();
});

$('#rng-speed').addEventListener('input', function() {
  autoplaySpeed = parseInt(this.value, 10);
  $('#speed-val').textContent = autoplaySpeed + 's';
  localStorage.setItem('dnd-speed', autoplaySpeed);
  if (autoplayOn) { stopAutoplay(); startAutoplay(); }
});

function startAutoplay() {
  stopAutoplay();
  autoplayTimer = setInterval(() => {
    if (!advancing && campaignState && campaignState.status === 'active') {
      advanceTurn();
    }
  }, autoplaySpeed * 1000);
}

function stopAutoplay() {
  if (autoplayTimer) { clearInterval(autoplayTimer); autoplayTimer = null; }
}

// ---- X-ray mode ----
$('#xray-check').addEventListener('change', function() {
  xrayMode = this.checked;
  localStorage.setItem('dnd-xray', xrayMode);
  // Force full re-render of the log to apply x-ray state
  renderedLogCount = 0;
  $('#log-scroll').querySelectorAll('.turn-card').forEach(function(c) { c.remove(); });
  if (campaignState) renderLog(campaignState);
});

// ---- New Campaign ----
$('#btn-new-campaign').addEventListener('click', function() {
  stopAutoplay();
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  code = null;
  campaignState = null;
  renderedLogCount = 0;
  lastSeq = -1;
  advancing = false;
  $('#log-scroll').querySelectorAll('.turn-card').forEach(function(c) { c.remove(); });
  $('#log-empty').style.display = '';
  $('#roster-list').innerHTML = '';
  $('#memory-list').innerHTML = '';
  $('#app').classList.remove('active');
  $('#top-bar').style.display = 'none';
  $('#mobile-tabs').style.display = 'none';
  $('#setup-screen').classList.remove('hidden');
  history.replaceState(null, '', location.pathname);
});
