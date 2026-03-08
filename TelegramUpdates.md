# Telegram Channel Updates Dashboard

A real-time dashboard that displays the latest posts from configured Telegram channels in an auto-scrolling interface.

## Architecture

- **Backend**: FastAPI server (`server.py`) that scrapes public Telegram channel pages (`t.me/s/<channel>`)
- **Database**: Supabase (PostgreSQL) via async Python SDK, encapsulated in `database.py`
- **Frontend**: Single-page vanilla HTML/CSS/JavaScript application (`static/index.html`) with auto-scrolling tiles
- **Configuration**: `config.json` for global settings; per-user channel feeds stored in Supabase `Feeds` table
- **Environment**: `.env` file for secrets (`SUPABASE_URL`, `SUPABASE_KEY`) loaded via `python-dotenv`
- **Dependencies**: FastAPI, httpx, BeautifulSoup4, supabase, python-dotenv (see `requirements.txt`)

## Core Features

### 1. Channel Configuration

#### Per-User Feeds (Supabase)
- Each user has their own set of channel feeds stored in the Supabase `Feeds` table
- Feeds are managed via the management interface (⚙️) or the `/api/feeds` API
- Duplicate prevention: adding a feed that already exists for the user returns a 409 error
- **Channel formats supported**:
  - Plain name: `"channelname"`
  - With @: `"@channelname"`
  - Full URL: `"https://t.me/channelname"`
  - Public preview URL: `"https://t.me/s/channelname"`

#### Global Settings (`config.json`)
- **File**: `config.json`
- Contains only global settings (no channel lists):
  ```json
  {
    "refresh_interval_minutes": 5,
    "max_posts": 30,
    "scroll_speed": 50
  }
  ```
- Editable only by admin user (`id = 1`) via the management interface

### 2. Data Scraping (`server.py`)
- **Method**: HTTP requests to `https://t.me/s/<channel>` (public web preview) via httpx
- **Parser**: BeautifulSoup4 HTML parsing with CSS selectors
- **User Agent**: Mozilla/5.0 (Chrome) to avoid blocking
- **Timeout**: 15 seconds per request
- **Channel normalization**: `normalize_channel()` function handles various URL formats
- **Extracted data**:
  - Post text (HTML via `text_html` and plain text via `text_plain`)
  - Publication date/time (`datetime` field from `time[datetime]` elements)
  - View count (`views` from `.tgme_widget_message_views`)
  - Channel name and avatar (`channel_title` and `channel_photo`)
  - Media photos (`photo_url` from `.tgme_widget_message_photo_wrap` style attribute)
  - Video thumbnails (`video_thumb` from `.tgme_widget_message_video_thumb`)
  - Link previews (title, description, URL, image from `.tgme_widget_message_link_preview`)
- **No caching**: Fresh data on every request
- **Concurrency**: `asyncio.gather()` for simultaneous channel fetching
- **Error handling**: Failed channels are silently skipped, errors logged

### 3. Authentication System

#### Login Flow
1. User visits the app and sees a full-screen login form
2. User submits username and password via `POST /api/login`
3. Server queries the `Users` table in Supabase to verify credentials
4. On success: server returns a signed JWT token (30-day expiry), the username, and an `is_admin` flag
5. Frontend stores the token, username, and `is_admin` in `localStorage`
6. App UI is shown and `init()` loads the feed
7. On failure: server returns 401 with "Login failed, incorrect credentials"

#### JWT Token Management
- **Secret**: Generated per server process via `secrets.token_hex(32)` (tokens invalidate on server restart)
- **Algorithm**: HS256
- **Expiry**: 30 days from login
- **Payload**: `{ "user_name": "...", "user_id": <int>, "exp": <timestamp> }`
- **Storage**: `localStorage` keys `token`, `user_name`, and `is_admin`

#### Role-Based Access
- **Admin** (user `id = 1`): Full access to management interface including global settings (refresh interval, max posts, scroll speed) and channel feed management
- **Regular users** (user `id != 1`): Can manage their own channel feeds only; settings section is hidden
- **Backend enforcement**: `/api/admin/config` endpoints return 403 for non-admin users
- **Frontend enforcement**: `is_admin` flag from login response controls settings section visibility

#### Backend Endpoint Protection
- All `/api/*` endpoints (except `/api/login`) are protected via `require_auth` FastAPI dependency
- `require_auth` extracts the `Authorization: Bearer <token>` header, decodes and verifies the JWT, and returns the decoded payload (including `user_id` and `user_name`)
- Returns 401 if the token is missing, invalid, or expired
- **Unprotected endpoints**: `/api/login`, `/health`, `/`, static file routes

