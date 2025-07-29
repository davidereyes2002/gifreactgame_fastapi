# GIF Reactions Game Web App

A real-time multiplayer game where players respond to prompts with GIFs and vote on the funniest or most fitting submission. Built with FastAPI, WebSockets, and vanilla JavaScript.

---

## Features

- **User Authentication System**
  - Secure registration, login, and logout with hashed passwords
  - Session-based authentication using signed cookies
  - Access control for host-only routes (e.g., starting/pausing game)

- **Multiplayer Game Sessions**
  - Users can **host** or **join** live game sessions
  - Each session has a unique ID, host identity, and live status tracking
  - Host controls game start, pause, and round transitions

- **Real-Time WebSocket Communication**
  - Bi-directional updates using FastAPI WebSocket endpoints
  - Dynamic updates for:
    - Player joins/leaves
    - Presence tracking (waiting room vs game page)
    - Round countdowns and transitions
    - Live GIF submissions and votes
    - Game pause logic on disconnects

- **GIF Reaction Game Flow**
  - Sentence prompt generation (via OpenAI)
  - GIF search and selection using the Giphy API
  - Round-based gameplay: submit, vote, score
  - Players can't vote on their own GIFs
  - Automatic round advancement and endgame logic

- **Timers and Round Management**
  - Host-controlled round timer with auto-submission if time expires
  - Countdown UI for round start and pause transitions
  - Round states: `idle`, `new_round`, `started`, `voting`, `results`, `ended`, `paused`, `game_over`
  - Pause/resume system triggered by player disconnections

- **Scoring and Leaderboards**
  - Vote-based scoring with configurable `points_to_win` threshold
  - Real-time leaderboard updates
  - Game-over logic once a player reaches the win condition

- **OpenAI Integration**
  - Dynamically generated sentence prompts to keep the game fresh
  - Prompts tailored to encourage creative GIF reactions

- **Persistent Database Storage (PostgreSQL)**
  - Stores user data, sessions, rounds, votes, GIFs, scores
  - Fully normalized schema with foreign key relationships

- **Security & Validation**
  - Passwords hashed with `bcrypt`
  - Input validation and access checks for all sensitive operations
  - Protection against duplicate submissions or votes

- **Configurable Environment with `.env`**
  - Uses `python-dotenv` for managing secrets and environment config
  - Easy deployment and development configuration separation

- **Production-Ready Deployment**
  - Fully deployed backend on Render with PostgreSQL support
  - Works on both desktop and mobile browsers
  - Graceful error handling for expired/inactive sessions
