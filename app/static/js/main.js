// ============================================================
// Classroom Chat — Master Javascript Entrypoint
// Native ES Module Architecture
// ============================================================

import { state, setCurrentUser, setActiveRoom } from './state.js';
import { showToast } from './utils.js';
import { initAuthListeners, bootstrapAuth, updateNickBadge, showNicknameHint, refreshStorageWarning, showAuthModal } from './auth.js';
import { loadChannels, renderChannels, initChannelsListeners, channelsDirectory } from './channels.js';
import { renderDms, displayNickname, getOrCreateDm } from './dm.js';
import {
  loadDrafts,
  saveCurrentDraft,
  clearCurrentDraft,
  loadActiveDraft,
  resizeComposer,
  updateCharCount,
  renderComposerPreviews,
  renderMessages,
  appendMessageNode,
  updateMessageInDOM,
  updateMessageHiddenInDOM,
  removeMessageFromDOM,
  applyAttachmentDeletedInDOM,
  initChatListeners,
  replyTargets,
  pendingAttachments,
} from './chat.js';
import { initWebSocket, sendWebSocketMessage } from './ws.js';
import { initSearchListeners, showSearchHint } from './search.js';
import { fetchActivePinnedMessages, initPinsListeners } from './pins.js';
import {
  initQuizListeners,
  refreshQuizHeaderStreak,
  refreshQuizSidebarCounts,
  handleLeaderboardInvalidated,
} from './quiz.js';
import { initChessListeners } from './chess.js';
import {
  openGlobalNotificationModal,
  closeGlobalNotificationModal,
  openConvNotificationModal,
  closeConvNotificationModal,
  updateConversationNotificationUI,
  updateGlobalNotificationUI,
  setSnooze,
  clearSnooze,
  checkDesktopNotificationContext,
  setDesktopNotificationEnabled,
  isDesktopNotificationEnabled,
} from './notifications.js';
import { playNotificationSound, setSoundMode, setSoundVolume } from './audio.js';

function syncTouchableViewport() {
  const viewportHeight = window.visualViewport?.height || window.innerHeight;
  if (viewportHeight > 0) {
    document.documentElement.style.setProperty('--app-height', `${Math.round(viewportHeight)}px`);
  }
}

function initTouchableViewport() {
  syncTouchableViewport();
  window.addEventListener('resize', syncTouchableViewport, { passive: true });
  window.addEventListener('orientationchange', syncTouchableViewport, { passive: true });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', syncTouchableViewport, { passive: true });
    window.visualViewport.addEventListener('scroll', syncTouchableViewport, { passive: true });
  }
}


// --- Room Switching ---
async function acknowledgeConversation(type, id, lastReadMessageId) {
  const numericId = Number(String(lastReadMessageId || '').replace(/^(public|dm):/, ''));
  if (!Number.isInteger(numericId) || numericId <= 0) return;

  // Do not let a slow history request acknowledge a room the user already left.
  if (state.activeRoom.type !== type || String(state.activeRoom.id) !== String(id)) return;

  try {
    const res = await fetch('/api/read-states/ack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_type: type,
        conversation_id: id,
        last_read_message_id: numericId,
      }),
    });
    if (!res.ok) throw new Error(`ACK failed with status ${res.status}`);
  } catch (err) {
    console.warn('Failed to acknowledge conversation read state.', err);
  }
}

