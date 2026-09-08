// ============================================================
// DMs Module: Direct Messaging, Online User Presence & Directory
// ============================================================

import { state, isConversationMuted } from './state.js';
import { makeKeyboardClickable } from './utils.js';

export const userDirectory = new Map();

export function displayNickname(nick) {
  const entry = userDirectory.get(nick);
  return entry?.display_name || nick;
}

export function getOrCreateDm(nick, partnerUserId = null) {
  const convId = nick;
  const existing = state.dms.find(d => d.name === nick);
  if (!existing) {
    const newDm = {
      id: convId,
      name: nick,
      type: 'dm',
      partnerUserId: partnerUserId,
      messages: [],
      messageIds: new Set(),
      unread: 0,
      hasOlder: false,
      loadingOlder: false,
      lastActivityTime: 0,
    };
    state.dms.push(newDm);
    return newDm;
  }
  if (partnerUserId && !existing.partnerUserId) {
    existing.partnerUserId = partnerUserId;
  }
  return existing;
}

export function renderDms(onSwitchConv) {
  const dmListEl = document.getElementById('dm-list');
  if (!dmListEl) return;
  dmListEl.replaceChildren();

  const sorted = [...state.dms].sort((a, b) => {
    const timeA = a.lastActivityTime || 0;
    const timeB = b.lastActivityTime || 0;
    if (timeB !== timeA) return timeB - timeA;
    return String(a.name).localeCompare(String(b.name));
  });

  sorted.forEach(conv => {
    const isActive = state.activeRoom.type === 'dm' && state.activeRoom.id === conv.name;
    const item = document.createElement('li');
    item.className = `sidebar-item conv-item dm-item${isActive ? ' active' : ''}`;
    item.dataset.id = conv.id;

    const icon = document.createElement('span');
    icon.className = 'conv-icon';
    icon.textContent = '👤';

    const name = document.createElement('span');
    name.className = 'sidebar-item-name conv-name';
    name.textContent = displayNickname(conv.name);

    item.append(icon, name);

    const partnerId = conv.partnerUserId || userDirectory.get(conv.name)?.id || conv.name;
    if (isConversationMuted('dm', partnerId)) {
      const muteIcon = document.createElement('span');
      muteIcon.className = 'conv-mute-indicator';
      muteIcon.textContent = '🔕';
      muteIcon.title = '음소거됨';
      item.appendChild(muteIcon);
    }

    const unreadCount = state.unreadCounts.dms[partnerId] ?? state.unreadCounts.dms[conv.name] ?? conv.unread ?? 0;
    if (unreadCount > 0 && !isActive) {
      const badge = document.createElement('span');
      badge.className = 'sidebar-badge conv-unread dm-unread';
      badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
      item.appendChild(badge);
    }

    const activate = () => onSwitchConv('dm', conv.name);
    item.addEventListener('click', activate);
    makeKeyboardClickable(item, activate);
    dmListEl.appendChild(item);
  });
}

export function renderOnlineList(users, onOpenDm) {
  const onlineCountEl = document.getElementById('online-count');
  const userCountEl = document.getElementById('user-count');
  const onlineListEl = document.getElementById('online-list');
  if (!onlineListEl) return;

  const sortedUsers = [...users].sort((first, second) =>
    Number(Boolean(second.online)) - Number(Boolean(first.online))
      || String(first.username || first.nickname).localeCompare(String(second.username || second.nickname))
  );

  if (onlineCountEl) onlineCountEl.textContent = String(sortedUsers.filter(user => user.online).length);
  if (userCountEl) userCountEl.textContent = String(sortedUsers.length);
  onlineListEl.replaceChildren();

  const myNick = state.currentUser ? state.currentUser.username : '';

  sortedUsers.forEach(user => {
    const nick = user.username || user.nickname;
    const displayName = user.display_name || displayNickname(nick);
    const online = Boolean(user.online);
    const item = document.createElement('li');
    item.className = `sidebar-item online-item${online ? '' : ' offline'}`;

    const dot = document.createElement('span');
    dot.className = `online-dot${online ? '' : ' offline'}`;

    const nickElement = document.createElement('span');
    nickElement.className = `sidebar-item-name online-nick${nick === myNick ? ' is-me' : ''}`;
    nickElement.textContent = displayName + (nick === myNick ? ' (나)' : '');

    item.setAttribute('aria-label', `${displayName} ${online ? '온라인' : '오프라인'}`);
    item.title = `${displayName} (@${nick}) — ${online ? '온라인' : '오프라인'}`;
    item.append(dot, nickElement);

    if (user.quiz_badge) {
      const badgeSpan = document.createElement('span');
      badgeSpan.className = `quiz-user-badge badge-${user.quiz_badge.type}`;
      badgeSpan.textContent = `${user.quiz_badge.icon} ${user.quiz_badge.label}`;
      badgeSpan.title = user.quiz_badge.title || '';
      item.appendChild(badgeSpan);
    }

    if (nick !== myNick) {
      const openDm = () => {
        getOrCreateDm(nick, user.id);
        onOpenDm(nick);
      };
      item.title = `${displayName} (@${nick}) — ${online ? '온라인' : '오프라인'} (클릭 시 1:1 대화)`;
      item.addEventListener('click', openDm);
      makeKeyboardClickable(item, openDm);
    }
    onlineListEl.appendChild(item);

  });
}