#### Frontend Auth Integration
- **`authFetch(url, options)`**: Wrapper around `fetch()` that automatically attaches the `Authorization: Bearer <token>` header to every request
- All API calls go through `authFetch` (except the login call itself)
- If any API call returns 401, `authFetch` triggers `handleLogout()` which clears `localStorage` and shows the login form
- **Logout button**: Exit icon in the header (right side) that clears the session and returns to login

#### Login UI
- Full-screen dark-themed overlay matching the app's design system
- Username and password inputs with `autocomplete` attributes
- Error message area (red text) for failed login attempts
- Login button with disabled state during submission
- Visible only when no valid token exists in `localStorage`

### 4. API Endpoints

#### `POST /api/login`
Authenticates a user against the Supabase `Users` table.

**Request body**:
```json
{ "user_name": "admin", "password": "secret" }
```

**Success response** (200):
```json
{ "token": "<jwt>", "user_name": "admin", "is_admin": true }
```

**Failure response** (401):
```json
{ "detail": "Login failed, incorrect credentials" }
```

#### `GET /api/posts` (protected)
Returns the latest posts from the logged-in user's configured channels, sorted by datetime (newest first), limited to `max_posts`.

**Implementation**: 
- Extracts `user_id` from JWT payload
- Queries Supabase `Feeds` table for the user's feed URLs
- Normalizes channel names
- Fetches all channels concurrently with 15s timeout
- Parses HTML for each channel
- Merges and sorts posts by datetime (descending)
- Returns top `max_posts` entries (from `config.json`)

**Response format**:
```json
[
  {
    "channel": "channelname",
    "channel_title": "Display Name",
    "channel_photo": "https://cdn.../avatar.jpg",
    "post_id": "12345",
    "post_url": "https://t.me/channelname/12345",
    "text_html": "<div>...</div>",
    "text_plain": "Plain text content",
    "views": "1.2K",
    "datetime": "2026-03-05T20:08:05+00:00",
    "photo_url": "https://cdn.../image.jpg",
    "video_thumb": "https://cdn.../thumb.jpg",
    "link_preview": {
      "url": "https://example.com",
      "title": "Link Title",
      "description": "Link description",
      "image": "https://cdn.../preview.jpg"
    }
  }
]
```

#### `GET /api/feeds` (protected)
Returns the logged-in user's feed URLs from Supabase.

**Response format**: `["https://t.me/channel1", "https://t.me/channel2"]`

#### `POST /api/feeds` (protected)
Adds a feed for the logged-in user with duplicate prevention.

**Request body**:
```json
{ "feed_url": "https://t.me/channelname" }
```

**Success response** (200):
```json
{ "status": "success", "feed_url": "https://t.me/channelname" }
```

**Duplicate response** (409):
```json
{ "detail": "Feed already exists" }
```

#### `DELETE /api/feeds` (protected)
Removes a feed for the logged-in user.

**Request body**:
```json
{ "feed_url": "https://t.me/channelname" }
```

**Success response** (200):
```json
{ "status": "success" }
```

#### `GET /api/config` (protected)
Returns client-specific configuration settings from `config.json`.

**Response format**:
```json
{
  "refresh_interval_minutes": 5,
  "scroll_speed": 50
}
```

#### `GET /api/admin/config` (protected, admin-only)
Returns full configuration for the admin settings interface. Returns 403 for non-admin users (`user_id != 1`).

#### `POST /api/admin/config` (protected, admin-only)
Updates global settings via admin interface. Returns 403 for non-admin users (`user_id != 1`).

**Request body**:
```json
{
  "refresh_interval_minutes": 5,
  "max_posts": 30,
  "scroll_speed": 50
}
```

#### `GET /health` and `HEAD /health`
Health check endpoint returning `{"status": "ok"}`. Supports both GET and HEAD methods for efficient health monitoring and keep-alive services.

#### `GET /`
Serves the main application (`static/index.html`).

#### PWA Support
- **`GET /static/manifest.json`**: Web app manifest with correct MIME type
- **`GET /static/sw.js`**: Service worker for PWA compliance

#### Static Files
- **Mount point**: `/static` serves files from `static/` directory
- **Main app**: `static/index.html` contains the entire frontend application
- **PWA assets**: Icons, manifest, and service worker files

