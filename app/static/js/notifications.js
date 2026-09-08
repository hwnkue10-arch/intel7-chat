// ============================================================
// Notifications Module: Web Notifications, Snooze & Modals
// ============================================================

import { state, isConversationMuted } from './state.js';
import { getSoundMode, getSoundVolume, setSoundMode, setSoundVolume, playNotificationSound } from './audio.js';
import { showToast } from './utils.js';

function getDesktopNotifKey() {
  return state.currentUser ? `bamboochat_desktop_notif_${state.currentUser.id}` : 'bamboochat_desktop_notif';
}

function getSnoozeKey() {
  return state.currentUser ? `bamboochat_snooze_until_${state.currentUser.id}` : 'bamboochat_snooze_until';
}

export function isDesktopNotificationEnabled() {
  try {
    return localStorage.getItem(getDesktopNotifKey()) === 'enabled';
  } catch {
    return false;
  }
}

export function setDesktopNotificationEnabled(enabled) {
  try {
    localStorage.setItem(getDesktopNotifKey(), enabled ? 'enabled' : 'disabled');
  } catch { /* storage */ }
}

export function getSnoozeUntil() {
  if (!state.currentUser) return 0;
  try {
    const raw = localStorage.getItem(getSnoozeKey());
    return raw ? parseInt(raw, 10) || 0 : 0;
  } catch {
    return 0;
  }
}

export function isSnoozed() {
  const until = getSnoozeUntil();
  if (!until) return false;
  if (Date.now() >= until) {
    clearSnooze();
    return false;
  }
  return true;
}

export function setSnooze(durationMinutes) {
  let until = 0;
  const now = Date.now();
  if (durationMinutes === 'tomorrow') {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    until = d.getTime();
  } else {
    const mins = Number(durationMinutes) || 15;
    until = now + mins * 60 * 1000;
  }
  try {
    localStorage.setItem(getSnoozeKey(), String(until));
  } catch { /* storage */ }
  updateGlobalNotificationUI();
}

export function clearSnooze() {
  try {
    localStorage.removeItem(getSnoozeKey());
  } catch { /* storage */ }
  updateGlobalNotificationUI();
}

export function formatSnoozeRemaining(untilMs) {
  const diffMs = untilMs - Date.now();
  if (diffMs <= 0) return '종료됨';
  const mins = Math.ceil(diffMs / 60000);
  if (mins < 60) return `${mins}분 남음`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins > 0 ? `${hours}시간 ${remMins}분 남음` : `${hours}시간 남음`;
}

export function checkDesktopNotificationContext() {
  const isSecure = window.isSecureContext || ['localhost', '127.0.0.1'].includes(location.hostname);
  const hasSupport = 'Notification' in window;
  let permission = 'unsupported';
  if (hasSupport) {
    try {
      permission = Notification.permission;
    } catch {
      permission = 'unsupported';
    }
  }
  return { isSecure, hasSupport, permission };
}

export function showDesktopNotification({ title, body, conversationId, onSelectConv }) {
  if (!isDesktopNotificationEnabled()) return;
  const { hasSupport, permission } = checkDesktopNotificationContext();
  if (!hasSupport || permission !== 'granted') return;

  try {
    const notif = new Notification(title || 'BambooChat', {
      body: body || '',
      icon: '/favicon.ico',
      tag: conversationId || 'bamboochat-msg',
    });
    notif.onclick = () => {
      window.focus();
      if (conversationId && onSelectConv) {
        onSelectConv(conversationId);
      }
      notif.close();
    };
  } catch {
    // Suppressed or failed
  }
}

export function showMessageToast({ kind, conversationId, title, body, tone = 'info', onSelectConv }) {
  const toastRegion = document.getElementById('toast-region');
  if (!toastRegion) return;

  const toast = document.createElement('div');
  toast.className = `toast message-toast ${tone}`;
  
  const headerRow = document.createElement('div');
  headerRow.className = 'toast-header-row';

  const titleEl = document.createElement('div');
  titleEl.className = 'toast-title';
  titleEl.textContent = title || '새 메시지';

  const badgeEl = document.createElement('span');
  badgeEl.className = 'toast-badge';
  badgeEl.textContent = kind === 'dm' ? 'DM' : (kind === 'mention' ? '@멘션' : (kind === 'reply' ? '답장' : '알림'));

  headerRow.append(titleEl, badgeEl);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'toast-body';
  bodyEl.textContent = body || '';

  const hintEl = document.createElement('div');
  hintEl.className = 'toast-hint';
  hintEl.textContent = '클릭하여 해당 대화방으로 이동';

  toast.append(headerRow, bodyEl, hintEl);

  toast.addEventListener('click', () => {
    toast.remove();
    if (conversationId && onSelectConv) {
      onSelectConv(conversationId);
    }
  });

  toastRegion.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(20px)';
    toast.style.transition = 'opacity 0.25s, transform 0.25s';
    setTimeout(() => toast.remove(), 260);
  }, 4800);
}