async function switchConversation(type, id) {
  saveCurrentDraft();
  setActiveRoom(type, id);

  const chatAreaTitle = document.getElementById('chat-area-title');
  const chatAreaDesc = document.getElementById('chat-area-desc');
  const msgInput = document.getElementById('msg-input');
  const channelSettingsBtn = document.getElementById('channel-settings-btn');
  const loadOlderBtn = document.getElementById('load-older-btn');

  if (type === 'channel') {
    const chanId = Number(id);
    const chanObj = channelsDirectory.get(chanId);
    const displayName = chanObj?.display_name || (chanId === 1 ? '전체 채팅' : `채널 ${chanId}`);
    if (chatAreaTitle) chatAreaTitle.textContent = `# ${displayName}`;
    if (chatAreaDesc) chatAreaDesc.textContent = chanObj?.description || '';
    if (msgInput) msgInput.placeholder = `# ${displayName}에 메시지 입력`;
    if (channelSettingsBtn) {
      channelSettingsBtn.classList.toggle('hidden', state.currentUser?.role !== 'admin');
    }
  } else {
    const displayName = displayNickname(String(id));
    if (chatAreaTitle) chatAreaTitle.textContent = `💬 ${displayName}`;
    if (chatAreaDesc) chatAreaDesc.textContent = '';
    if (msgInput) msgInput.placeholder = `${displayName}에게 DM 보내기`;
    if (channelSettingsBtn) channelSettingsBtn.classList.add('hidden');
  }

  loadActiveDraft();
  renderComposerPreviews();
  updateConversationNotificationUI();
  renderChannels(switchConversation);
  renderDms(switchConversation);

  // Fetch conversation messages
  try {
    const endpoint = type === 'channel'
      ? (String(id) === '1' ? `/api/history/public` : `/api/channels/${id}/messages`)
      : `/api/history/dm/${encodeURIComponent(id)}`;
    const res = await fetch(endpoint, { cache: 'no-store' });
    if (res.ok) {
      const data = await res.json();
      if (state.activeRoom.type !== type || String(state.activeRoom.id) !== String(id)) return;
      const rawMessages = Array.isArray(data.messages) ? data.messages : [];
      const messages = rawMessages.map(m => ({
        ...m,
        msgType: type === 'channel' ? 'chat' : 'dm'
      }));
      renderMessages(messages);
      activeHasMore = Boolean(data.has_more);
      if (loadOlderBtn) {
        loadOlderBtn.classList.toggle('hidden', !data.has_more);
      }
      const newestMessage = rawMessages.reduce((latest, message) => {
        const messageId = Number(String(message.message_id || '').replace(/^(public|dm):/, ''));
        return Number.isInteger(messageId) && messageId > latest ? messageId : latest;
      }, 0);
      if (newestMessage) acknowledgeConversation(type, id, newestMessage);
    }
  } catch { /* ignore */ }



  fetchActivePinnedMessages({ autoOpen: false });

  // Close mobile sidebar if open
  const sidebar = document.getElementById('sidebar');
  const sidebarBackdrop = document.getElementById('sidebar-backdrop');
  if (sidebar && sidebar.classList.contains('open')) {
    sidebar.classList.remove('open');
    if (sidebarBackdrop) sidebarBackdrop.classList.add('hidden');
  }

  msgInput?.focus();
}

// --- Outbound Message Sender ---
function handleSendMessage() {
  const msgInput = document.getElementById('msg-input');
  if (!msgInput) return;
  const content = msgInput.value.trim();
  const currentKey = `${state.activeRoom.type}:${state.activeRoom.id}`;
  const states = pendingAttachments.get(currentKey) || [];

  if (states.some(st => ['queued', 'uploading'].includes(st.status))) {
    showToast('모든 파일 업로드가 끝날 때까지 기다려 주세요.', 'warning');
    return;
  }
  if (states.some(st => st.status === 'error')) {
    showToast('실패한 첨부 파일을 제거해 주세요.', 'error');
    return;
  }

  const readyAttachments = states.filter(st => st.status === 'ready' && st.meta);
  if (!content && readyAttachments.length === 0) return;

  const payload = {
    type: state.activeRoom.type === 'dm' ? 'dm' : 'chat',
    content,
  };
  if (state.activeRoom.type === 'dm') {
    payload.to = state.activeRoom.id;
  } else {
    payload.channel_id = Number(state.activeRoom.id);
  }
  if (readyAttachments.length) {
    payload.attachment_ids = readyAttachments.map(st => st.meta.id);
  }
  const reply = replyTargets.get(currentKey);
  if (reply) payload.reply = reply;

  const sent = sendWebSocketMessage(payload);
  if (sent) {
    msgInput.value = '';
    clearCurrentDraft(currentKey);
    replyTargets.delete(currentKey);
    pendingAttachments.delete(currentKey);
    resizeComposer();
    updateCharCount();
    renderComposerPreviews();
    msgInput.focus();
  }
}