### 5. Frontend Interface (`static/index.html`)

#### Design System
- **Theme**: Dark theme with CSS custom properties
- **Colors**: 
  - Background: `#0e1117`
  - Cards: `#1a1d27`
  - Text: `#e8eaed` / `#9aa0a6` (secondary)
  - Accent: `#2196f3`
  - Borders: `#2d3240`
- **Typography**: System font stack (-apple-system, BlinkMacSystemFont, Segoe UI, etc.)
- **Border radius**: 14px consistent rounded corners

#### Navigation System
- **Multi-View Interface**: Three distinct views accessible via header navigation
  - **Main Feed**: Primary channel content (default view, "Main" link on left)
  - **Saved Posts**: Bookmarked posts (bookmark icon in control buttons)
  - **Management Interface**: Feed and admin controls (⚙️ icon on top-right)
- **Navigation Layout**: Streamlined header without logo
  - **Left side**: Main navigation link + control buttons
  - **Right side**: Filter bar, status, settings icon, logout button
- **Context-Aware UI**: Different controls shown based on current view

#### Layout Structure
- **Login Overlay**: Full-screen centered login form (hidden when authenticated)
- **App Container**: Flex column wrapper (hidden when not authenticated)
- **Header**: Fixed 44px height with streamlined navigation and controls
  - **Horizontally Scrollable**: Entire header scrolls when content overflows
  - **Left Section**: "Main" navigation + Control buttons (sync, sort, stop/resume scrolling, saved)
  - **Right Section**: Scrollable filter bar + Status indicator + Settings (⚙️) + Logout
  - **No Logo**: Clean, minimalist design without branding elements
- **Viewport**: Flex-grow scrollable area with thin scrollbars
- **Cards**: Responsive layout adapting to screen orientation
  - **Desktop/Landscape**: Horizontal layout with media side (40% width, max 500px)
  - **Mobile Portrait**: Vertical layout with media above text
- **Management Interface**: Full-screen configuration panel with scrollable content

#### Mobile Portrait Mode Support
- **Responsive Design**: Automatic detection of portrait orientation on mobile
- **Layout Transformation**: 
  - Posts switch from horizontal to vertical layout
  - Media appears above text content
  - Reduced text sizes for mobile readability
- **Typography Scaling**:
  - Post text: 1.4rem → 1rem (reduced from original 2rem for better readability)
  - Channel names: 1.3rem → 1rem
  - Metadata: 1rem → 0.9rem
- **Spacing Optimization**:
  - Reduced horizontal padding (12vw → 4vw)
  - Optimized content padding (28px 48px → 20px 16px)
- **Auto-scroll Preserved**: Full functionality maintained in mobile mode

#### Auto-scrolling Implementation
- **Engine**: `requestAnimationFrame` with delta-time calculations
- **Speed**: Configurable pixels per second from API config
- **Direction**: Top to bottom (newest posts first)
- **Manual Control**: Stop/Resume button for user control over auto-scrolling
- **Loop behavior**: 
  - Scroll to bottom → pause 3 seconds → reset to top
  - Initial 2-second pause before starting
- **Pause triggers**:
  - Mouse wheel: 4-second pause
  - Touch start/end: 4-second pause
  - Manual scroll detection via event listeners
  - Stop/Resume button: Complete user control

#### Typography Scale
- **Post text**: 1.4rem, line-height 1.55 (reduced from 2rem for better readability)
- **Channel names**: 1.3rem, font-weight 600
- **Metadata**: 1rem (dates, views)
- **Link previews**: 1.15rem titles, 1.05rem descriptions
- **Filters**: 0.85rem buttons

#### Interactive Elements
- **Post cards**: Click to open in Telegram (`window.open`)
- **Channel links**: Click to open channel page (event propagation stopped)
- **Link previews**: Click to open external links
- **Filter buttons**: Toggle between "All" and individual channels
- **Control buttons**: Manual sync, sort order toggle, and stop/resume scrolling
- **Navigation links**: Switch between Main (left), Saved (control bar), and Management (top-right) views
- **Logout button**: Clears session and returns to login form
- **Login form**: Username/password inputs with error message display
- **Hover effects**: Border color changes, text color transitions

#### Enhanced Control System
- **Manual Sync Button**: 
  - Instant refresh of current feed content
  - Visual loading animation during refresh
  - Prevents multiple simultaneous requests
  - Works independently of automatic refresh timer
