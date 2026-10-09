// Boot.  Loaded last, once every other file has defined its functions.
'use strict';

// Panel toggles, called from onclick="DND.…" attributes in index.html.
window.DND = {
  toggleMemory: function() {
    memoryCollapsed = !memoryCollapsed;
    $('#memory-chevron').textContent = memoryCollapsed ? '▶' : '▼';
    if (campaignState) renderMemory(campaignState);
  },
  toggleArch: function() {
    archCollapsed = !archCollapsed;
    $('#arch-chevron').textContent = archCollapsed ? '▶' : '▼';
    $('#arch-body').classList.toggle('collapsed', archCollapsed);
    if (!archCollapsed && campaignState) renderArchitecture(campaignState);
  },
  toggleContextBreakdown: function() {
    contextBreakdownOpen = !contextBreakdownOpen;
    $('#context-breakdown').classList.toggle('open', contextBreakdownOpen);
    if (contextBreakdownOpen && campaignState) renderContextBreakdown(campaignState);
  }
};

// ----------------------------------------------------------------
// Boot
// ----------------------------------------------------------------
const hashCode = readHash();
if (hashCode) {
  code = hashCode;
  enterCampaign();
}