export function isChatActiveAndFocused() {
  return typeof document !== 'undefined'
    && document.visibilityState === 'visible'
    && (typeof document.hasFocus !== 'function' || document.hasFocus());
}

export function emitAttention({
  kind = 'ordinary',
  conversationId = null,
  title = '',
  body = '',
  isHistory = false,
  isOwnMessage = false,
  onSelectConv = null,
}) {
  if (isOwnMessage || isHistory) return;

  const activeConversationId = `${state.activeRoom.type}:${state.activeRoom.id}`;
  const isCurrentActive = Boolean(conversationId && conversationId === activeConversationId) && isChatActiveAndFocused();
  const separator = conversationId ? conversationId.indexOf(':') : -1;
  const conversationType = separator > 0 ? conversationId.slice(0, separator) : '';
  const conversationKey = separator > 0 ? conversationId.slice(separator + 1) : '';
  const isMuted = Boolean(conversationType && conversationKey
    && isConversationMuted(conversationType, conversationKey));
  const snoozed = isSnoozed();

  if (isCurrentActive || isMuted || snoozed) return;

  const isImportant = (kind === 'dm' || kind === 'mention' || kind === 'reply');
  const soundMode = getSoundMode();

  if (soundMode === 'all' || (soundMode === 'important' && isImportant)) {
    playNotificationSound();
  }

  if (title || body) {
    const toastTone = (kind === 'mention' || kind === 'reply') ? 'mention' : (kind === 'dm' ? 'dm' : 'info');
    showMessageToast({
      kind,
      conversationId,
      title,
      body,
      tone: toastTone,
      onSelectConv,
    });
  }

  if (isImportant && isDesktopNotificationEnabled()) {
    showDesktopNotification({ title, body, conversationId, onSelectConv });
  }
}

export function updateGlobalNotificationUI() {
  if (!state.currentUser) return;

  const soundModeInputs = document.querySelectorAll('input[name="sound-mode"]');
  const soundVolumeSlider = document.getElementById('sound-volume-slider');
  const soundVolumeVal = document.getElementById('sound-volume-val');
  const desktopNotifStatusBadge = document.getElementById('desktop-notif-status-badge');
  const desktopNotifToggleBtn = document.getElementById('desktop-notif-toggle-btn');
  const desktopNotifHint = document.getElementById('desktop-notif-hint');
  const snoozeStatusBadge = document.getElementById('snooze-status-badge');
  const snoozeResumeBtn = document.getElementById('snooze-resume-btn');
  const snoozeButtons = document.querySelectorAll('.snooze-btn');

  const currentSoundMode = getSoundMode();
  soundModeInputs.forEach(input => {
    input.checked = input.value === currentSoundMode;
  });

  const vol = getSoundVolume();
  if (soundVolumeSlider) soundVolumeSlider.value = String(Math.round(vol * 100));
  if (soundVolumeVal) soundVolumeVal.textContent = `${Math.round(vol * 100)}%`;

  const { isSecure, hasSupport, permission } = checkDesktopNotificationContext();
  const enabled = isDesktopNotificationEnabled();

  if (desktopNotifStatusBadge) {
    desktopNotifStatusBadge.className = 'notif-badge';
    if (!hasSupport) {
      desktopNotifStatusBadge.textContent = '브라우저 미지원';
    } else if (permission === 'denied') {
      desktopNotifStatusBadge.textContent = '권한 차단됨';
      desktopNotifStatusBadge.classList.add('denied');
    } else if (permission === 'granted') {
      desktopNotifStatusBadge.textContent = enabled ? '활성화됨 (허용됨)' : '꺼짐 (권한 허용됨)';
      if (enabled) desktopNotifStatusBadge.classList.add('granted');
    } else {
      desktopNotifStatusBadge.textContent = '권한 필요';
    }
  }

  if (desktopNotifToggleBtn) {
    if (!hasSupport) {
      desktopNotifToggleBtn.disabled = true;
      desktopNotifToggleBtn.textContent = '지원하지 않음';
    } else if (permission === 'denied') {
      desktopNotifToggleBtn.disabled = true;
      desktopNotifToggleBtn.textContent = '브라우저에서 차단됨';
    } else if (permission === 'granted') {
      desktopNotifToggleBtn.disabled = false;
      desktopNotifToggleBtn.textContent = enabled ? '데스크톱 알림 끄기' : '데스크톱 알림 켜기';
      desktopNotifToggleBtn.className = enabled ? 'caution-btn' : 'primary-btn';
    } else {
      desktopNotifToggleBtn.disabled = false;
      desktopNotifToggleBtn.textContent = '데스크톱 알림 허용하기';
      desktopNotifToggleBtn.className = 'primary-btn';
    }
  }

  if (desktopNotifHint) {
    desktopNotifHint.classList.toggle('hidden', isSecure);
  }

  const snoozed = isSnoozed();
  const until = getSnoozeUntil();
  if (snoozeStatusBadge) {
    snoozeStatusBadge.className = `snooze-badge ${snoozed ? 'active' : 'inactive'}`;
    snoozeStatusBadge.textContent = snoozed ? `방해 금지 중 (${formatSnoozeRemaining(until)})` : '알림 수신 중';
  }
  if (snoozeResumeBtn) {
    snoozeResumeBtn.classList.toggle('hidden', !snoozed);
  }
  snoozeButtons.forEach(btn => {
    btn.classList.toggle('active', false);
  });
}

