// ============================================================
// BambooChat - Multiplayer Chess Module
// Native ES Module Architecture
// ============================================================

import { state } from './state.js';
import { showToast } from './utils.js';

let chessWs = null;
let currentRoom = null;
let localGame = null;
let viewGame = null;
let myColor = null; // 'w', 'b', or null
let selectedSquare = null;
let legalTargets = [];
let legalMoves = [];
let pendingPromotion = null;
let clockInterval = null;
let currentHistoryIndex = 0;
let previewHistoryIndex = null;
let lastResultKeyHandled = null;
let localGameStartedHandled = false;

const FILES = ['a','b','c','d','e','f','g','h'];
const RANKS = ['8','7','6','5','4','3','2','1'];
const PIECE_UNICODE = {
  w: { p: '♟', n: '♞', b: '♝', r: '♜', q: '♛', k: '♚' },
  b: { p: '♟', n: '♞', b: '♝', r: '♜', q: '♛', k: '♚' }
};

const PIECE_GUIDES = {
  p: { title: "♟️ 폰 (일꾼)", desc: "앞으로 1칸 전진합니다 (첫 이동 시 2칸 가능). 적을 잡을 때는 대각선 앞 1칸으로 이동합니다." },
  n: { title: "♞ 나이트 (기사)", desc: "L자 모양으로 이동합니다. 다른 기물을 뛰어넘을 수 있는 유일한 기물입니다!" },
  b: { title: "♝ 비숍 (성직자)", desc: "대각선 방향으로 장애물이 없을 때까지 원하는 만큼 멀리 이동합니다." },
  r: { title: "♜ 룩 (전차/성)", desc: "가로 또는 세로 방향으로 장애물이 없을 때까지 멀리 직선 이동합니다." },
  q: { title: "♛ 퀸 (여왕)", desc: "가로, 세로, 대각선 모든 방향으로 멀리 이동할 수 있는 가장 강력한 기물입니다!" },
  k: { title: "♚ 킹 (국왕 - 가장 중요)", desc: "모든 방향으로 1칸씩 이동합니다. 적의 공격 범위 안으로는 스스로 들어갈 수 없습니다." }
};

const $ = id => document.getElementById(id);

export function initChessListeners() {
  const chessBtn = $('chess-btn');
  const modalCloseBtn = $('chess-modal-close');
  if (chessBtn) {
    chessBtn.addEventListener('click', openChessModal);
  }
  if (modalCloseBtn) {
    modalCloseBtn.addEventListener('click', closeChessModal);
  }

  // Lobby controls
  const openCreateBtn = $('chess-open-create-btn');
  const cancelCreateBtn = $('chess-cancel-create-btn');
  const submitCreateBtn = $('chess-submit-create-btn');
  const refreshLobbyBtn = $('chess-refresh-lobby-btn');

  if (openCreateBtn) {
    openCreateBtn.addEventListener('click', () => {
      $('chess-create-panel').classList.toggle('hidden');
      $('chess-create-title').focus();
    });
  }
  if (cancelCreateBtn) {
    cancelCreateBtn.addEventListener('click', () => {
      $('chess-create-panel').classList.add('hidden');
    });
  }
  if (submitCreateBtn) {
    submitCreateBtn.addEventListener('click', handleCreateRoom);
  }
  if (refreshLobbyBtn) {
    refreshLobbyBtn.addEventListener('click', requestLobbyList);
  }

  // Game Room controls
  const leaveRoomBtn = $('chLeaveRoomBtn');
  const startGameBtn = $('chStartGameBtn');
  const drawOfferBtn = $('chDrawOfferBtn');
  const acceptDrawBtn = $('chAcceptDrawBtn');
  const rejectDrawBtn = $('chRejectDrawBtn');
  const resignBtn = $('chResignBtn');
  const returnToSpecBtn = $('chReturnToSpecBtn');
  const applyMatchBtn = $('chApplyMatchBtn');
  const promoCloseBtn = $('chPromoCloseBtn');
  const returnLiveBtn = $('chReturnLiveBtn');

  if (leaveRoomBtn) leaveRoomBtn.addEventListener('click', handleLeaveRoom);
  if (startGameBtn) startGameBtn.addEventListener('click', handleStartGame);
  if (drawOfferBtn) drawOfferBtn.addEventListener('click', handleOfferDraw);
  if (acceptDrawBtn) acceptDrawBtn.addEventListener('click', () => handleRespondDraw(true));
  if (rejectDrawBtn) rejectDrawBtn.addEventListener('click', () => handleRespondDraw(false));
  if (resignBtn) resignBtn.addEventListener('click', handleResign);
  if (returnToSpecBtn) returnToSpecBtn.addEventListener('click', handleReturnToSpec);
  if (applyMatchBtn) applyMatchBtn.addEventListener('click', handleApplyQueue);
  if (promoCloseBtn) promoCloseBtn.addEventListener('click', closePromotionModal);
  if (returnLiveBtn) {
    returnLiveBtn.addEventListener('click', () => {
      jumpToHistory((currentRoom?.move_history?.length || 1) - 1);
    });
  }
}