- **Sort Order Toggle**:
  - Switch between "Newest First" (default) and "Oldest First"
  - Visual indicator showing current sort mode ("New"/"Old")
  - Instant re-sorting without network requests
  - Tooltip shows current sort state
- **Stop/Resume Scrolling Button**:
  - Complete user control over auto-scrolling behavior
  - Toggle between "Stop" and "Resume" states
  - Visual indicator with dynamic icon (stop/play)
  - Immediate response without page refresh

#### Channel Filtering System
- **UI**: Horizontally scrollable filter bar in header
- **Scrollable Design**: 
  - Desktop: 500px max-width with horizontal scroll
  - Mobile: 250px max-width with touch scroll
  - Styled scrollbars matching app theme
- **Buttons**: "All" + individual channel buttons with active states
- **State management**: `activeFilter` variable persists selection
- **Implementation**: Filter posts array and re-render on change
- **Context Awareness**: Hidden in Management view, visible in feed views

#### Content Processing
- **HTML sanitization**: `sanitizeHtml()` function removes `onclick`, adds security attributes
- **Date formatting**: Relative time display (minutes/hours/days ago) with fallback to date
- **Image loading**: Lazy loading with `loading="lazy"` attribute
- **Error handling**: Loading states, error messages, empty state handling

### 6. Data Flow

#### Initialization and Configuration
1. **Server Lifespan**: `load_dotenv()` loads `.env`, then the lifespan handler initializes `Database` and logs all users and feeds
2. **Auth Gate**: On page load, frontend checks `localStorage` for a JWT token; if missing, shows login form instead of the app
3. **Login**: User authenticates via `POST /api/login`; on success, token, username, and `is_admin` flag are stored and `init()` is called
4. **Frontend Startup**: `init()` function initializes multi-view interface (only called after successful auth)
5. **Event Listeners**: Set up navigation, control buttons, feed management buttons, logout, and PWA service worker
6. **Configuration**: Fetch `/api/config` (with auth header) to get `refresh_interval_minutes` and `scroll_speed`
7. **Default View**: Load Main feed as default view

