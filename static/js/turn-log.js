// Center panel: the campaign log.  One card per turn, rendered
// incrementally as new entries arrive from /state.
'use strict';

// ---- Campaign Log ----
function renderLog(state) {
  const log = state.log || [];
  const scrollEl = $('#log-scroll');
  const empty = $('#log-empty');

  if (log.length === 0) {
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  // Check if user has scrolled up
  const atBottom = scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 30;

  // Incremental rendering: only add new entries
  const startFrom = renderedLogCount;
  for (let i = startFrom; i < log.length; i++) {
    const entry = log[i];
    const card = buildTurnCard(entry, state);
    scrollEl.appendChild(card);
  }
  renderedLogCount = log.length;

  // Apply x-ray mode to all details
  if (xrayMode) {
    scrollEl.querySelectorAll('details').forEach(d => d.open = true);
  }

  // Auto-scroll
  if (atBottom || !userScrolled) {
    scrollEl.scrollTop = scrollEl.scrollHeight;
  }

  // Update context bar
  updateContextBar(state);
}

function buildTurnCard(entry, state) {
  const agent = state.agents[entry.agent_id] || {};
  const isDM = agent.role === 'dm';
  const isCompaction = entry.type === 'compaction';

  // Enhanced compaction card
  if (isCompaction) return buildCompactionCard(entry, state);

  const card = document.createElement('div');
  card.className = 'turn-card' + (isDM ? ' dm-turn' : '');

  // ---- Level 0: Header + message (always visible) ----
  const header = document.createElement('div');
  header.className = 'turn-card__header';

  const emoji = document.createElement('span');
  emoji.className = 'turn-card__emoji';
  emoji.textContent = agent.emoji || '';

  const name = document.createElement('span');
  name.className = 'turn-card__agent-name' + (isDM ? ' dm' : '');
  name.textContent = agent.name || entry.agent_id;

  if (!isDM) {
    const order = state.agent_order || [];
    const idx = order.indexOf(entry.agent_id);
    if (idx > 0) name.style.color = PLAYER_COLORS[(idx - 1) % PLAYER_COLORS.length];
  }

  const roundTag = document.createElement('span');
  roundTag.className = 'turn-card__round-tag';
  roundTag.textContent = 'Round ' + entry.round;

  header.appendChild(emoji);
  header.appendChild(name);
  header.appendChild(roundTag);
  card.appendChild(header);

  // Message (always visible — Level 0)
  const msg = document.createElement('div');
  msg.className = 'turn-card__message';
  msg.textContent = entry.message || '';
  card.appendChild(msg);

  // Whispers received (Level 0 — part of the narrative)
  if (entry.whispers_received && entry.whispers_received.length > 0) {
    const wDiv = document.createElement('div');
    wDiv.className = 'turn-card__whispers';
    entry.whispers_received.forEach(w => {
      const wItem = document.createElement('div');
      wItem.className = 'turn-card__whisper';
      wItem.textContent = 'Whisper: "' + (w.text || '') + '"';
      wDiv.appendChild(wItem);
    });
    card.appendChild(wDiv);
  }

  // ---- Level 1: "How this turn worked" (one click) ----
  const hasDetail = (entry.reasoning && entry.reasoning.length > 20) ||
                    (entry.tool_calls && entry.tool_calls.length > 0) ||
                    entry.tokens_in > 0 || entry.tokens_out > 0;

  if (hasDetail) {
    const trigger = document.createElement('span');
    trigger.className = 'turn-detail-trigger' + (xrayMode ? ' open' : '');
    trigger.textContent = 'How this turn worked';

    const detailBody = document.createElement('div');
    detailBody.className = 'turn-detail-body' + (xrayMode ? ' open' : '');
    let detailRendered = xrayMode;

    trigger.addEventListener('click', function() {
      const isOpen = detailBody.classList.toggle('open');
      trigger.classList.toggle('open', isOpen);
      if (isOpen && !detailRendered) {
        renderTurnDetail(detailBody, entry, state);
        detailRendered = true;
      }
    });

    if (xrayMode) renderTurnDetail(detailBody, entry, state);

    card.appendChild(trigger);
    card.appendChild(detailBody);
  }

  return card;
}

function formatArgs(args) {
  if (!args || typeof args !== 'object') return '';
  return Object.entries(args).map(([k, v]) => k + ': ' + JSON.stringify(v)).join(', ');
}

function formatResult(tc) {
  const span = document.createElement('span');
  const result = tc.result;

  if (tc.name === 'roll_dice') {
    const inner = document.createElement('span');
    inner.className = 'dice-roll';
    const sides = tc.args && tc.args.sides ? tc.args.sides : '?';
    inner.textContent = '➠ ' + result + (sides ? ' (d' + sides + ')' : '');
    span.appendChild(inner);
  } else if (tc.name === 'resolve_action') {
    const inner = document.createElement('span');
    if (typeof result === 'string') {
      const isSuccess = result.toLowerCase().includes('success');
      inner.className = isSuccess ? 'action-success' : 'action-fail';
      inner.textContent = result;
    } else {
      inner.textContent = JSON.stringify(result);
    }
    span.appendChild(inner);
  } else {
    span.textContent = typeof result === 'string' ? result : JSON.stringify(result);
  }
  return span;
}

// Scroll tracking
$('#log-scroll').addEventListener('scroll', function() {
  const atBottom = this.scrollTop + this.clientHeight >= this.scrollHeight - 30;
  userScrolled = !atBottom;
});

// JSON syntax highlighting
function syntaxHighlight(json) {
  if (typeof json !== 'string') json = JSON.stringify(json, null, 2);
  return json.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?)/g, function(match) {
      if (/:$/.test(match)) return '<span class="j-key">' + match + '</span>';
      return '<span class="j-str">' + match + '</span>';
    })
    .replace(/\b(-?\d+\.?\d*([eE][+-]?\d+)?)\b/g, '<span class="j-num">$1</span>')
    .replace(/\b(true|false)\b/g, '<span class="j-bool">$1</span>')
    .replace(/\bnull\b/g, '<span class="j-null">null</span>');
}