export function openChessModal() {
  const modal = $('chess-modal');
  if (!modal) return;
  modal.classList.remove('hidden');

  if (!window.Chess) {
    showToast('체스 엔진을 로드하는 중입니다. 잠시 후 다시 시도해주세요.', 'warning');
    return;
  }
  if (!localGame) localGame = new window.Chess();
  if (!viewGame) viewGame = new window.Chess();

  ensureChessWs();
}

export function closeChessModal() {
  const modal = $('chess-modal');
  if (!modal) return;
  modal.classList.add('hidden');
}

function ensureChessWs() {
  if (chessWs && (chessWs.readyState === WebSocket.OPEN || chessWs.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${protocol}//${window.location.host}/ws/chess`;
  chessWs = new WebSocket(url);

  chessWs.onopen = () => {
    requestLobbyList();
    if (clockInterval) clearInterval(clockInterval);
    clockInterval = setInterval(realtimeClockTick, 200);
  };

  chessWs.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      handleWsMessage(data);
    } catch (err) {
      console.error('Chess WS parse error:', err);
    }
  };

  chessWs.onclose = () => {
    if (clockInterval) clearInterval(clockInterval);
  };

  chessWs.onerror = (err) => {
    console.warn('Chess WS error:', err);
  };
}

function sendWs(payload) {
  if (chessWs && chessWs.readyState === WebSocket.OPEN) {
    chessWs.send(JSON.stringify(payload));
  }
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case 'lobby_update':
      renderLobby(msg.rooms || []);
      break;
    case 'room_state':
      syncRoomState(msg.room);
      break;
    case 'error':
      showToast(msg.message || '오류가 발생했습니다.', 'error');
      break;
    case 'pong':
      break;
  }
}

function requestLobbyList() {
  sendWs({ action: 'list_rooms' });
}

function handleCreateRoom() {
  const title = ($('chess-create-title').value || '').trim();
  const timeMinutes = parseInt($('chess-create-time').value, 10) || 10;
  sendWs({
    action: 'create_room',
    title: title,
    time_minutes: timeMinutes
  });
  $('chess-create-panel').classList.add('hidden');
  $('chess-create-title').value = '';
}

function handleLeaveRoom() {
  if (!currentRoom) return;
  if (currentRoom.game_started && !currentRoom.result && myColor) {
    if (!confirm('게임 도중에 나가면 기권패 처리됩니다. 나가시겠습니까?')) return;
  }
  sendWs({ action: 'leave_room', room_id: currentRoom.id });
  currentRoom = null;
  myColor = null;
  showLobbyScreen();
}

function handleStartGame() {
  if (!currentRoom) return;
  sendWs({ action: 'start_game', room_id: currentRoom.id });
}

function handleOfferDraw() {
  if (!currentRoom || !myColor || !currentRoom.game_started || currentRoom.result) return;
  if (localGame.turn() !== myColor) {
    showToast('자기 차례일 때만 무승부를 신청할 수 있습니다.', 'warning');
    return;
  }
  sendWs({ action: 'offer_draw', room_id: currentRoom.id });
  showToast('상대방에게 무승부를 신청했습니다.', 'info');
}

function handleRespondDraw(accept) {
  if (!currentRoom) return;
  $('chDrawModal').classList.add('hidden');
  sendWs({ action: 'respond_draw', room_id: currentRoom.id, accept: accept });
}

function handleResign() {
  if (!currentRoom || !myColor || !currentRoom.game_started || currentRoom.result) return;
  if (confirm('정말 기권하시겠습니까? 상대방의 승리로 처리됩니다.')) {
    sendWs({ action: 'resign', room_id: currentRoom.id });
  }
}

function handleReturnToSpec() {
  if (!currentRoom) return;
  sendWs({ action: 'pick_role', room_id: currentRoom.id, role: 'spectator' });
}