// --- Load Older Messages ---
let loadingOlder = false;
let activeHasMore = false;

async function loadOlderMessages() {
  if (loadingOlder || !activeHasMore) return;
  const messageListEl = document.getElementById('message-list');
  const loadOlderBtn = document.getElementById('load-older-btn');
  const firstRow = messageListEl?.querySelector('.msg-row[data-message-id]');
  const firstId = firstRow?.dataset?.messageId;
  if (!firstId) return;

  // Extract numeric id
  const numericId = firstId.includes(':') ? firstId.split(':')[1] : firstId;
  if (!numericId) return;

  loadingOlder = true;
  if (loadOlderBtn) { loadOlderBtn.disabled = true; loadOlderBtn.textContent = '불러오는 중...'; }

  try {
    const { type, id } = state.activeRoom;
    const basePath = type === 'channel'
      ? (String(id) === '1' ? `/api/history/public` : `/api/channels/${id}/messages`)
      : `/api/history/dm/${encodeURIComponent(id)}`;
    const res = await fetch(`${basePath}?before_id=${numericId}`, { cache: 'no-store' });
    if (!res.ok) throw new Error();
    const data = await res.json();
    const rawMessages = Array.isArray(data.messages) ? data.messages : [];
    const messages = rawMessages.map(m => ({ ...m, msgType: type === 'channel' ? 'chat' : 'dm' }));

    if (messages.length && messageListEl) {
      const prevHeight = messageListEl.scrollHeight;
      const prevTop = messageListEl.scrollTop;
      // Prepend older messages before the first existing .msg-row
      const loadOlderBtnEl = messageListEl.querySelector('#load-older-btn');
      const insertAnchor = messageListEl.querySelector('.msg-row') || null;
      // Render from oldest to newest at the top
      const frag = document.createDocumentFragment();
      const tempDiv = document.createElement('div');
      messages.forEach(msg => appendMessageNode(msg, null)); // appended temporarily
      // They got appended at end, so move them to before insertAnchor
      const newRows = [...messageListEl.querySelectorAll('.msg-row')]
        .slice(-(messages.length));
      newRows.reverse().forEach(row => {
        messageListEl.insertBefore(row, insertAnchor);
      });
      messageListEl.scrollTop = prevTop + (messageListEl.scrollHeight - prevHeight);
    }

    activeHasMore = Boolean(data.has_more);
    if (loadOlderBtn) loadOlderBtn.classList.toggle('hidden', !activeHasMore);
  } catch {
    showToast('이전 메시지를 불러오지 못했습니다.', 'error');
  } finally {
    loadingOlder = false;
    if (loadOlderBtn) { loadOlderBtn.disabled = false; loadOlderBtn.textContent = '↑ 이전 메시지 더 불러오기'; }
  }
}

