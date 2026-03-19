const homeScreen = document.getElementById("home-screen");
const lobbyScreen = document.getElementById("lobby-screen");
const countdownScreen = document.getElementById("countdown-screen");
const gameScreen = document.getElementById("game-screen");
const resultScreen = document.getElementById("result-screen");

const nicknameInput = document.getElementById("nickname");
const roomCodeInput = document.getElementById("room-code-input");
const roomCodeDisplay = document.getElementById("room-code-display");
const playersList = document.getElementById("players-list");

const durationSelect = document.getElementById("duration");
const createRoomBtn = document.getElementById("create-room-btn");
const joinRoomBtn = document.getElementById("join-room-btn");
const startBtn = document.getElementById("start-btn");
const backBtn = document.getElementById("back-btn");
const submitBtn = document.getElementById("submit-btn");
const playAgainBtn = document.getElementById("play-again-btn");

const countdownNumber = document.getElementById("countdown-number");
const timerDisplay = document.getElementById("timer");
const scoreDisplay = document.getElementById("score");
const questionDisplay = document.getElementById("question");
const answerInput = document.getElementById("answer-input");
const winnerText = document.getElementById("winner-text");
const resultsList = document.getElementById("results-list");

const BACKEND_HTTP_URL = "https://speed-demom.onrender.com/";
const BACKEND_WS_URL = "wss://speed-demom.onrender.com/ws";

let socket = null;
let nickname = "";
let roomCode = "";
let players = [];
let host = "";
let timeLeft = 60;
let score = 0;
let correctAnswer = 0;
let timerInterval = null;
let gameDuration = 60;

let questions = [];
let currentQuestionIndex = 0;

function showScreen(screenToShow) {
  homeScreen.classList.add("hidden");
  lobbyScreen.classList.add("hidden");
  countdownScreen.classList.add("hidden");
  gameScreen.classList.add("hidden");
  resultScreen.classList.add("hidden");

  screenToShow.classList.remove("hidden");
}

function connectWebSocket() {
  socket = new WebSocket(BACKEND_WS_URL);

  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.type === "room_created") {
      roomCode = data.room_code;
      players = data.players;
      host = data.host;

      roomCodeDisplay.textContent = roomCode;
      updatePlayersList();
      updateHostControls();
      showScreen(lobbyScreen);
    }

    if (data.type === "player_joined") {
      roomCode = data.room_code;
      players = data.players;
      host = data.host;

      roomCodeDisplay.textContent = roomCode;
      updatePlayersList();
      updateHostControls();
      showScreen(lobbyScreen);
    }

    if (data.type === "game_started") {
      questions = data.questions;
      gameDuration = data.duration;
      currentQuestionIndex = 0;
      startCountdown();
    }

    if (data.type === "game_results") {
      renderResults(data.scores, data.winner);
      showScreen(resultScreen);
    }

    if (data.type === "error") {
      alert(data.message);
    }
  };
}

function updatePlayersList() {
  playersList.innerHTML = "";

  players.forEach((player) => {
    const li = document.createElement("li");
    li.textContent = player;
    playersList.appendChild(li);
  });
}

function updateHostControls() {
  const isHost = nickname === host;

  startBtn.style.display = isHost ? "block" : "none";
  durationSelect.style.display = isHost ? "block" : "none";

  const durationLabel = document.querySelector('label[for="duration"]');
  if (durationLabel) {
    durationLabel.style.display = isHost ? "block" : "none";
  }
}

function renderResults(scores, winner) {
  winnerText.textContent = `Winner: ${winner}`;
  resultsList.innerHTML = "";

  Object.entries(scores).forEach(([playerName, playerScore]) => {
    const li = document.createElement("li");
    li.textContent = `${playerName}: ${playerScore}`;
    resultsList.appendChild(li);
  });
}

function showCurrentQuestion() {
  if (currentQuestionIndex >= questions.length) {
    questionDisplay.textContent = "No more questions";
    correctAnswer = null;
    return;
  }

  const currentQuestion = questions[currentQuestionIndex];
  questionDisplay.textContent = currentQuestion.question;
  correctAnswer = currentQuestion.answer;
}

function startCountdown() {
  showScreen(countdownScreen);

  let count = 3;
  countdownNumber.textContent = count;

  const countdownInterval = setInterval(() => {
    count--;
    countdownNumber.textContent = count;

    if (count <= 0) {
      clearInterval(countdownInterval);
      startGame();
    }
  }, 1000);
}

function startGame() {
  timeLeft = gameDuration;
  score = 0;
  currentQuestionIndex = 0;

  scoreDisplay.textContent = score;
  timerDisplay.textContent = timeLeft;

  showScreen(gameScreen);
  showCurrentQuestion();

  answerInput.value = "";
  answerInput.focus();

  timerInterval = setInterval(() => {
    timeLeft--;
    timerDisplay.textContent = timeLeft;

    if (timeLeft <= 0) {
      endGame();
    }
  }, 1000);
}

function submitAnswer() {
  const userAnswer = Number(answerInput.value);

  if (userAnswer === correctAnswer) {
    score++;
    scoreDisplay.textContent = score;
  }

  currentQuestionIndex++;
  answerInput.value = "";
  showCurrentQuestion();
  answerInput.focus();
}

function endGame() {
  clearInterval(timerInterval);

  socket.send(JSON.stringify({
    type: "submit_score",
    room_code: roomCode,
    nickname: nickname,
    score: score
  }));

  winnerText.textContent = "Waiting for other player...";
  resultsList.innerHTML = `<li>${nickname}: ${score}</li>`;
  showScreen(resultScreen);
}

function createRoom() {
  nickname = nicknameInput.value.trim();

  if (!nickname) {
    alert("Please enter a nickname.");
    return;
  }

  connectWebSocket();

  socket.onopen = () => {
    socket.send(JSON.stringify({
      type: "create_room",
      nickname: nickname
    }));
  };
}

function joinRoom() {
  nickname = nicknameInput.value.trim();
  const enteredRoomCode = roomCodeInput.value.trim().toUpperCase();

  if (!nickname) {
    alert("Please enter a nickname.");
    return;
  }

  if (!enteredRoomCode) {
    alert("Please enter a room code.");
    return;
  }

  connectWebSocket();

  socket.onopen = () => {
    socket.send(JSON.stringify({
      type: "join_room",
      room_code: enteredRoomCode,
      nickname: nickname
    }));
  };
}

function requestStartGame() {
  socket.send(JSON.stringify({
    type: "start_game",
    room_code: roomCode,
    duration: Number(durationSelect.value)
  }));
}

createRoomBtn.addEventListener("click", createRoom);
joinRoomBtn.addEventListener("click", joinRoom);
startBtn.addEventListener("click", requestStartGame);
submitBtn.addEventListener("click", submitAnswer);
backBtn.addEventListener("click", () => showScreen(homeScreen));

answerInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    submitAnswer();
  }
});

playAgainBtn.addEventListener("click", () => {
  showScreen(homeScreen);
});