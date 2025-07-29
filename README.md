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

## [Play the game here](https://gifreactgame.onrender.com/)

## Tech Stack
- Backend: FastAPI, PostgreSQL, Jinja2 templates
- Frontend: HTML, CSS, JavaScript (vanilla)
- Realtime: WebSockets
- Deployment: Render.com

## Local Setup Instructions

Follow these steps to run the project locally on your machine:

### 1. Clone the Repository

```bash
git clone https://github.com/davidereyes2002/gifreactgame_fastapi.git
cd gifreactgame_fastapi
```

### 2. Create and Activate a Virtual Environment

```bash
# Create a virtual environment
python -m venv venv
```

Activate it:

- **On macOS/Linux:**
  ```bash
  source venv/bin/activate
  ```

- **On Windows:**
  ```bash
  venv\Scripts\activate
  ```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Set Up Environment Variables

Create a `.env` file in the root directory and add the following:

```env
DATABASE_URL=your_postgresql_url
OPENAI_API_KEY=your_openai_api_key
GIPHY_API_KEY=your_giphy_api_key
SECRET_KEY=your_secret_key
```

> **Note:** Never commit your `.env` file to version control.

### 5. Set Up the Database

Ensure PostgreSQL is running, then:

- Create the database (if not already created)
- Run the schema setup (see **Database Structure** section below)

### 6. Start the Server

```bash
uvicorn main:app --reload
```

### 7. Open in Browser

Visit the app in your browser at:

[http://localhost:8000](http://localhost:8000)

---

## Database Structure

This project uses a PostgreSQL database with the following core tables and relationships:

---

### Tables and Key Columns

> **Note:** Primary Key (PK) and Foreign Key (FK)

#### `users`
- **id** (PK): Unique identifier for each user.
- **username**: User's login name.
- **hash**: Hashed password for authentication.

#### `sessions`
- **id** (PK): Unique game session identifier.
- **category**: Game category or type.
- **players**: Number of players in the session.
- **time_per_question**: Time allowed per round/question.
- **points_to_win**: Points required to win the session.
- **host_id** (FK to `users.id`): User who is the session host.
- **active**: Boolean indicating if the session is active.

#### `session_users`
- Composite PK: (`session_id`, `user_id`)
- **session_id** (FK to `sessions.id`): Session reference.
- **user_id** (FK to `users.id`): User reference.
- **is_host**: Boolean indicating if the user is host in this session.

#### `rounds`
- Composite PK: (`session_id`, `round`)
- **started**: Boolean if round started.
- **ended**: Boolean if round ended.
- **paused**: Boolean if round paused.
- **start_at**, **pause_at**, **resume_at**, **end_at**: Timestamps for round lifecycle.

#### `game_sentences`
- **id** (PK)
- **session_id** (FK to `sessions.id`)
- **sentence**: Text sentence used in the game round.

#### `game_started`
- **session_id** (PK, FK to `sessions.id`)
- **started**: Boolean if game started.
- **start_time**: Timestamp when the game started.
- **paused**: Boolean if the game is paused.

#### `gif_urls`
- Composite Unique Constraint on (`session_id`, `user_id`, `round`): Ensures unique GIF submission per user per round per session.
- **gif_url**: URL of submitted GIF.
- **is_n**: Boolean flag (e.g., if GIF is a null submission).

#### `votes`
- Composite PK: (`session_id`, `user_id`, `round`)
- **voted_for_user_id** (FK to `users.id`): The user voted for.
- Tracks votes cast by users each round.

#### `user_scores`
- Composite PK: (`session_id`, `user_id`)
- **score**: Total score of the user in the session.
- **winner**: Boolean flag if the user won the session.

---

### Relationships

- **Users** participate in **Sessions** via `session_users`.
- Each **Session** consists of multiple **Rounds**.
- During rounds, users submit **GIFs** stored in `gif_urls`.
- Users vote on GIFs in `votes`.
- Scores for users per session are tracked in `user_scores`.
- The game sentences linked to sessions are stored in `game_sentences`.
- The overall game state (started, paused) is stored in `game_started`.

---

### Additional Notes

- Primary keys and foreign keys ensure data integrity.
- Unique constraints prevent duplicate submissions and votes.
- Timestamp fields track the timing of rounds and game states for synchronization.
