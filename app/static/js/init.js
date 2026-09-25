"use strict";


//  Init

document.addEventListener("DOMContentLoaded", function() {
  initAccessibility(window.BC_ACCESSIBILITY || {});
  initWarningBanner();
  initFlashMessages();
  applyMarkdownToExistingMessages();
  enhanceAssistantTools(document);
  initVoiceInput();
  initChat();
  initChatRename();
  initChatShare();
  initChatDelete();
  initChatDownload();
  initHeroModelPicker();
  initInputModelPicker();
  initSidebarModelPicker();
  initParamsPanel();
  initSidebarSearch();
  initPlayground();
  initAdminStatusPolling();
  initConfirmForms();
  initNewChatForm();
  initTokenRename();
  initSidebarContextMenus();
  initAdminResetPassword();
  initNavHamburger();
  initSidebarDrawer();
  initIncognitoClose();
  initMusicPlayer();
  // Ensure hidden model input reflects persisted preference on page load
  var _saved = localStorage.getItem(MODEL_PREF_KEY) || "auto";
  var _mh = document.getElementById("model-select-chat");
  if (_mh) _mh.value = _saved;
  var _eml = document.getElementById("empty-model-label");
  if (_eml) {
    var _im = _getInitialModel();
    _eml.textContent = _im.label;
  }
});
