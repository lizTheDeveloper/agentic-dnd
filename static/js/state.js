// Shared state for the whole page.
//
// Every file in static/js/ is a plain <script>, loaded in the order listed
// in index.html.  Top-level const/let/function declarations are shared
// across all of them, so this file declares the constants, the mutable
// state, and the tiny DOM helpers ($, $$, esc, toast) everything else uses.
'use strict';

const BASE = '/tools/agentic-dnd';
const PLAYER_COLORS = ['#6ba5e7','#e76b6b','#6be7a7','#e7c86b','#c76be7'];
const FACT_CATEGORIES = ['all','inventory','location','event','relationship','world'];
const TOOL_ACCENT_MAP = {
  roll_dice: 'dice', resolve_action: 'action',
  remember_fact: 'memory', recall_facts: 'memory',
  take_note: 'notes', read_notes: 'notes',
  set_scene: 'scene', create_npc: 'npc', view_npc_sheet: 'npc',
  lookup_rule: 'rule',
};

// Fantasy emoji set for character picker
const CHARACTER_EMOJIS = [
  '⚔️','🛡️','🏹','🔮','🗡️',
  '🧙','🧝','🧜','🦸','🎢',
  '🐉','🐺','🦅','💀','👻',
  '🎭','🔥','✨','🌙','💎',
  '🏺','🗝️','🧡','⚡','🌿',
];
const DM_EMOJIS = [
  '🎲','📜','🗺️','🕰️','🎭',
  '👁️','🧠','🔮','⚖️','📖',
];

// State
let code = null;
let lastSeq = -1;
let pollTimer = null;
let advancing = false;
let autoplayTimer = null;
let charCount = 1;
let campaignState = null;
let renderedLogCount = 0;
let userScrolled = false;
let activeWhisperAgent = null;
let expandedAgentCard = null;
let memoryCollapsed = false;
let activeMemoryFilter = 'all';
let archCollapsed = true;
let contextBreakdownOpen = false;

// Rulebook state
let rulebookChunks = [];
let chunkStrategy = 'header';
let chunkFixedSize = 500;
let dmEmoji = '🎲';
let charEmojis = {}; // index -> emoji

// Prefs from localStorage
let xrayMode = localStorage.getItem('dnd-xray') === 'true';
let autoplayOn = localStorage.getItem('dnd-autoplay') === 'true';
let autoplaySpeed = parseInt(localStorage.getItem('dnd-speed') || '5', 10);

// DOM helpers
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function esc(s) {
  const d = document.createElement('span');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2000);
}

function fmtNum(n) {
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

// ----------------------------------------------------------------
// URL hash — preserve campaign code across refresh
// ----------------------------------------------------------------
function readHash() {
  const h = location.hash.replace('#', '').trim().toUpperCase();
  if (h.length === 6) return h;
  try {
    const last = localStorage.getItem('dnd.last-campaign');
    if (last && last.length === 6) return last;
  } catch (_) {}
  return null;
}

function setHash(c) {
  history.replaceState(null, '', '#' + c);
}