#### View Data Management
- **View Switching**: `showView(view)` function manages interface state
  - **Main View**: Fetches from `/api/posts` (user's channels from Supabase)
  - **Saved View**: Fetches from `/api/saved` (bookmarked posts)
  - **Management View**: Loads feed management interface (and admin settings if admin)
- **Auto-Refresh**: Periodic refresh of main feed only
- **UI State Management**: Controls visibility of filters, buttons based on active view

#### Rendering Pipeline
1. **Data Fetching**: View-specific API endpoints with loading states
2. **Sorting**: Client-side sorting based on user preference (newest/oldest first)
3. **Filtering**: Channel-specific filtering with scrollable filter bar
4. **Rendering**: 
   - `renderFilters()`: Dynamic, scrollable filter buttons
   - `renderPosts()`: Responsive post cards with mobile support
   - `startScrolling()`: Auto-scroll with pause/resume functionality

#### Management Interface Flow
1. **Feed Loading**: Fetch user's feeds via `GET /api/feeds`
2. **Feed Display**: Render feed list with remove buttons
3. **Add Feed**: Input field + Add button; calls `POST /api/feeds` with duplicate detection (409 → "Feed already exists" message)
4. **Remove Feed**: Remove button calls `DELETE /api/feeds` and refreshes the list
5. **Admin Settings** (admin only): Load settings via `GET /api/admin/config`, save via `POST /api/admin/config`

#### PWA Integration
1. **Service Worker**: Register minimal service worker for PWA compliance
2. **Manifest**: Serve web app manifest with proper MIME types
3. **Installation**: Support for "Add to Home Screen" functionality
4. **Standalone Mode**: Full-screen experience without browser UI

### 7. Database Layer (`database.py`)

#### Supabase Integration
- **SDK**: Supabase Python SDK (async client via `supabase._async.client`)
- **Authentication**: `SUPABASE_URL` and `SUPABASE_KEY` environment variables (loaded from `.env`)
- **Client Type**: `AsyncClient` for non-blocking use in async request handlers
- **Initialization**: Async factory method `Database.create()` (since `create_client()` is async)
- **Instance Access**: Stored on `app.state.db`, accessible in endpoints via `request.app.state.db`

#### Database Schema

##### `Users` Table
| Column         | Type                     | Description                          |
|----------------|--------------------------|--------------------------------------|
| `id`           | bigint                   | Primary key (identity, auto-generated) |
| `created_at`   | timestamp with time zone | Row creation time (default `now()`)  |
| `user_name`    | text                     | Username for login                   |
| `User_password`| text                     | User password (plain text)           |

- **RLS**: Row Level Security is enabled; a SELECT policy is required for the API key to read rows
- **Referenced by**: `Feeds.user_id` foreign key
- **Admin user**: User with `id = 1` has admin privileges (can edit global settings)

##### `Feeds` Table
| Column    | Type   | Description                              |
|-----------|--------|------------------------------------------|
| `user_id` | bigint | Foreign key to `Users.id` (NOT NULL)     |
| `feed_url` | text  | Telegram channel URL associated with the user |

- **Foreign Key**: `user_id` references `Users.id` with `ON DELETE CASCADE`
- **Index**: `idx_feeds_user_id` on `user_id` for join/RLS performance
- **RLS**: Row Level Security requires appropriate policies for the API key to read/write rows
- **Duplicate Prevention**: Application-level check before insert (query for existing `user_id` + `feed_url` pair)

##### `save_for_later` Table
| Column      | Type                     | Description                                    |
|-------------|--------------------------|------------------------------------------------|
| `id`        | bigint                   | Primary key (identity, auto-generated)         |
| `user_id`   | bigint                   | The user who saved the post (NOT NULL)         |
| `created_at`| timestamp with time zone | Row creation time (default `now()`)            |
| `saved_post`| text                     | JSON-serialized post object (NOT NULL)         |

- **Index**: `idx_save_for_later_user_id` on `user_id` for query performance
- **RLS**: Row Level Security is enabled; appropriate policies required for the API key
- **Duplicate Prevention**: Application-level check before insert (parse existing rows to match `channel` + `post_id`)
- **Per-User Scope**: Each user's saved posts are isolated by `user_id`

#### Database Class API
- `Database.create() -> Database` (async classmethod): Initializes the async Supabase client and returns a `Database` instance
- `get_all_users() -> list[dict]` (async): Queries all rows from the `Users` table
- `get_all_feeds() -> list[dict]` (async): Queries all rows from the `Feeds` table
- `authenticate_user(user_name, password) -> dict | None` (async): Queries the `Users` table for matching `user_name` and `User_password`; returns the user row or `None`
- `get_feeds_for_user(user_id) -> list[dict]` (async): Queries `Feeds` for all rows matching the given `user_id`
- `add_feed(user_id, feed_url) -> dict | None` (async): Checks for existing duplicate; if none, inserts a new row. Returns `None` if duplicate exists
- `remove_feed(user_id, feed_url) -> bool` (async): Deletes the matching feed row. Returns `True` if a row was deleted
- `get_saved_posts(user_id) -> list[dict]` (async): Queries `save_for_later` for all rows matching the user, parses JSON `saved_post` column, returns list of post dicts with `saved_at` from `created_at`
- `save_post(user_id, post) -> dict | None` (async): Checks for duplicate (matching `channel` + `post_id` in existing saved posts); if none, inserts a new row with JSON-serialized post. Returns `None` if duplicate
- `unsave_post(user_id, channel, post_id) -> bool` (async): Finds and deletes the saved post row matching `channel` and `post_id` for the user. Returns `True` if a row was deleted

#### Startup Behavior
- On server startup (via FastAPI lifespan), the `Database` is initialized
- All users are fetched and logged (passwords excluded from log output for security; only `user_name` and `created_at` are logged)
- All feeds are fetched and logged (`user_id` and `feed_url`)

### 8. Deployment Configuration

#### Docker (Hugging Face Spaces)
- **Base image**: `python:3.13-slim`
- **User setup**: UID 1000 for HF Spaces compatibility
- **Port**: 7860 (HF Spaces standard)
- **Files**: `Dockerfile`, `README.md` with HF metadata

#### Render/Generic Hosting
- **Start command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- **Build command**: `pip install -r requirements.txt`
- **Port**: Dynamic via `$PORT` environment variable

#### Keep-Alive Service (GitHub Actions)
- **Purpose**: Prevents Render.com free tier services from sleeping due to inactivity
- **File**: `.github/workflows/keep-alive.yml`
- **Schedule**: Runs every 10 minutes using cron (`*/10 * * * *`)
- **Method**: HTTP GET request to `/health` endpoint
- **Features**:
  - Automatic scheduling via GitHub Actions cron
  - Manual trigger capability (`workflow_dispatch`)
  - Health check endpoint monitoring
  - Failure detection with non-zero exit codes
- **Configuration**:
  ```yaml
  name: Keep Alive
  on:
    schedule:
      - cron: '*/10 * * * *'  # Every 10 minutes
    workflow_dispatch:         # Manual trigger
  jobs:
    keep-alive:
      runs-on: ubuntu-latest
      steps:
        - name: Ping keep-alive endpoint
          run: curl -f https://your-app.onrender.com/health || exit 1
  ```

### 9. Dependencies (`requirements.txt`)
```
fastapi[standard]  # Web framework with built-in ASGI server
httpx              # Async HTTP client for Telegram scraping
beautifulsoup4     # HTML parsing for post extraction
supabase           # Supabase Python SDK (async client for PostgreSQL)
python-dotenv      # Load environment variables from .env file
```

**Key imports in `server.py`**:
- `asyncio` - Concurrent channel fetching
- `json` - Config file parsing
- `logging` - Request/error logging
- `re` - Image URL extraction from CSS
- `secrets` - JWT secret generation
- `pathlib.Path` - File path handling
- `contextlib.asynccontextmanager` - FastAPI lifespan management
- `jwt` (PyJWT) - JWT token encoding/decoding for authentication
- `fastapi` - Web framework, responses, static files, `Depends` for auth middleware
- `dotenv.load_dotenv` - Environment variable loading
- `database.Database` - Supabase database abstraction

## Technical Specifications

### Error Handling
- **Network failures**: 
  - Backend: `try/except` blocks log errors, return `None` for failed channels
  - Frontend: Failed API calls show error message and red status dot
- **Parse errors**: `BeautifulSoup` parsing errors don't crash the server
- **Empty responses**: Frontend shows "No posts to display" message
- **Graceful degradation**: Missing data fields handled with fallbacks

### Performance
- **Async I/O**: `httpx.AsyncClient` with `asyncio.gather()` for concurrent requests
- **No caching**: Fresh data on every request (trade-off: freshness vs speed)
- **Timeout**: 15-second timeout per HTTP request
- **Frontend optimization**: 
  - `requestAnimationFrame` for smooth scrolling
  - Lazy image loading with `loading="lazy"`
  - Event delegation for click handlers

### Security
- **Authentication**: JWT-based login with backend endpoint protection
  - All `/api/*` endpoints (except `/api/login`) require a valid `Authorization: Bearer <token>` header
  - `require_auth` FastAPI dependency verifies JWT signature, algorithm, and expiry; returns decoded payload with `user_id` and `user_name`
  - Invalid/expired tokens return 401; frontend auto-redirects to login on 401
- **Authorization**: Role-based access control
  - Admin endpoints (`/api/admin/config`) check `user_id == 1` and return 403 for non-admin users
  - Feed endpoints (`/api/feeds`) are scoped to the logged-in user's `user_id`
  - Frontend hides admin-only UI elements based on `is_admin` flag from login response
- **Session management**: JWT tokens stored in `localStorage` with 30-day expiry
  - JWT secret regenerated on server restart (invalidates all sessions)
  - Logout clears `localStorage` (`token`, `user_name`, `is_admin`) and resets UI to login form
- **Password handling**: Compared as plain text against Supabase `User_password` column (no hashing)
- **Input sanitization**: 
  - `sanitizeHtml()` removes `onclick` attributes
  - External links get `target="_blank"` and `rel="noopener noreferrer"`
- **XSS prevention**: HTML content sanitized before DOM insertion
- **CORS**: Not required (same-origin requests)
- **Rate limiting**: None implemented (relies on Telegram's rate limiting)
- **User-Agent spoofing**: Uses Chrome UA to avoid bot detection
- **Supabase credentials**: Stored in `.env` file (excluded from git via `.gitignore`); loaded at runtime via `python-dotenv`
- **Password logging**: User passwords are intentionally excluded from startup log output
- **Service worker**: Does not intercept fetch requests (avoids stripping `Authorization` headers on mobile browsers)

### Browser Compatibility
- **Target browsers**: Modern browsers with ES2017+ support
- **Required features**: 
  - CSS Custom Properties (variables)
  - CSS Grid and Flexbox
  - `async/await` and `fetch` API
  - `requestAnimationFrame`
- **No fallbacks**: Assumes modern browser environment

## Configuration Examples

### Global settings (`config.json`):
```json
{
  "refresh_interval_minutes": 5,
  "max_posts": 30,
  "scroll_speed": 50
}
```

## Known Limitations

1. **Public channels only**: Cannot access private channels or channels requiring authentication
2. **Rate limiting**: Subject to Telegram's rate limiting on public preview pages
3. **Content restrictions**: Some media may not be accessible in web preview format
4. **Network dependencies**: Requires outbound HTTP access to `t.me` (may be blocked in some hosting environments)
5. **No real-time updates**: Relies on periodic polling rather than WebSocket/SSE
6. **Memory usage**: All posts kept in memory (no pagination or cleanup)
7. **Single-threaded**: FastAPI runs in single process (no horizontal scaling)
8. **Keep-alive dependency**: Render.com free tier requires external pinging to prevent sleep
9. **PWA offline limitations**: No offline functionality - requires internet connection
10. **Supabase RLS**: Row Level Security must have appropriate policies for the API key to read/write the `Users` and `Feeds` tables
11. **Plain text passwords**: Passwords are stored and compared as plain text in Supabase (no hashing)
12. **JWT secret lifetime**: JWT secret is generated per server process; restarting the server invalidates all active sessions
13. **Admin detection**: Admin role is determined by `user_id == 1` (hardcoded); no dynamic role management

## Implementation Details

### Key Functions

#### Backend (`server.py`)
- `require_auth(request) -> dict`: FastAPI dependency that verifies JWT `Authorization: Bearer` header; returns decoded payload with `user_id` and `user_name`
- `lifespan(app)`: Async context manager that initializes `Database` and logs users/feeds at startup
- `login(request)`: `POST /api/login` handler -- authenticates credentials, returns JWT token with `user_id`, and `is_admin` flag
- `get_posts(request, user)`: `GET /api/posts` -- fetches user's feeds from Supabase, scrapes channels, returns sorted posts
- `get_feeds(request, user)`: `GET /api/feeds` -- returns user's feed URLs from Supabase
- `add_feed(request, user)`: `POST /api/feeds` -- adds a feed with duplicate check (409 on conflict)
- `delete_feed(request, user)`: `DELETE /api/feeds` -- removes a feed for the user
- `get_full_config(user)`: `GET /api/admin/config` -- returns config.json (admin-only, 403 for non-admin)
- `update_config(request, user)`: `POST /api/admin/config` -- updates config.json (admin-only, 403 for non-admin)
- `normalize_channel(raw: str) -> str`: Extracts channel name from various URL formats
- `fetch_channel_html(client, channel) -> str | None`: Async HTTP request with error handling
- `extract_image_url(style: str) -> str | None`: Regex extraction from CSS `url()` values
- `parse_channel_posts(html: str, channel: str) -> list[dict]`: BeautifulSoup parsing logic
- `load_config() -> dict`: JSON config file loader
- `get_saved_posts(request, user)`: `GET /api/saved` -- returns the logged-in user's saved posts from Supabase
- `save_post(request, user)`: `POST /api/saved` -- saves a post for the logged-in user in Supabase (duplicate check)
- `unsave_post(channel, post_id, request, user)`: `DELETE /api/saved/{channel}/{post_id}` -- removes a saved post for the logged-in user

#### Database (`database.py`)
- `Database.create() -> Database`: Async factory that initializes the Supabase async client
- `Database.get_all_users() -> list[dict]`: Fetches all rows from the `Users` table
- `Database.get_all_feeds() -> list[dict]`: Fetches all rows from the `Feeds` table
- `Database.authenticate_user(user_name, password) -> dict | None`: Queries `Users` for matching credentials
- `Database.get_feeds_for_user(user_id) -> list[dict]`: Fetches feeds for a specific user
- `Database.add_feed(user_id, feed_url) -> dict | None`: Inserts a feed with duplicate check; returns `None` if duplicate
- `Database.remove_feed(user_id, feed_url) -> bool`: Deletes a feed row; returns whether deletion occurred
- `Database.get_saved_posts(user_id) -> list[dict]`: Fetches saved posts for a user from `save_for_later`, parses JSON
- `Database.save_post(user_id, post) -> dict | None`: Saves a post with duplicate check; returns `None` if duplicate
- `Database.unsave_post(user_id, channel, post_id) -> bool`: Deletes a saved post row; returns whether deletion occurred

#### Frontend (`static/index.html`)
- `authFetch(url, options)`: Fetch wrapper that attaches JWT `Authorization` header and handles 401 logout
- `authHeaders()`: Returns `Authorization: Bearer <token>` header object from `localStorage`
- `handleLogin(e)`: Login form submission handler -- calls `/api/login`, stores token, username, and `is_admin`, shows app
- `handleLogout()`: Clears `localStorage` (token, user_name, is_admin), stops timers/animations, shows login form
- `showApp()` / `showLogin()`: Toggle visibility between login overlay and app container
- `fetchConfig()`: Loads client settings from `/api/config`
- `fetchPosts()`: Main feed data fetching with loading states
- `showView(view)`: Multi-view navigation and state management (`main`, `saved`, `management`)
- `startScrolling()`: Auto-scroll animation engine
- `renderFilters()`: Dynamic, scrollable filter button generation
- `renderPosts()`: Responsive post HTML generation with mobile support
- `loadManagementInterface()`: Loads feed list and conditionally shows admin settings
- `loadFeedList()`: Fetches and renders user's feeds from `/api/feeds`
- `addFeed()`: Adds a feed via `POST /api/feeds` with duplicate error display
- `removeFeed(feedUrl)`: Removes a feed via `DELETE /api/feeds`
- `saveAdminSettings()`: Saves global settings via `POST /api/admin/config` (admin only)
- `manualSync()`: Manual post refresh with loading states
- `toggleSortOrder()`: Client-side post sorting toggle
- `toggleStopScrolling()`: Stop/resume auto-scrolling control
- `formatDate(isoStr)`: Relative time formatting
- `sanitizeHtml(html)`: XSS prevention for post content

### File Structure
```
/
├── server.py              # FastAPI backend with PWA support and Supabase lifespan
├── database.py           # Database class encapsulating Supabase async client
├── config.json           # Global settings (refresh interval, max posts, scroll speed)
├── .env                  # Environment variables (SUPABASE_URL, SUPABASE_KEY)
├── requirements.txt      # Python dependencies
├── static/
│   ├── index.html        # Complete frontend application with PWA
│   ├── manifest.json     # PWA web app manifest
│   ├── sw.js            # Minimal service worker for PWA compliance
│   └── icons/           # PWA application icons
│       ├── icon-72x72.png
│       ├── icon-96x96.png
│       ├── icon-128x128.png
│       ├── icon-144x144.png
│       ├── icon-152x152.png
│       ├── icon-192x192.png
│       ├── icon-384x384.png
│       └── icon-512x512.png
├── .github/
│   └── workflows/
│       └── keep-alive.yml # GitHub Actions keep-alive workflow
└── TelegramUpdates.md    # This comprehensive documentation
```

## Progressive Web App (PWA) Features

### Installation Support
- **Android**: "Add to Home Screen" creates standalone app experience
- **iOS**: "Add to Home Screen" with custom icon and splash screen
- **Desktop**: Chrome/Edge "Install App" option available
- **Standalone Mode**: Runs without browser UI elements when installed

### PWA Configuration
- **Web App Manifest**: Complete metadata for installation
- **Service Worker**: Minimal worker for PWA compliance (no offline caching, no fetch interception)
- **Meta Tags**: Comprehensive mobile and desktop PWA support
- **Icons**: Full icon set (72px to 512px) for all platforms
- **Theme Integration**: Consistent dark theme across installed app

### Technical Implementation
- **Manifest Serving**: Proper MIME type (`application/manifest+json`)
- **Service Worker**: Minimal implementation for PWA recognition; does not intercept fetch events (avoids stripping `Authorization` headers on mobile)
- **Icon Generation**: Automated icon creation from SVG template
- **Mobile Optimization**: Viewport settings for full-screen experience

## Management Interface

### Feed Management (All Users)
- **Per-User Feeds**: Each user manages their own channel list stored in Supabase
- **Add Feed**: Text input + Add button; calls `POST /api/feeds` with server-side duplicate prevention
- **Remove Feed**: Remove button next to each feed URL; calls `DELETE /api/feeds`
- **Duplicate Detection**: Server returns 409 if feed already exists; frontend displays "Feed already exists" error
- **Real-time Updates**: Feed list refreshes immediately after add/remove operations

### Settings Management (Admin Only, `user_id = 1`)
- **Refresh Interval**: Configurable auto-refresh timing (1-60 minutes)
- **Max Posts**: Post limit configuration (5-100 posts)
- **Scroll Speed**: Auto-scroll speed adjustment (10-200 pixels/second)
- **Visibility**: Settings section is only shown when `localStorage.getItem('is_admin') === 'true'`
- **Persistence**: Saved via `POST /api/admin/config` which overwrites `config.json`