export function openGlobalNotificationModal() {
  const globalNotificationModal = document.getElementById('global-notification-modal');
  if (!globalNotificationModal || !state.currentUser) return;
  updateGlobalNotificationUI();
  globalNotificationModal.classList.remove('hidden');
}

export function closeGlobalNotificationModal() {
  const globalNotificationModal = document.getElementById('global-notification-modal');
  if (globalNotificationModal) globalNotificationModal.classList.add('hidden');
}

export function updateConversationNotificationUI() {
  const isMuted = isConversationMuted(state.activeRoom.type, state.activeRoom.id);
  const convNotifRoomName = document.getElementById('conv-notif-room-name');
  const convNotifStatusDesc = document.getElementById('conv-notif-status-desc');
  const convNotifStatusIcon = document.getElementById('conv-notif-status-icon');
  const convNotifToggleBtn = document.getElementById('conv-notif-toggle-btn');
  const convMuteBtn = document.getElementById('conv-mute-btn');

  if (convMuteBtn) {
    convMuteBtn.textContent = isMuted ? '🔕' : '🔔';
    convMuteBtn.title = isMuted ? '알림 설정 (음소거됨)' : '알림 설정 (켜짐)';
    convMuteBtn.classList.toggle('is-muted', isMuted);
  }

  if (convNotifRoomName) convNotifRoomName.textContent = `# ${state.activeRoom.id}`;
  if (convNotifStatusIcon) convNotifStatusIcon.textContent = isMuted ? '🔕' : '🔔';
  if (convNotifStatusDesc) {
    convNotifStatusDesc.textContent = isMuted
      ? '이 대화방의 모든 알림과 소리가 꺼져 있습니다.'
      : '이 대화방의 새 메시지 수신 시 알림을 받습니다.';
  }
  if (convNotifToggleBtn) {
    convNotifToggleBtn.textContent = isMuted ? '음소거 해제' : '대화방 음소거';
    convNotifToggleBtn.className = isMuted ? 'primary-btn notif-toggle-action-btn' : 'danger-btn notif-toggle-action-btn';
  }
}

export function openConvNotificationModal() {
  const convNotificationModal = document.getElementById('conv-notification-modal');
  if (!convNotificationModal || !state.currentUser) return;
  updateConversationNotificationUI();
  convNotificationModal.classList.remove('hidden');
}

export function closeConvNotificationModal() {
  const convNotificationModal = document.getElementById('conv-notification-modal');
  if (convNotificationModal) convNotificationModal.classList.add('hidden');
}

export function updateDocumentTitle(totalUnread = 0) {
  if (!state.currentUser) {
    document.title = 'BambooChat';
    return;
  }
  document.title = totalUnread > 0 ? `(${totalUnread}) BambooChat` : 'BambooChat';
}
