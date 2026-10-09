// Setup screen: draft autosave, emoji pickers, character slots, and the
// Launch button that POSTs everything to /create.
'use strict';

// ----------------------------------------------------------------
// Setup screen — draft persistence
// ----------------------------------------------------------------
const DRAFT_KEY = 'dnd.setup.draft.v1';

function saveDraft() {
  try {
    const draft = {
      name: $('#inp-campaign-name').value,
      dm: $('#inp-dm-md').value,
      chars: Array.from($$('.char-md')).map(t => t.value),
      ts: Date.now()
    };
    localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch (_) {}
}

function loadDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return;
    const draft = JSON.parse(raw);
    if (draft.name) $('#inp-campaign-name').value = draft.name;
    if (draft.dm) $('#inp-dm-md').value = draft.dm;
    if (draft.chars && draft.chars.length) {
      const slots = $$('.char-md');
      draft.chars.forEach((md, i) => {
        if (i === 0 && slots[0]) { slots[0].value = md; return; }
        if (i >= charCount) { $('#btn-add-char').click(); }
        const allSlots = $$('.char-md');
        if (allSlots[i]) allSlots[i].value = md;
      });
    }
  } catch (_) {}
}

function clearDraft() {
  try { localStorage.removeItem(DRAFT_KEY); } catch (_) {}
}

// Auto-save draft on every input change (debounced)
let draftTimer = null;
document.querySelector('#setup-screen').addEventListener('input', () => {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(saveDraft, 500);
});


// ----------------------------------------------------------------
// Emoji picker
// ----------------------------------------------------------------
function buildEmojiPicker(container, emojis, currentEmoji, onPick) {
  container.innerHTML = '';
  emojis.forEach(function(em) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = em;
    btn.className = em === currentEmoji ? 'selected' : '';
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      container.querySelectorAll('button').forEach(function(b) { b.classList.remove('selected'); });
      btn.classList.add('selected');
      onPick(em);
      container.classList.remove('open');
    });
    container.appendChild(btn);
  });
}

// DM emoji picker
buildEmojiPicker($('#dm-emoji-picker'), DM_EMOJIS, dmEmoji, function(em) {
  dmEmoji = em;
  $('#dm-emoji-trigger').textContent = em;
});
$('#dm-emoji-trigger').addEventListener('click', function() {
  $('#dm-emoji-picker').classList.toggle('open');
});

function showSetupError(msg) {
  const el = $('#setup-error');
  el.textContent = msg;
}

function clearSetupError() {
  $('#setup-error').textContent = '';
}

// Initialize first character's emoji picker
function initCharEmojiPicker(idx) {
  var defaultEmoji = CHARACTER_EMOJIS[idx % CHARACTER_EMOJIS.length];
  charEmojis[idx] = charEmojis[idx] || defaultEmoji;
  var picker = document.querySelector('.char-emoji-picker[data-index="' + idx + '"]');
  var trigger = document.querySelector('.char-emoji-trigger[data-index="' + idx + '"]');
  if (!picker || !trigger) return;
  trigger.textContent = charEmojis[idx];
  buildEmojiPicker(picker, CHARACTER_EMOJIS, charEmojis[idx], function(em) {
    charEmojis[idx] = em;
    trigger.textContent = em;
  });
  trigger.addEventListener('click', function(e) {
    e.stopPropagation();
    picker.classList.toggle('open');
  });
}
initCharEmojiPicker(0);

// Add character slot
$('#btn-add-char').addEventListener('click', () => {
  if (charCount >= 8) { toast('Maximum 8 characters'); return; }
  charCount++;
  const idx = charCount - 1;
  charEmojis[idx] = CHARACTER_EMOJIS[idx % CHARACTER_EMOJIS.length];
  const slot = document.createElement('div');
  slot.className = 'char-slot';
  slot.dataset.index = idx;
  slot.innerHTML = `
    <div class="char-slot__header">
      <span>Character ${charCount}</span>
      <button class="char-slot__remove">remove</button>
    </div>
    <div class="char-slot__body">
      <div class="char-name-row">
        <input type="text" class="char-name" placeholder="Character name" maxlength="80">
        <span class="emoji-trigger char-emoji-trigger" data-index="${idx}" title="Pick emoji">${charEmojis[idx]}</span>
        <div class="emoji-picker char-emoji-picker" data-index="${idx}"></div>
      </div>
      <textarea class="char-md" placeholder="# Character Name\n\nPersonality, backstory, abilities..."></textarea>
    </div>`;
  slot.querySelector('.char-slot__header').addEventListener('click', function(e) {
    if (e.target.classList.contains('char-slot__remove')) return;
    const body = this.parentElement.querySelector('.char-slot__body');
    body.style.display = body.style.display === 'none' ? '' : 'none';
  });
  slot.querySelector('.char-slot__remove').addEventListener('click', function(e) {
    e.stopPropagation();
    if ($$('.char-slot').length <= 1) { toast('Need at least 1 character'); return; }
    this.closest('.char-slot').remove();
    charCount--;
  });
  $('#char-slots').appendChild(slot);
  initCharEmojiPicker(idx);
});

// Launch campaign
$('#btn-start').addEventListener('click', async () => {
  const name = $('#inp-campaign-name').value.trim() || 'Untitled Campaign';
  const dmMd = $('#inp-dm-md').value.trim();
  const charMds = Array.from($$('.char-md')).map(t => t.value.trim()).filter(Boolean);

  // Gather character names and emojis
  const charNameEls = Array.from($$('.char-name'));
  const charNameValues = charNameEls.map(el => el.value.trim());
  const charSlots = Array.from($$('.char-slot'));
  const charEmojiValues = charSlots.map(function(slot) {
    var idx = parseInt(slot.dataset.index, 10);
    return charEmojis[idx] || '';
  });

  // DM name and emoji
  const dmNameVal = $('#inp-dm-name').value.trim();

  // Rulebook
  const rulebookName = $('#inp-rulebook-name').value.trim();
  // Auto-chunk if text is present but chunks haven't been generated
  if ($('#inp-rulebook-text').value.trim() && rulebookChunks.length === 0) {
    runChunker();
  }

  clearSetupError();
  if (!dmMd) { showSetupError('A Dungeon Master markdown is required.'); return; }
  if (charMds.length < 1) { showSetupError('Add at least one character with content.'); return; }

  $('#btn-start').disabled = true;
  $('#btn-start').textContent = 'Creating...';

  try {
    const payload = {
      name: name,
      dm_markdown: dmMd,
      dm_name: dmNameVal,
      dm_emoji: dmEmoji,
      characters: charMds,
      char_names: charNameValues,
      char_emojis: charEmojiValues,
    };
    // Include rulebook chunks if present
    if (rulebookChunks.length > 0) {
      payload.rulebook_chunks = rulebookChunks;
      payload.rulebook_name = rulebookName || 'Custom Rulebook';
    }

    const res = await fetch(BASE + '/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showSetupError(data.error || 'Failed to create campaign.');
      $('#btn-start').disabled = false;
      $('#btn-start').textContent = 'Launch Campaign';
      return;
    }
    code = data.code;
    setHash(code);
    clearDraft();
    localStorage.setItem('dnd.last-campaign', code);
    enterCampaign();
  } catch (e) {
    showSetupError('Network error. Try again.');
    $('#btn-start').disabled = false;
    $('#btn-start').textContent = 'Launch Campaign';
  }
});

// Restore draft on load. Runs last so the + Add Character handler above is
// attached; loadDraft() clicks it to recreate slots 2+.
loadDraft();
