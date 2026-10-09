// Left panel: top bar, agent roster cards, and whispers.
'use strict';

// ---- Top bar ----
function renderTopBar(state) {
  $('#tb-title').textContent = state.name || 'Campaign';
  const order = state.agent_order || [];
  const agentCount = order.length;
  const turnIndex = state.current_agent_index || 0;
  $('#tb-round').textContent = 'Round ' + state.current_round + ' • Turn ' + (turnIndex + 1) + ' of ' + agentCount;
  const status = state.status || 'active';
  const statusEl = $('#tb-status');
  statusEl.dataset.status = status;
  statusEl.textContent = status.charAt(0).toUpperCase() + status.slice(1);
  $('#tb-code').textContent = state.code;
  // Rulebook badge
  const rbEl = $('#tb-rulebook');
  if (state.rulebook_name && state.rulebook_chunks > 0) {
    rbEl.style.display = '';
    rbEl.textContent = state.rulebook_name + ' (' + state.rulebook_chunks + ' chunks)';
  } else {
    rbEl.style.display = 'none';
  }
}

// Copy code on click
$('#tb-code').addEventListener('click', () => {
  if (code) {
    navigator.clipboard.writeText(code).then(() => toast('Code copied'));
  }
});

// ---- Agent Roster ----
function renderRoster(state) {
  const container = $('#roster-list');
  const order = state.agent_order || [];
  const nextAgentId = order[state.current_agent_index] || '';

  // Only rebuild DOM if agent count changed
  if (container.children.length !== order.length) {
    container.innerHTML = '';
    order.forEach((aid, i) => {
      const a = state.agents[aid];
      if (!a) return;
      const card = document.createElement('div');
      card.className = 'agent-card' + (a.role === 'dm' ? ' dm' : '');
      card.dataset.aid = aid;

      const isDM = a.role === 'dm';
      const playerColor = isDM ? '' : PLAYER_COLORS[(i - 1) % PLAYER_COLORS.length];
      if (!isDM) card.style.borderLeftColor = playerColor;
      if (!isDM) card.style.borderLeftWidth = '3px';

      const summary = extractSummary(a.system_prompt || '');

      const topDiv = document.createElement('div');
      topDiv.className = 'agent-card__top';
      const emojiSpan = document.createElement('span');
      emojiSpan.className = 'agent-card__emoji';
      emojiSpan.textContent = a.emoji || '';
      const nameSpan = document.createElement('span');
      nameSpan.className = 'agent-card__name' + (isDM ? ' dm-name' : '');
      nameSpan.textContent = a.name || aid;
      if (!isDM) nameSpan.style.color = playerColor;
      const roleSpan = document.createElement('span');
      roleSpan.className = 'agent-card__role ' + (isDM ? 'dm-role' : 'player-role');
      roleSpan.textContent = isDM ? 'DM' : 'Player';
      topDiv.appendChild(emojiSpan);
      topDiv.appendChild(nameSpan);
      topDiv.appendChild(roleSpan);

      const summaryDiv = document.createElement('div');
      summaryDiv.className = 'agent-card__summary';
      summaryDiv.textContent = summary;

      const whisperBtn = document.createElement('button');
      whisperBtn.className = 'agent-card__whisper-btn';
      whisperBtn.textContent = 'Whisper';
      whisperBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleWhisperInput(aid, card);
      });

      // Whisper input (inline)
      const whisperInput = document.createElement('div');
      whisperInput.className = 'whisper-input';
      whisperInput.dataset.aid = aid;
      whisperInput.innerHTML = `
        <textarea placeholder="Whisper something to ${esc(a.name)}..."></textarea>
        <div class="whisper-input__actions">
          <button class="whisper-input__cancel">Cancel</button>
          <button class="whisper-input__send">Send</button>
        </div>`;
      whisperInput.querySelector('.whisper-input__cancel').addEventListener('click', (e) => {
        e.stopPropagation();
        whisperInput.classList.remove('open');
        activeWhisperAgent = null;
      });
      whisperInput.querySelector('.whisper-input__send').addEventListener('click', (e) => {
        e.stopPropagation();
        const text = whisperInput.querySelector('textarea').value.trim();
        if (text) sendWhisper(aid, text);
        whisperInput.classList.remove('open');
        activeWhisperAgent = null;
      });

      // Detail (full system prompt)
      const detail = document.createElement('div');
      detail.className = 'agent-card__detail';
      const pre = document.createElement('pre');
      pre.textContent = a.system_prompt || '(no system prompt)';
      detail.appendChild(pre);

      card.addEventListener('click', () => {
        if (expandedAgentCard === aid) {
          detail.classList.remove('open');
          expandedAgentCard = null;
        } else {
          // Close other expanded
          container.querySelectorAll('.agent-card__detail.open').forEach(d => d.classList.remove('open'));
          detail.classList.add('open');
          expandedAgentCard = aid;
        }
      });

      card.appendChild(topDiv);
      card.appendChild(summaryDiv);
      card.appendChild(whisperBtn);
      card.appendChild(whisperInput);
      card.appendChild(detail);
      container.appendChild(card);
    });
  }

  // Update current highlight
  container.querySelectorAll('.agent-card').forEach(card => {
    card.classList.toggle('current', card.dataset.aid === nextAgentId);
  });
}

function extractSummary(md) {
  const lines = md.split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith('#') && trimmed.length > 10) {
      return trimmed.length > 80 ? trimmed.substring(0, 77) + '...' : trimmed;
    }
  }
  return '';
}

function toggleWhisperInput(aid, card) {
  // Close any open whisper
  $$('.whisper-input.open').forEach(w => w.classList.remove('open'));

  if (activeWhisperAgent === aid) {
    activeWhisperAgent = null;
    return;
  }
  const input = card.querySelector('.whisper-input');
  input.classList.add('open');
  input.querySelector('textarea').value = '';
  input.querySelector('textarea').focus();
  activeWhisperAgent = aid;
}

async function sendWhisper(agentId, text) {
  if (!code) return;
  try {
    const res = await fetch(BASE + '/' + code + '/whisper', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: agentId, text: text }),
    });
    if (res.ok) {
      toast('Whisper sent');
      pollState();
    } else {
      const data = await res.json();
      toast(data.error || 'Whisper failed');
    }
  } catch (e) {
    toast('Network error');
  }
}