// --- App Bootstrap ---
function initApp() {
  initTouchableViewport();
  const sidebarToggle = document.getElementById('sidebar-toggle');

  const sidebar = document.getElementById('sidebar');
  const sidebarBackdrop = document.getElementById('sidebar-backdrop');
  const headerNotifBtn = document.getElementById('header-notif-btn');
  const convMuteBtn = document.getElementById('conv-mute-btn');
  const globalNotificationModalClose = document.getElementById('global-notification-modal-close');
  const globalNotificationModalCancel = document.getElementById('global-notification-modal-cancel');
  const convNotificationModalClose = document.getElementById('conv-notification-modal-close');
  const convNotificationModalCancel = document.getElementById('conv-notification-modal-cancel');
  const convNotificationToggleBtn = document.getElementById('conv-notif-toggle-btn');
  const soundModeInputs = document.querySelectorAll('input[name="sound-mode"]');
  const soundVolumeSlider = document.getElementById('sound-volume-slider');
  const soundTestBtn = document.getElementById('sound-test-btn');
  const desktopNotifToggleBtn = document.getElementById('desktop-notif-toggle-btn');
  const snoozeButtons = document.querySelectorAll('.snooze-btn');
  const snoozeResumeBtn = document.getElementById('snooze-resume-btn');

  // 1. Mobile Sidebar toggle
  if (sidebarToggle && sidebar && sidebarBackdrop) {
    sidebarToggle.addEventListener('click', () => {
      const open = sidebar.classList.toggle('open');
      sidebarBackdrop.classList.toggle('hidden', !open);
    });
    sidebarBackdrop.addEventListener('click', () => {
      sidebar.classList.remove('open');
      sidebarBackdrop.classList.add('hidden');
    });
  }

  // 2. Notification Modals & Settings
  if (headerNotifBtn) headerNotifBtn.addEventListener('click', openGlobalNotificationModal);
  if (globalNotificationModalClose) globalNotificationModalClose.addEventListener('click', closeGlobalNotificationModal);
  if (globalNotificationModalCancel) globalNotificationModalCancel.addEventListener('click', closeGlobalNotificationModal);

  if (convMuteBtn) convMuteBtn.addEventListener('click', openConvNotificationModal);
  if (convNotificationModalClose) convNotificationModalClose.addEventListener('click', closeConvNotificationModal);
  if (convNotificationModalCancel) convNotificationModalCancel.addEventListener('click', closeConvNotificationModal);

  if (convNotificationToggleBtn) {
    convNotificationToggleBtn.addEventListener('click', async () => {
      const key = `${state.activeRoom.type}:${state.activeRoom.id}`;
      const isMuted = state.mutedConversations.has(key);
      const newMuted = !isMuted;
      try {
        const res = await fetch('/api/read-states/mute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            conversation_type: state.activeRoom.type,
            conversation_id: state.activeRoom.id,
            muted: newMuted,
          }),
        });
        if (res.ok) {
          if (newMuted) state.mutedConversations.add(key);
          else state.mutedConversations.delete(key);
          updateConversationNotificationUI();
          renderChannels(switchConversation);
          renderDms(switchConversation);
          showToast(newMuted ? '대화방을 음소거했습니다.' : '대화방 음소거를 해제했습니다.', 'info');
        }
      } catch { /* ignore */ }
    });
  }

  if (soundModeInputs) {
    soundModeInputs.forEach(input => {
      input.addEventListener('change', () => {
        setSoundMode(input.value);
      });
    });
  }

  if (soundVolumeSlider) {
    soundVolumeSlider.addEventListener('input', () => {
      const val = Number(soundVolumeSlider.value) / 100;
      setSoundVolume(val);
      const soundVolumeVal = document.getElementById('sound-volume-val');
      if (soundVolumeVal) soundVolumeVal.textContent = `${soundVolumeSlider.value}%`;
    });
  }

  if (soundTestBtn) {
    soundTestBtn.addEventListener('click', () => playNotificationSound(true));
  }

  if (desktopNotifToggleBtn) {
    desktopNotifToggleBtn.addEventListener('click', async () => {
      const { hasSupport, permission } = checkDesktopNotificationContext();
      if (!hasSupport) return;
      if (permission === 'default') {
        try {
          const res = await Notification.requestPermission();
          if (res === 'granted') setDesktopNotificationEnabled(true);
        } catch { /* ignore */ }
      } else if (permission === 'granted') {
        setDesktopNotificationEnabled(!isDesktopNotificationEnabled());
      }
      updateGlobalNotificationUI();
    });
  }

  if (snoozeButtons) {
    snoozeButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        setSnooze(btn.dataset.snooze);
      });
    });
  }

  if (snoozeResumeBtn) {
    snoozeResumeBtn.addEventListener('click', clearSnooze);
  }

  // --- Sidebar Section Collapse & Draggable Sliders ---
  const SIDEBAR_SIZES_STORAGE_KEY = 'bamboochat_sidebar_sizes';
  const SIDEBAR_COLLAPSED_STORAGE_KEY = 'bamboochat_sidebar_collapsed';

  function initSidebarSections() {
    const toggleButtons = document.querySelectorAll('.section-toggle-btn');
    toggleButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        const section = btn.closest('.sidebar-section');
        if (!section) return;
        const isCollapsed = section.classList.toggle('collapsed');
        btn.setAttribute('aria-expanded', String(!isCollapsed));
        if (!isCollapsed) {
          try {
            const sizes = JSON.parse(localStorage.getItem(SIDEBAR_SIZES_STORAGE_KEY) || '{}');
            const secName = section.dataset.section;
            if (secName && sizes[secName] && sizes[secName] >= 50) {
              section.style.flex = `0 0 ${sizes[secName]}px`;
            }
          } catch { /* storage */ }
        }
        saveSidebarState();
      });
    });

    const sliders = document.querySelectorAll('.sidebar-slider');
    sliders.forEach(slider => {
      slider.addEventListener('pointerdown', event => {
        const prevSection = slider.previousElementSibling;
        const nextSection = slider.nextElementSibling;
        if (!prevSection || !nextSection) return;
        if (prevSection.classList.contains('collapsed') || nextSection.classList.contains('collapsed')) return;

        event.preventDefault();
        slider.classList.add('is-dragging');
        slider.setPointerCapture(event.pointerId);

        const startY = event.clientY;
        const startPrevHeight = prevSection.getBoundingClientRect().height;
        const startNextHeight = nextSection.getBoundingClientRect().height;

        const onPointerMove = moveEvent => {
          const deltaY = moveEvent.clientY - startY;
          const newPrevHeight = Math.max(50, startPrevHeight + deltaY);
          const newNextHeight = Math.max(50, startNextHeight - deltaY);
          prevSection.style.flex = `0 0 ${newPrevHeight}px`;
          nextSection.style.flex = `0 0 ${newNextHeight}px`;
        };

        const onPointerUp = upEvent => {
          slider.classList.remove('is-dragging');
          try { slider.releasePointerCapture(upEvent.pointerId); } catch { /* ignore */ }
          window.removeEventListener('pointermove', onPointerMove);
          window.removeEventListener('pointerup', onPointerUp);
          saveSidebarState();
        };

        window.addEventListener('pointermove', onPointerMove);
        window.addEventListener('pointerup', onPointerUp);
      });
    });

    restoreSidebarState();
  }

  function saveSidebarState() {
    try {
      const collapsed = [];
      const sizes = {};
      document.querySelectorAll('.sidebar-section').forEach(sec => {
        const secName = sec.dataset.section;
        if (!secName) return;
        if (sec.classList.contains('collapsed')) {
          collapsed.push(secName);
        } else {
          sizes[secName] = sec.getBoundingClientRect().height;
        }
      });
      localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, JSON.stringify(collapsed));
      localStorage.setItem(SIDEBAR_SIZES_STORAGE_KEY, JSON.stringify(sizes));
    } catch { /* storage */ }
  }

  function restoreSidebarState() {
    try {
      const collapsed = JSON.parse(localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) || '[]');
      const sizes = JSON.parse(localStorage.getItem(SIDEBAR_SIZES_STORAGE_KEY) || '{}');

      document.querySelectorAll('.sidebar-section').forEach(sec => {
        const secName = sec.dataset.section;
        if (!secName) return;
        const btn = sec.querySelector('.section-toggle-btn');
        if (collapsed.includes(secName)) {
          sec.classList.add('collapsed');
          if (btn) btn.setAttribute('aria-expanded', 'false');
        } else if (sizes[secName] && sizes[secName] >= 50) {
          sec.style.flex = `0 0 ${sizes[secName]}px`;
        }
      });
    } catch { /* storage */ }
  }

  // 3. Initialize modules
  initChatListeners(handleSendMessage);
  initChannelsListeners(switchConversation);
  initPinsListeners();
  initQuizListeners();
  initChessListeners();
  initSearchListeners(switchConversation);
  initSidebarSections();

  // 4. Load older messages button
  const loadOlderBtn = document.getElementById('load-older-btn');
  if (loadOlderBtn) loadOlderBtn.addEventListener('click', loadOlderMessages);


  const onLoginSuccess = async (user) => {
    const chatApp = document.getElementById('chat-app');
    const adminBtn = document.getElementById('admin-btn');
    const quizNavAdminBtn = document.getElementById('quiz-nav-admin-btn');
    if (chatApp) chatApp.classList.remove('hidden');
    if (adminBtn) adminBtn.classList.toggle('hidden', user?.role !== 'admin');
    if (quizNavAdminBtn) quizNavAdminBtn.classList.toggle('hidden', user?.role !== 'admin');
    updateNickBadge();
    loadDrafts();
    await loadChannels(switchConversation);
    await switchConversation('channel', 1);
    initWebSocket({
      onSwitchConv: (convKey) => {
        const [type, ...rest] = convKey.split(':');
        switchConversation(type, rest.join(':'));
      },
      onOpenDm: (nick) => switchConversation('dm', nick),
      onDmsUpdated: () => renderDms(switchConversation),
      onUnreadUpdated: () => {
        renderChannels(switchConversation);
        renderDms(switchConversation);
      },
      onMuteUpdated: () => {
        updateConversationNotificationUI();
        renderChannels(switchConversation);
        renderDms(switchConversation);
      },
      onChannelUpdated: () => renderChannels(switchConversation),
      onMessageEdited: (msg) => updateMessageInDOM(msg),
      onMessageHidden: (msg, isHidden) => updateMessageHiddenInDOM(msg, isHidden),
      onMessageMoved: (msgId) => removeMessageFromDOM(msgId),
      onAttachmentDeleted: (attachmentId) => applyAttachmentDeletedInDOM(attachmentId),
      onChannelArchived: (channelId) => {
        if (state.activeRoom.type === 'channel' && String(state.activeRoom.id) === String(channelId)) {
          switchConversation('channel', 1);
        }
      },
      onNewMessage: (message) => {
        const messageType = message.msgType === 'dm' ? 'dm' : 'channel';
        const isActive = messageType === 'channel'
          ? state.activeRoom.type === 'channel' && String(state.activeRoom.id) === String(message.channel_id)
          : state.activeRoom.type === 'dm'
            && [message.from_nick, message.to_nick].includes(String(state.activeRoom.id));
        if (isActive) {
          acknowledgeConversation(state.activeRoom.type, state.activeRoom.id, message.message_id);
        }
      },
      onLeaderboardInvalidated: (event) => handleLeaderboardInvalidated(event),
      onSessionExpired: () => {
        setCurrentUser(null);
        if (state.socket) state.socket = null;
        showAuthModal('세션이 만료되었습니다. 다시 로그인해 주세요.');
      },
    });
    showNicknameHint();
    refreshQuizHeaderStreak();
    refreshQuizSidebarCounts();
    refreshStorageWarning();
    setTimeout(showSearchHint, 12000);
  };



  const onLogout = () => {
    const chatApp = document.getElementById('chat-app');
    if (chatApp) chatApp.classList.add('hidden');
    if (state.socket) {
      state.socket.close();
      state.socket = null;
    }
  };

  initAuthListeners(onLoginSuccess, onLogout);
  bootstrapAuth(onLoginSuccess);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}

