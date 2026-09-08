// ============================================================
// WebSocket Module: Connection, Reconnection & Message Router
// ============================================================

import { state, setMutedConversations } from './state.js';
import { showToast } from './utils.js';
import { updateMessageReactionsInDOM } from './reactions.js';
import { fetchActivePinnedMessages } from './pins.js';
import { emitAttention, updateDocumentTitle, isChatActiveAndFocused } from './notifications.js';
import { displayNickname, renderOnlineList, getOrCreateDm, userDirectory } from './dm.js';

import { getOrCreateChannel, channelsDirectory } from './channels.js';
import { appendMessageNode, setMentionUsers, refreshRenderedAuthorNames } from './chat.js';

const RECONNECT_DELAY = 3000;
let reconnectTimer = null;
let activeCallbacks = {};

export function setConnected(connected) {
  state.isWsConnected = connected === true;
  const statusEl = document.getElementById('conn-status');
  if (statusEl) {
    const connecting = connected === null;
    statusEl.className = `conn-status ${connected === true ? 'connected' : 'disconnected'}${connecting ? ' connecting' : ''}`;
    statusEl.textContent = connected === true ? '● 온라인' : (connecting ? '↻ 재연결 중' : '○ 오프라인');
  }
}

export function applyServerUnreadCounts(unreadCounts) {
  if (!unreadCounts || typeof unreadCounts !== 'object') return 0;
  state.unreadCounts.channels = {};
  state.unreadCounts.dms = {};
  let total = 0;
  Object.entries(unreadCounts).forEach(([k, v]) => {
    const count = Number(v) || 0;
    if (k.startsWith('channel:')) {
      const chanId = k.replace('channel:', '');
      state.unreadCounts.channels[chanId] = count;
      total += count;
    } else if (k.startsWith('dm:')) {
      const senderId = k.replace('dm:', '');
      state.unreadCounts.dms[senderId] = count;
      for (const [uname, u] of userDirectory.entries()) {
        if (String(u.id) === String(senderId)) {
          state.unreadCounts.dms[uname] = count;
          break;
        }
      }
      const matchingDm = state.dms.find(d => String(d.partnerUserId) === String(senderId));
      if (matchingDm) {
        state.unreadCounts.dms[matchingDm.name] = count;
      }
      total += count;
    }
  });

  if (isChatActiveAndFocused() && state.activeRoom.id) {
    if (state.activeRoom.type === 'channel') {
      const currentChanCount = state.unreadCounts.channels[state.activeRoom.id] || 0;
      if (currentChanCount > 0) {
        total = Math.max(0, total - currentChanCount);
        state.unreadCounts.channels[state.activeRoom.id] = 0;
      }
    } else if (state.activeRoom.type === 'dm') {
      const dmPartner = state.activeRoom.id;
      const dmPartnerId = userDirectory.get(dmPartner)?.id || state.dms.find(d => d.name === dmPartner)?.partnerUserId;
      const currentDmCount = state.unreadCounts.dms[dmPartner] ?? (dmPartnerId ? state.unreadCounts.dms[String(dmPartnerId)] : 0) ?? 0;
      if (currentDmCount > 0) {
        total = Math.max(0, total - currentDmCount);
        state.unreadCounts.dms[dmPartner] = 0;
        if (dmPartnerId) state.unreadCounts.dms[String(dmPartnerId)] = 0;
      }
    }
  }

  updateDocumentTitle(total);
  return total;
}