function handleApplyQueue() {
  if (!currentRoom) return;
  sendWs({ action: 'join_queue', room_id: currentRoom.id });
  showToast('대국 신청 대기열에 등록되었습니다.', 'info');
}

function joinRoomFromLobby(roomId, rolePref) {
  sendWs({ action: 'join_room', room_id: roomId, role_pref: rolePref });
}

window.pickChessRole = function(role) {
  if (!currentRoom) return;
  sendWs({ action: 'pick_role', room_id: currentRoom.id, role: role });
};

// ================= RENDER LOBBY =================
function showLobbyScreen() {
  $('chess-lobby-screen').classList.remove('hidden');
  $('chess-game-screen').classList.add('hidden');
  requestLobbyList();
}

function showGameScreen() {
  $('chess-lobby-screen').classList.add('hidden');
  $('chess-game-screen').classList.remove('hidden');
}

function renderLobby(rooms) {
  if (currentRoom) return;
  const grid = $('chess-room-grid');
  const empty = $('chess-lobby-empty');
  if (!grid || !empty) return;

  grid.innerHTML = '';
  if (rooms.length === 0) {
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  rooms.forEach(r => {
    const card = document.createElement('div');
    card.className = 'chess-room-card';

    let statusText = '대기 중';
    let statusClass = 'waiting';
    if (r.game_started && !r.result) {
      statusText = '대국 중';
      statusClass = 'playing';
    } else if (r.result) {
      statusText = '종료';
      statusClass = 'ended';
    }

    const hasSeat = (!r.white || !r.black) && !r.game_started;

    card.innerHTML = `
      <div>
        <div class="chess-room-header">
          <h4 class="chess-room-title">${escapeHtml(r.title)}</h4>
          <span class="chess-room-badge ${statusClass}">${statusText}</span>
        </div>
        <div class="chess-room-info">
          <div>👑 개설자: ${escapeHtml(r.created_by)}</div>
          <div>⚔️ 백: ${r.white ? escapeHtml(r.white) : '<span class="empty-seat">비어있음</span>'} | 흑: ${r.black ? escapeHtml(r.black) : '<span class="empty-seat">비어있음</span>'}</div>
          <div>⏱️ ${r.time_minutes}분 대국 | 👁️ 관전자 ${r.spectator_count}명</div>
        </div>
      </div>
      <div class="chess-room-actions">
        ${hasSeat ? `<button class="ch-btn ch-btn-primary small" onclick="window.chessJoin('${r.id}', 'play')">참가하기</button>` : ''}
        <button class="ch-btn ch-btn-ghost small" onclick="window.chessJoin('${r.id}', 'spectator')">관전하기</button>
      </div>
    `;
    grid.appendChild(card);
  });
}

window.chessJoin = function(roomId, mode) {
  joinRoomFromLobby(roomId, mode === 'play' ? null : 'spectator');
};

// ================= SYNC GAME ROOM =================
function syncRoomState(room) {
  if (!room) return;
  currentRoom = room;
  showGameScreen();

  const myUserId = state.currentUser?.id;
  if (room.white && room.white.id === myUserId) myColor = 'w';
  else if (room.black && room.black.id === myUserId) myColor = 'b';
  else myColor = null;

  $('chGameRoomTitle').textContent = `${room.title} (${room.time_minutes}분)`;

  if (!localGame) localGame = new window.Chess();
  localGame.load(room.fen);
  if (!viewGame) viewGame = new window.Chess();

  const history = room.move_history || [{ fen: room.fen, move: 'Start' }];
  currentHistoryIndex = history.length - 1;
  viewGame.load(room.fen);

  if (room.game_started && !localGameStartedHandled && !room.result) {
    localGameStartedHandled = true;
    showStartBanner();
  }
  if (!room.game_started) {
    localGameStartedHandled = false;
  }

  if (room.result && JSON.stringify(room.result) !== lastResultKeyHandled) {
    lastResultKeyHandled = JSON.stringify(room.result);
    showResultBanner(room.result);
  }

  updateRoleUI();
  buildBoardSkeleton();
  renderBoard();
  renderPlayersAndSpectators();
  renderHistoryUI();
  updateStatusUI();
  updateGuideCard();

  // Draw Modal
  if (room.draw_offer && myColor) {
    if (room.draw_offer === myColor) {
      $('chDrawWaitModal').classList.remove('hidden');
      $('chDrawModal').classList.add('hidden');
    } else {
      $('chDrawModal').classList.remove('hidden');
      $('chDrawWaitModal').classList.add('hidden');
    }
  } else {
    $('chDrawWaitModal').classList.add('hidden');
    $('chDrawModal').classList.add('hidden');
  }
}

function updateRoleUI() {
  const isPlayer = !!myColor;
  const isWaitingState = !currentRoom?.game_started && !currentRoom?.result;
  const isActiveGame = isPlayer && currentRoom?.game_started && !currentRoom?.result;

  if (isPlayer) {
    $('chPlayerActions').classList.remove('hidden');
    $('chSpectatorActions').classList.add('hidden');
  } else {
    $('chPlayerActions').classList.add('hidden');
    $('chSpectatorActions').classList.remove('hidden');
  }

  $('chDrawOfferBtn').style.display = isActiveGame ? 'inline-flex' : 'none';
  $('chResignBtn').style.display = isActiveGame ? 'inline-flex' : 'none';
  $('chReturnToSpecBtn').style.display = isPlayer && isWaitingState ? 'inline-flex' : 'none';
  $('chStartGameBtn').style.display = isPlayer && isWaitingState ? 'inline-flex' : 'none';
}

function squareAt(visRow, visCol) {
  const flipped = myColor === 'b';
  const row = flipped ? 7 - visRow : visRow;
  const col = flipped ? 7 - visCol : visCol;
  return FILES[col] + RANKS[row];
}

function buildBoardSkeleton() {
  const boardEl = $('chBoard');
  boardEl.innerHTML = '';
  for (let vr = 0; vr < 8; vr++) {
    for (let vc = 0; vc < 8; vc++) {
      const div = document.createElement('div');
      div.className = 'sq ' + (((vr + vc) % 2 === 0) ? 'light' : 'dark');
      div.dataset.vr = vr;
      div.dataset.vc = vc;
      div.addEventListener('click', onSquareClick);
      boardEl.appendChild(div);
    }
  }
  const flipped = myColor === 'b';
  $('chCoords').innerHTML = (flipped ? [...FILES].reverse() : FILES).map(f => `<span>${f}</span>`).join('');
  $('chRanksCol').innerHTML = (flipped ? [...RANKS].reverse() : RANKS).map(r => `<span>${r}</span>`).join('');
}

function renderBoard() {
  const boardEl = $('chBoard');
  const cells = boardEl.children;
  if (!currentRoom || !localGame) return;

  const activeIdx = (previewHistoryIndex !== null) ? previewHistoryIndex : currentHistoryIndex;
  const isLatest = activeIdx === (currentRoom.move_history?.length || 1) - 1;
  const targetGame = isLatest ? localGame : viewGame;
  const inCheck = targetGame.in_check ? targetGame.in_check() : false;
  const turn = targetGame.turn();

  const freezeOverlay = $('chBoardFreezeOverlay');
  if (currentRoom.result || !currentRoom.game_started) {
    freezeOverlay.classList.remove('hidden');
  } else {
    freezeOverlay.classList.add('hidden');
  }

  for (let i = 0; i < cells.length; i++) {
    const cell = cells[i];
    const vr = +cell.dataset.vr, vc = +cell.dataset.vc;
    const sq = squareAt(vr, vc);
    const piece = targetGame.get(sq);

    cell.className = 'sq ' + (((vr + vc) % 2 === 0) ? 'light' : 'dark');
    cell.innerHTML = '';

    if (piece) {
      cell.classList.add(piece.color === 'w' ? 'piece-w' : 'piece-b');
      const pieceSpan = document.createElement('span');
      pieceSpan.className = 'piece-char';
      pieceSpan.textContent = PIECE_UNICODE[piece.color][piece.type];
      cell.appendChild(pieceSpan);

      if (piece.type === 'k' && piece.color === turn && inCheck) {
        cell.classList.add('in-check');
      }
    }

    if (sq === selectedSquare) cell.classList.add('selected');
    if (legalTargets.includes(sq)) {
      const targetMove = legalMoves.find(m => m.to === sq);
      const isCapture = (targetMove && (targetMove.captured || (targetMove.flags && targetMove.flags.includes('e')))) || targetGame.get(sq);
      if (isCapture) cell.classList.add('legal-capture');
      else cell.classList.add('legal');
    }
    if (isLatest) {
      if (sq === currentRoom.last_from) cell.classList.add('last-from');
      if (sq === currentRoom.last_to) cell.classList.add('last-to');
    }
  }
}

function onSquareClick(e) {
  if (!myColor || !currentRoom?.game_started || currentRoom?.result || !currentRoom?.white || !currentRoom?.black) return;
  if (localGame.turn() !== myColor) return;

  if (currentHistoryIndex !== (currentRoom.move_history?.length || 1) - 1) {
    jumpToHistory((currentRoom.move_history?.length || 1) - 1);
  }

  const vr = +e.currentTarget.dataset.vr, vc = +e.currentTarget.dataset.vc;
  const sq = squareAt(vr, vc);

  if (selectedSquare) {
    if (sq === selectedSquare) { clearSelection(); return; }
    if (legalTargets.includes(sq)) {
      checkAndMakeMove(selectedSquare, sq);
      return;
    }
    const p = localGame.get(sq);
    if (p && p.color === myColor) { selectSquare(sq); return; }
    clearSelection();
  } else {
    const p = localGame.get(sq);
    if (p && p.color === myColor) selectSquare(sq);
  }
}

function selectSquare(sq) {
  selectedSquare = sq;
  legalMoves = localGame.moves({ square: sq, verbose: true });
  legalTargets = legalMoves.map(m => m.to);
  renderBoard();

  const piece = localGame.get(sq);
  if (piece && PIECE_GUIDES[piece.type]) {
    $('chGuideTitle').textContent = PIECE_GUIDES[piece.type].title;
    $('chGuideDesc').textContent = PIECE_GUIDES[piece.type].desc;
  }
}

function clearSelection() {
  selectedSquare = null;
  legalTargets = [];
  legalMoves = [];
  renderBoard();
  updateGuideCard();
}

function checkAndMakeMove(from, to) {
  const piece = localGame.get(from);
  const isPawn = piece && piece.type === 'p';
  const isPromotionRow = (piece.color === 'w' && to[1] === '8') || (piece.color === 'b' && to[1] === '1');

  if (isPawn && isPromotionRow) {
    pendingPromotion = { from, to };
    showPromotionModal(piece.color);
  } else {
    executeMove(from, to, 'q');
  }
}

function showPromotionModal(color) {
  const container = $('chPromoBtns');
  container.innerHTML = '';
  const pieces = [{ type: 'q', sym: '♛' }, { type: 'r', sym: '♜' }, { type: 'b', sym: '♝' }, { type: 'n', sym: '♞' }];

  pieces.forEach(p => {
    const btn = document.createElement('button');
    btn.className = `promo-btn ${color === 'w' ? 'piece-w' : 'piece-b'}`;
    btn.textContent = p.sym;
    btn.addEventListener('click', () => {
      $('chPromoModal').classList.add('hidden');
      if (pendingPromotion) {
        executeMove(pendingPromotion.from, pendingPromotion.to, p.type);
        pendingPromotion = null;
      }
    });
    container.appendChild(btn);
  });
  $('chPromoModal').classList.remove('hidden');
}

function closePromotionModal() {
  pendingPromotion = null;
  $('chPromoModal').classList.add('hidden');
  clearSelection();
}

function checkGameResult(gameInstance, historyList) {
  if (gameInstance.in_checkmate()) {
    const winner = gameInstance.turn() === 'w' ? 'b' : 'w';
    return { type: 'checkmate', winner: winner, desc: `${winner === 'w' ? '백' : '흑'} 체크메이트 승리` };
  }

  // 1. 스테일메이트 (Stalemate)
  if (gameInstance.in_stalemate()) {
    return { type: 'draw', winner: null, desc: '스테일메이트 무승부 (둘 수 있는 수가 없음)' };
  }

  // 2. 50수 규칙 (50-Move Rule - FEN halfmove clock 100회 도달)
  const fenParts = (gameInstance.fen() || '').split(' ');
  const halfMoves = parseInt(fenParts[4] || '0', 10);
  if (halfMoves >= 100) {
    return { type: 'draw', winner: null, desc: '50수 규칙 무승부 (50수간 폰 전진 및 기물 포획 없음)' };
  }

  // 3. 3수 동형 반복 (Threefold Repetition)
  // FIDE 규정: 기물 배치 + 턴 + 캐슬링 권한 + 앙파상 타깃이 동일한 포지션이 3회 발생
  if (historyList && historyList.length >= 5) {
    const counts = {};
    for (const item of historyList) {
      if (!item.fen) continue;
      const posKey = item.fen.split(' ').slice(0, 4).join(' ');
      counts[posKey] = (counts[posKey] || 0) + 1;
      if (counts[posKey] >= 3) {
        return { type: 'draw', winner: null, desc: '3회 동형 반복 무승부 (동일한 국면 3회 발생)' };
      }
    }
  }

  // 4. 기물 부족 무승부 (Insufficient Material)
  if (gameInstance.insufficient_material()) {
    return { type: 'draw', winner: null, desc: '기물 부족 무승부 (체크메이트 불가)' };
  }

  // 5. 기타 chess.js 무승부 조건
  if (gameInstance.in_draw()) {
    return { type: 'draw', winner: null, desc: '체스 규칙에 의한 무승부' };
  }

  return null;
}

function executeMove(from, to, promotionPiece) {
  const moveObj = localGame.move({ from, to, promotion: promotionPiece });
  if (!moveObj) return;

  clearSelection();

  const nextHistory = [...(currentRoom?.move_history || []), { fen: localGame.fen(), move: moveObj.san }];
  const result = checkGameResult(localGame, nextHistory);

  sendWs({
    action: 'move',
    room_id: currentRoom.id,
    from: from,
    to: to,
    san: moveObj.san,
    fen: localGame.fen(),
    flags: moveObj.flags,
    result: result
  });
}

function updateGuideCard() {
  const guideBox = $('chGuideCardBox');
  const titleEl = $('chGuideTitle');
  const descEl = $('chGuideDesc');

  if (!myColor) {
    titleEl.textContent = '👁️ 관전 모드';
    descEl.textContent = '현재 두 플레이어의 대국을 실시간 관전 중입니다.';
    return;
  }
  if (currentRoom?.result) {
    guideBox.classList.add('game-over-mode');
    titleEl.textContent = '🏁 게임 종료';
    descEl.textContent = `${currentRoom.result.desc}. 기보를 자유롭게 복기해 보세요.`;
    return;
  }
  guideBox.classList.remove('game-over-mode');

  if (!currentRoom?.game_started) {
    titleEl.textContent = '🎮 대국 시작 대기 중';
    descEl.textContent = '두 플레이어가 모두 준비되면 [게임 시작]을 눌러 대국을 시작합니다.';
    return;
  }

  if (selectedSquare) return;

  if (localGame.in_check()) {
    if (localGame.turn() === myColor) {
      titleEl.textContent = '⚠️ 체크! (킹이 위험합니다)';
      descEl.textContent = '내 킹이 공격받고 있습니다! 킹을 피신시키거나 막아야 합니다.';
    } else {
      titleEl.textContent = '⚔️ 상대 킹 체크!';
      descEl.textContent = '상대방 킹을 공격했습니다!';
    }
    return;
  }

  if (localGame.turn() !== myColor) {
    titleEl.textContent = '⏳ 상대방 차례입니다';
    descEl.textContent = '상대방이 수순을 생각하고 있습니다.';
    return;
  }

  titleEl.textContent = '💡 내 차례입니다!';
  descEl.textContent = '움직이고 싶은 기물을 선택하세요. 이동 가능한 위치가 표시됩니다.';
}

function updateStatusUI() {
  const el = $('chStatusLine');
  el.innerHTML = '';
  if (!currentRoom) return;

  const isWaiting = !currentRoom.game_started && !currentRoom.result;
  const canStart = !!currentRoom.white && !!currentRoom.black && isWaiting;

  if (!currentRoom.white || !currentRoom.black) {
    el.innerHTML = '<span class="badge">🎮 상대방 대기 중 (양쪽 좌석에 플레이어가 앉아야 시작 가능)</span>';
    $('chStartGameBtn').disabled = true;
    return;
  }

  if (isWaiting) {
    el.innerHTML = '<span class="badge">⏳ 준비 완료: [게임 시작] 버튼을 눌러주세요.</span>';
    $('chStartGameBtn').disabled = !canStart;
    return;
  }

  if (currentRoom.result) {
    el.innerHTML = `<span class="badge over">🏁 게임 종료: ${escapeHtml(currentRoom.result.desc)}</span>`;
    $('chStartGameBtn').disabled = true;
  } else {
    el.innerHTML = '<span class="badge turn-w">🎮 대국 진행 중</span>';
    const turnName = localGame.turn() === 'w' ? '백' : '흑';
    el.innerHTML += `<span class="badge turn-${localGame.turn()}">${turnName} 차례</span>`;
    if (localGame.in_check()) el.innerHTML += '<span class="badge check">체크!</span>';
    $('chStartGameBtn').disabled = true;
  }
}

function renderPlayersAndSpectators() {
  const box = $('chPlayersBox');
  if (!currentRoom) return;

  const myUserId = state.currentUser?.id;
  const canJoinWhite = !currentRoom.white && !currentRoom.game_started && !myColor;
  const canJoinBlack = !currentRoom.black && !currentRoom.game_started && !myColor;

  const getStat = (id) => {
    if (!id || !currentRoom.stats || !currentRoom.stats[id]) return '(0승 0무 0패)';
    const s = currentRoom.stats[id];
    return `(${s.wins}승 ${s.draws}무 ${s.losses}패)`;
  };

  box.innerHTML = `
    <div class="player-row">
      <div class="top">
        <span><span class="dot w"></span><b>백 (White)</b>: ${currentRoom.white ? escapeHtml(currentRoom.white.name) : '<span class="empty-seat">비어있음</span>'}</span>
        ${currentRoom.white ? `<span class="record-badge">${getStat(currentRoom.white.id)}</span>` : ''}
      </div>
      ${canJoinWhite ? `<button class="ch-btn ch-btn-primary small" style="margin-top:5px;" onclick="window.pickChessRole('w')">백으로 앉기</button>` : ''}
    </div>
    <div class="player-row">
      <div class="top">
        <span><span class="dot b"></span><b>흑 (Black)</b>: ${currentRoom.black ? escapeHtml(currentRoom.black.name) : '<span class="empty-seat">비어있음</span>'}</span>
        ${currentRoom.black ? `<span class="record-badge">${getStat(currentRoom.black.id)}</span>` : ''}
      </div>
      ${canJoinBlack ? `<button class="ch-btn ch-btn-primary small" style="margin-top:5px;" onclick="window.pickChessRole('b')">흑으로 앉기</button>` : ''}
    </div>
  `;

  const specBox = $('chSpectatorsBox');
  const specs = currentRoom.spectators || [];
  if (specs.length === 0) {
    specBox.innerHTML = '<span class="placeholder-line">관전자가 없습니다.</span>';
  } else {
    specBox.innerHTML = specs.map(s => {
      const qIndex = (currentRoom.match_queue || []).findIndex(q => q.id === s.id);
      const isQueued = qIndex !== -1;
      return `
        <div class="spec-item">
          <span>👁️ ${escapeHtml(s.name)} ${s.id === myUserId ? '(나)' : ''}</span>
          ${isQueued ? `<span style="color:var(--ch-gold);font-weight:bold;">[대기 ${qIndex + 1}번]</span>` : ''}
        </div>
      `;
    }).join('');
  }
}

function renderHistoryUI() {
  const listEl = $('chHistoryList');
  const history = currentRoom?.move_history || [];
  if (history.length <= 1) {
    listEl.innerHTML = '<span class="placeholder-line">대국이 시작되면 기보가 기록됩니다.</span>';
    return;
  }

  listEl.innerHTML = '';
  for (let i = 1; i < history.length; i += 2) {
    const moveNum = Math.ceil(i / 2);
    const whiteMove = history[i];
    const blackMove = history[i + 1];

    const unitDiv = document.createElement('div');
    unitDiv.className = 'history-row-unit';

    const numSpan = document.createElement('span');
    numSpan.className = 'history-num';
    numSpan.textContent = `${moveNum}.`;
    unitDiv.appendChild(numSpan);

    const wSpan = document.createElement('span');
    wSpan.className = 'history-item' + (i === currentHistoryIndex ? ' active' : '');
    wSpan.textContent = whiteMove.move;
    const wIdx = i;
    wSpan.addEventListener('mouseenter', () => previewHistory(wIdx));
    wSpan.addEventListener('mouseleave', () => clearPreviewHistory());
    wSpan.addEventListener('click', () => {
      previewHistoryIndex = null;
      jumpToHistory(wIdx);
    });
    unitDiv.appendChild(wSpan);

    if (blackMove) {
      const bSpan = document.createElement('span');
      bSpan.className = 'history-item' + ((i + 1) === currentHistoryIndex ? ' active' : '');
      bSpan.textContent = blackMove.move;
      const bIdx = i + 1;
      bSpan.addEventListener('mouseenter', () => previewHistory(bIdx));
      bSpan.addEventListener('mouseleave', () => clearPreviewHistory());
      bSpan.addEventListener('click', () => {
        previewHistoryIndex = null;
        jumpToHistory(bIdx);
      });
      unitDiv.appendChild(bSpan);
    }
    listEl.appendChild(unitDiv);
  }
  listEl.scrollTop = listEl.scrollHeight;
  updateReturnLiveBtn();
}

function previewHistory(index) {
  if (!currentRoom?.move_history?.[index]) return;
  previewHistoryIndex = index;
  const targetFen = currentRoom.move_history[index].fen;
  viewGame.load(targetFen);
  renderBoard();
}

function clearPreviewHistory() {
  if (previewHistoryIndex !== null) {
    previewHistoryIndex = null;
    const targetFen = currentRoom?.move_history?.[currentHistoryIndex]?.fen || localGame?.fen();
    if (targetFen) viewGame.load(targetFen);
    renderBoard();
  }
}

function jumpToHistory(index) {
  previewHistoryIndex = null;
  currentHistoryIndex = index;
  const targetFen = currentRoom.move_history[index].fen;
  viewGame.load(targetFen);
  renderBoard();
  renderHistoryUI();
  updateReturnLiveBtn();
}

function updateReturnLiveBtn() {
  const returnLiveBtn = $('chReturnLiveBtn');
  if (!returnLiveBtn) return;
  const isLatest = currentHistoryIndex === (currentRoom?.move_history?.length || 1) - 1;
  returnLiveBtn.classList.toggle('hidden', isLatest);
}

function realtimeClockTick() {
  if (!currentRoom) return;
  const whiteName = currentRoom.white ? currentRoom.white.name : '대기중';
  const blackName = currentRoom.black ? currentRoom.black.name : '대기중';
  $('chClockLabelWhite').textContent = `백 (${whiteName})`;
  $('chClockLabelBlack').textContent = `흑 (${blackName})`;

  const isBoth = currentRoom.white && currentRoom.black;
  const clk = currentRoom.clock;
  if (!clk) return;

  if (!isBoth || !currentRoom.game_started || currentRoom.result || currentRoom.draw_offer) {
    $('chClockWhite').textContent = formatClock(clk.w_remain);
    $('chClockBlack').textContent = formatClock(clk.b_remain);
    $('chClockChipWhite').classList.remove('active');
    $('chClockChipBlack').classList.remove('active');
    clk.last_tick_at = Date.now() / 1000;
    return;
  }

  const now = Date.now() / 1000;
  const delta = now - clk.last_tick_at;
  clk.last_tick_at = now;

  const currentTurn = localGame.turn();
  if (currentTurn === 'w') {
    clk.w_remain = Math.max(0, clk.w_remain - delta);
  } else {
    clk.b_remain = Math.max(0, clk.b_remain - delta);
  }

  $('chClockWhite').textContent = formatClock(clk.w_remain);
  $('chClockBlack').textContent = formatClock(clk.b_remain);
  $('chClockChipWhite').classList.toggle('active', currentTurn === 'w');
  $('chClockChipBlack').classList.toggle('active', currentTurn === 'b');

  if ((clk.w_remain <= 0 || clk.b_remain <= 0) && !currentRoom.result) {
    const winner = clk.w_remain <= 0 ? 'b' : 'w';
    const result = { type: 'timeout', winner: winner, desc: `${winner === 'w' ? '백' : '흑'} 시간승` };
    sendWs({
      action: 'move',
      room_id: currentRoom.id,
      fen: localGame.fen(),
      result: result
    });
  }
}

function formatClock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function showStartBanner() {
  const banner = $('chResultBanner');
  banner.className = 'result-banner start show';
  banner.textContent = '⚔️ 대국 시작!';
  setTimeout(() => banner.classList.remove('show'), 1500);
}

function showResultBanner(res) {
  if (!res) return;
  const banner = $('chResultBanner');
  banner.className = 'result-banner';

  if (!res.winner) {
    banner.textContent = '🤝 무승부!';
    banner.classList.add('draw');
  } else if (myColor) {
    if (res.winner === myColor) {
      banner.textContent = '🎉 승리!';
      banner.classList.add('win');
    } else {
      banner.textContent = '💀 패배...';
      banner.classList.add('lose');
    }
  } else {
    banner.textContent = res.winner === 'w' ? '🏆 백(White) 승리!' : '🏆 흑(Black) 승리!';
    banner.classList.add('win');
  }

  banner.classList.add('show');
  setTimeout(() => banner.classList.remove('show'), 2500);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}