export function initWebSocket(callbacks = {}) {
  activeCallbacks = callbacks;
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${location.host}/ws`;
  const ws = new WebSocket(wsUrl);
  state.socket = ws;
  setConnected(null);

  ws.onopen = () => {
    if (state.socket !== ws) return;
    setConnected(true);
    state.reconnectAttempts = 0;
  };

  ws.onmessage = (event) => {
    let data = {};
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }

    const myNick = state.currentUser ? state.currentUser.username : '';
    const myUserId = state.currentUser ? Number(state.currentUser.id) : null;

    switch (data.type) {
      case 'chat': {
        const chanId = Number(data.channel_id || 1);
        const conv = getOrCreateChannel(channelsDirectory.get(chanId) || { id: chanId, name: `channel-${chanId}`, display_name: `채널 ${chanId}` });
        const chatMessage = {
          msgType: 'chat',
          message_id: data.message_id,
          nickname: data.nickname,
          author_id: data.author_id,
          channel_id: chanId,
          content: data.content || '',
          created_at: data.created_at,
          edited_at: data.edited_at || null,
          is_hidden: Boolean(data.is_hidden),
          reply: data.reply || null,
          attachment: data.attachment || null,
          attachments: Array.isArray(data.attachments) ? data.attachments : null,
          attachment_removed: Boolean(data.attachment_removed),
          mentions: Array.isArray(data.mentions) ? data.mentions : [],
          mentioned_user_ids: Array.isArray(data.mentioned_user_ids) ? data.mentioned_user_ids : [],
          reactions: Array.isArray(data.reactions) ? data.reactions : [],
          is_pinned: Boolean(data.is_pinned),
        };

        if (state.activeRoom.type === 'channel' && String(state.activeRoom.id) === String(chanId)) {
          const existing = document.querySelector(`.msg-row[data-message-id="${chatMessage.message_id}"]`);
          if (!existing) {
            appendMessageNode(chatMessage, null);
            const msgList = document.getElementById('message-list');
            if (msgList) msgList.scrollTop = msgList.scrollHeight;
          }
        }


        const isOwn = data.nickname === myNick || (myUserId !== null && Number(data.author_id) === myUserId);
        if (!isOwn && !data.history) {
          const senderDisplay = displayNickname(data.nickname);
          const chanTitle = conv.displayName || `채널 ${chanId}`;
          const contentPreview = (data.content || '파일 전송').slice(0, 50);

          const mentionsMe = Array.isArray(chatMessage.mentioned_user_ids)
            && chatMessage.mentioned_user_ids.some(id => Number(id) === myUserId);
          const repliesToMe = Boolean(chatMessage.reply && chatMessage.reply.nickname === myNick);

          if (repliesToMe) {
            emitAttention({
              kind: 'reply',
              conversationId: `channel:${chanId}`,
              title: `#${chanTitle} · ${senderDisplay}`,
              body: `${senderDisplay}님이 답장했습니다: ${contentPreview}`,
              onSelectConv: callbacks.onSwitchConv,
            });
          } else if (mentionsMe) {
            emitAttention({
              kind: 'mention',
              conversationId: `channel:${chanId}`,
              title: `#${chanTitle} · ${senderDisplay}`,
              body: `${senderDisplay}님이 멘션했습니다: ${contentPreview}`,
              onSelectConv: callbacks.onSwitchConv,
            });
          } else {
            emitAttention({
              kind: 'ordinary',
              conversationId: `channel:${chanId}`,
              title: `#${chanTitle}`,
              body: `${senderDisplay}: ${contentPreview}`,
              onSelectConv: callbacks.onSwitchConv,
            });
          }
        }
        if (callbacks.onNewMessage) callbacks.onNewMessage(chatMessage);
        break;
      }

      case 'dm': {
        const partner = data.from_nick === myNick ? data.to_nick : data.from_nick;
        const partnerUserId = data.from_nick === myNick ? data.to_user_id : data.from_user_id;
        const conv = getOrCreateDm(partner, partnerUserId);
        conv.lastActivityTime = Math.max(conv.lastActivityTime || 0, new Date(data.created_at || Date.now()).getTime());

        const dmMessage = {
          msgType: 'dm',
          message_id: data.message_id,
          from_nick: data.from_nick,
          from_user_id: data.from_user_id,
          to_nick: data.to_nick,
          to_user_id: data.to_user_id,
          content: data.content || '',
          created_at: data.created_at,
          reply: data.reply || null,
          attachment: data.attachment || null,
          attachments: Array.isArray(data.attachments) ? data.attachments : null,
          attachment_removed: Boolean(data.attachment_removed),
          edited_at: data.edited_at || null,
          is_hidden: Boolean(data.is_hidden),
          reactions: Array.isArray(data.reactions) ? data.reactions : [],
          is_pinned: Boolean(data.is_pinned),
        };

        if (state.activeRoom.type === 'dm' && (state.activeRoom.id === partner || state.activeRoom.id === data.from_nick)) {
          const existing = document.querySelector(`.msg-row[data-message-id="${dmMessage.message_id}"]`);
          if (!existing) {
            appendMessageNode(dmMessage, null);
            const msgList = document.getElementById('message-list');
            if (msgList) msgList.scrollTop = msgList.scrollHeight;
          }
        }

        const isOwn = data.from_nick === myNick || (myUserId !== null && Number(data.from_user_id) === myUserId);
        if (!isOwn && !data.history) {
          const senderDisplay = displayNickname(data.from_nick);
          emitAttention({
            kind: 'dm',
            conversationId: `dm:${partner}`,
            title: `💬 1:1 대화 · ${senderDisplay}`,
            body: (data.content || '파일 전송').slice(0, 50),
            onSelectConv: callbacks.onSwitchConv,
          });
        }
        if (callbacks.onDmsUpdated) callbacks.onDmsUpdated();
        if (callbacks.onNewMessage) callbacks.onNewMessage(dmMessage);
        break;
      }


      case 'reaction_updated': {
        const msgType = data.message_type;
        const msgId = Number(data.message_id);
        const formattedId = msgType === 'channel' ? `public:${msgId}` : `dm:${msgId}`;
        const newReactions = Array.isArray(data.reactions) ? data.reactions : [];
        updateMessageReactionsInDOM(formattedId, newReactions);
        break;
      }

      case 'pin_updated': {
        fetchActivePinnedMessages({ preserveOpen: true });
        break;
      }

      case 'read_state_updated': {
        if (data.unread_counts && typeof data.unread_counts === 'object') {
          applyServerUnreadCounts(data.unread_counts);
          if (callbacks.onUnreadUpdated) callbacks.onUnreadUpdated(data.unread_counts);
        }
        break;
      }

      case 'quiz_leaderboard_updated': {
        if (callbacks.onLeaderboardInvalidated) callbacks.onLeaderboardInvalidated(data);
        break;
      }

      case 'conversation_muted_updated': {
        if (data.state) {
          const key = `${data.state.conversation_type}:${data.state.conversation_id}`;
          if (data.state.muted) {
            state.mutedConversations.add(key);
          } else {
            state.mutedConversations.delete(key);
          }
          if (callbacks.onMuteUpdated) callbacks.onMuteUpdated();
        }
        break;
      }

      case 'users': {
        const userList = Array.isArray(data.mention_list) ? data.mention_list : [];
        setMentionUsers(userList);
        userDirectory.clear();
        userList.forEach(u => {
          userDirectory.set(u.username, {
            id: u.id,
            username: u.username,
            display_name: u.display_name || u.username,
            online: Boolean(u.online),
          });
          if (state.unreadCounts.dms[String(u.id)] !== undefined) {
            state.unreadCounts.dms[u.username] = state.unreadCounts.dms[String(u.id)];
          }
        });
        state.dms.forEach(conv => {
          const u = userDirectory.get(conv.name);
          if (u) {
            conv.partnerUserId = u.id;
            if (state.unreadCounts.dms[String(u.id)] !== undefined) {
              state.unreadCounts.dms[conv.name] = state.unreadCounts.dms[String(u.id)];
            }
          }
        });
        renderOnlineList(userList, callbacks.onOpenDm);
        refreshRenderedAuthorNames();
        if (callbacks.onDmsUpdated) callbacks.onDmsUpdated();
        if (callbacks.onUnreadUpdated) callbacks.onUnreadUpdated();
        break;
      }

      case 'history_ready': {
        const dmHasOlder = data.dm_has_older && typeof data.dm_has_older === 'object' ? data.dm_has_older : {};
        Object.entries(dmHasOlder).forEach(([partner, hasOlder]) => {
          getOrCreateDm(partner).hasOlder = Boolean(hasOlder);
        });
        if (data.read_states && typeof data.read_states === 'object') {
          setMutedConversations(Object.values(data.read_states).filter(item => item?.muted));
          if (callbacks.onMuteUpdated) callbacks.onMuteUpdated();
        }
        if (data.unread_counts && typeof data.unread_counts === 'object') {
          applyServerUnreadCounts(data.unread_counts);
        }
        if (callbacks.onDmsUpdated) callbacks.onDmsUpdated();
        if (callbacks.onUnreadUpdated) callbacks.onUnreadUpdated(data.unread_counts);
        break;
      }


      case 'error': {
        showToast(data.message || '오류가 발생했습니다.', 'error');
        break;
      }

      case 'message_edited': {
        if (data.message && callbacks.onMessageEdited) {
          callbacks.onMessageEdited(data.message);
        }
        break;
      }

      case 'dm_edited': {
        if (data.message && callbacks.onMessageEdited) {
          callbacks.onMessageEdited(data.message);
        }
        break;
      }

      case 'message_hidden': {
        if (data.message && callbacks.onMessageHidden) {
          callbacks.onMessageHidden(data.message, data.is_hidden);
        }
        break;
      }

      case 'message_moved': {
        if (callbacks.onMessageMoved && data.message?.message_id) {
          callbacks.onMessageMoved(data.message.message_id);
        }
        break;
      }

      case 'attachment_deleted': {
        if (callbacks.onAttachmentDeleted && data.attachment_id) {
          callbacks.onAttachmentDeleted(data.attachment_id);
        }
        break;
      }

      case 'channel_created': {
        if (data.channel) {
          channelsDirectory.set(data.channel.id, data.channel);
          getOrCreateChannel(data.channel);
          showToast(`'# ${data.channel.display_name}' 채널이 생성되었습니다.`, 'info');
          if (callbacks.onChannelUpdated) callbacks.onChannelUpdated();
        }
        break;
      }

      case 'channel_updated': {
        if (data.channel) {
          channelsDirectory.set(data.channel.id, data.channel);
          showToast(`'# ${data.channel.display_name}' 채널이 수정되었습니다.`, 'info');
          if (callbacks.onChannelUpdated) callbacks.onChannelUpdated();
        }
        break;
      }

      case 'channel_deleted': {
        const delChanId = Number(data.channel_id);
        const delChan = channelsDirectory.get(delChanId);
        const delName = delChan?.display_name || `채널 ${delChanId}`;
        channelsDirectory.delete(delChanId);
        showToast(`'# ${delName}' 채널이 삭제되었습니다.`, 'warning');
        if (callbacks.onChannelUpdated) callbacks.onChannelUpdated();
        break;
      }

      case 'channel_archived': {
        const archivedId = Number(data.channel_id);
        channelsDirectory.delete(archivedId);
        state.channels = state.channels.filter(channel => channel.channelId !== archivedId);
        if (callbacks.onChannelArchived) callbacks.onChannelArchived(archivedId);
        if (callbacks.onChannelUpdated) callbacks.onChannelUpdated();
        break;
      }

      case 'channel_unarchived': {
        if (data.channel) {
          channelsDirectory.set(Number(data.channel.id), data.channel);
          getOrCreateChannel(data.channel);
          if (callbacks.onChannelUpdated) callbacks.onChannelUpdated();
        }
        break;
      }

      case 'presence':
        // Silently ignored — presence count handled by 'users' event
        break;
    }
  };

  ws.onclose = () => {
    // A socket can close after a newer socket has already been installed.
    // Never let that stale event change the current connection indicator.
    if (state.socket !== ws) return;
    setConnected(false);
    if (ws.code === 1008) {
      state.socket = null;
      if (callbacks.onSessionExpired) callbacks.onSessionExpired();
      return;
    }
    if (state.currentUser) {
      scheduleReconnect(callbacks);
    }
  };

  ws.onerror = () => {
    if (state.socket === ws) setConnected(false);
  };
}

function scheduleReconnect(callbacks) {
  if (reconnectTimer) return;
  const reconnectCallbacks = callbacks && Object.keys(callbacks).length ? callbacks : activeCallbacks;
  setConnected(null);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (state.currentUser) initWebSocket(reconnectCallbacks);
  }, RECONNECT_DELAY);
}

export function sendWebSocketMessage(payload) {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(payload));
    return true;
  }
  showToast('서버와 연결이 끊겨 있습니다. 재연결을 시도합니다.', 'warning');
  if (state.currentUser && !reconnectTimer) scheduleReconnect(activeCallbacks);
  return false;
}
