# Telegram Channel Updates Dashboard

A real-time dashboard that displays the latest posts from configured Telegram channels in an auto-scrolling interface.

## Architecture

- **Backend**: FastAPI server (`server.py`) that scrapes public Telegram channel pages (`t.me/s/<channel>`) and fetches private channel posts via Telethon API
- **Database**: Supabase (PostgreSQL) via async Python SDK, encapsulated in `database.py`
- **Frontend**: Single-page vanilla HTML/CSS/JavaScript application (`static/index.html`) with auto-scrolling tiles
- **Configuration**: `config.json` for global settings; per-user channel feeds stored in Supabase `Feeds` table
- **Environment**: `.env` file for secrets (`SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`) loaded via `python-dotenv`
- **Dependencies**: FastAPI, httpx, BeautifulSoup4, supabase, python-dotenv, telethon (see `requirements.txt`)

## Core Features

### 1. Channel Configuration

#### Per-User Feeds (Supabase)
- Each user has their own set of channel feeds stored in the Supabase `Feeds` table
- Feeds are managed via the management interface (⚙️) or the `/api/feeds` API
- Duplicate prevention: adding a feed that already exists for the user returns a 409 error
- **Channel formats supported** (public channels):
  - Plain name: `"channelname"`
  - With @: `"@channelname"`
  - Full URL: `"https://t.me/channelname"`
  - Public preview URL: `"https://t.me/s/channelname"`
- **Private channels**: Stored by numeric channel ID with `is_private=true` flag; fetched via Telethon API
- **Admin-only feeds**: Feeds with `admin_only=true` can only be added by the admin user (`id = 1`); non-admin users are rejected with 403

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

### 2. Data Fetching

#### Public Channel Scraping (`server.py`)
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

#### Private Channel Fetching via Telethon API
- **Method**: `fetch_private_channel_posts(tg_client, channel_id, limit)` uses Telethon's `get_messages()` to fetch recent messages from private channels the session user is a member of
- **Channel identification**: Numeric channel ID (stored in `feed_url` column when `is_private=true`)
- **Entity resolution**: `client.get_entity(channel_id)` resolves channel title and metadata
- **Media handling**: Photos are downloaded as thumbnails via `client.download_media(msg, thumb=-1)`, base64-encoded, and served as `data:image/jpeg;base64,...` URIs in `photo_url`
- **Post URL format**: `https://t.me/c/{channel_id}/{msg_id}` (only accessible to channel members)
- **Channel avatar**: Downloaded via `client.download_profile_photo(entity)` and base64-encoded
- **Concurrency**: Private channel fetches run concurrently via `asyncio.gather()`
- **Channel subscribers**: `entity.participants_count` included as `channel_subscribers` in each post dict for engagement normalization
- **Post format**: Converted to the same dict structure as scraped public posts for seamless merging

#### Post Fetching Pipeline (`GET /api/posts`)
1. Load user's feeds from Supabase (includes `is_private` flag)
2. Split feeds into public (`is_private=false`) and private (`is_private=true`)
3. Public channels: fetched via httpx scraping (unchanged)
4. Private channels: fetched via Telethon `get_messages()` (if Telegram client is available)
5. Merge all posts, sort by datetime (descending), return top `max_posts`

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
- **Admin** (user `id = 1`): Full access to management interface including global settings (refresh interval, max posts, scroll speed), channel feed management, and private channel discovery
- **Regular users** (user `id != 1`): Can manage their own channel feeds only; settings section and private channels section are hidden; cannot add admin-only feeds
- **Backend enforcement**: `/api/admin/*` endpoints return 403 for non-admin users; `POST /api/feeds` rejects admin-only feeds for non-admin users
- **Frontend enforcement**: `is_admin` flag from login response controls settings and private channels section visibility

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
Returns the latest posts from the logged-in user's configured channels (both public and private), sorted by datetime (newest first), limited to `max_posts`.

**Implementation**: 
- Extracts `user_id` from JWT payload
- Queries Supabase `Feeds` table for the user's feeds (including `is_private` flag)
- Splits feeds into public and private
- Public: normalizes channel names, fetches via httpx scraping concurrently
- Private: fetches via Telethon `get_messages()` concurrently (if Telegram client available)
- Merges and sorts all posts by datetime (descending)
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
    "channel_subscribers": 12500,
    "link_preview": {
      "url": "https://example.com",
      "title": "Link Title",
      "description": "Link description",
      "image": "https://cdn.../preview.jpg"
    }
  }
]
```

For private channel posts, `photo_url` and `channel_photo` may be base64 `data:image/jpeg;base64,...` URIs, and `post_url` uses the format `https://t.me/c/{channel_id}/{msg_id}`.

#### `POST /api/top-posts` (protected)
Sends the user's current feed posts to xAI Grok LLM for importance ranking, returns the top 10.

**Request body**:
```json
{ "posts": [ /* array of post objects from /api/posts */ ] }
```

**Implementation**:
- Reads the system prompt from `top10_prompt.md`
- Strips heavy fields (HTML, base64 images) and truncates text to reduce tokens
- Computes per-post engagement ratio (views / channel subscribers * 100) for fair cross-channel comparison
- Calls `https://api.groq.com/openai/v1/chat/completions` with `llama-3.3-70b-versatile` model
- Parses the returned JSON array of indices and maps them back to full post objects

**Success response** (200): JSON array of up to 10 post objects (same format as `/api/posts`)

**Error responses**:
- 400: `{ "detail": "Need at least 10 posts to rank" }` (insufficient posts)
- 502: `{ "detail": "AI service request failed" }` (Grok API error)
- 502: `{ "detail": "Failed to parse AI response" }` (invalid response format)
- 503: `{ "detail": "AI ranking not configured (GROQ_API_KEY missing)" }` (missing API key)

#### `GET /api/feeds` (protected)
Returns the logged-in user's feeds from Supabase as objects with metadata.

**Response format**:
```json
[
  { "feed_url": "https://t.me/channel1", "is_private": false, "admin_only": false },
  { "feed_url": "1234567890", "is_private": true, "admin_only": true }
]
```

#### `POST /api/feeds` (protected)
Adds a feed for the logged-in user with duplicate prevention and admin-only restriction.

**Request body**:
```json
{ "feed_url": "https://t.me/channelname", "is_private": false, "admin_only": false }
```

The `is_private` and `admin_only` fields are optional (default `false`).

**Success response** (200):
```json
{ "status": "success", "feed_url": "https://t.me/channelname" }
```

**Error responses**:
- 409: `{ "detail": "Feed already exists" }` (duplicate)
- 403: `{ "detail": "This channel is restricted to admin" }` (non-admin trying to add admin-only feed)
- 403: `{ "detail": "Only admin can create admin-only feeds" }` (non-admin setting admin_only flag)

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

#### `GET /api/search-channels` (protected)
Searches for public Telegram channels using the Telegram API (Telethon). Requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SESSION` environment variables.

**Query parameter**: `q` (minimum 2 characters)

**Response format**:
```json
[
  {
    "username": "channelname",
    "title": "Channel Display Name",
    "participants_count": 12500
  }
]
```

**Error responses**:
- 503 if Telegram client is not configured/connected
- 500 if the Telegram search request fails

#### `GET /api/admin/channels` (protected, admin-only)
Lists all channels/supergroups the Telethon session is a member of. Used for discovering private channels to add as feeds.

**Response format**:
```json
[
  {
    "id": 1234567890,
    "title": "Channel Name",
    "participants_count": 500,
    "username": null
  }
]
```

**Error responses**:
- 403 for non-admin users
- 503 if Telegram client is not available

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
Serves the main application (`static/index.html`) with `Cache-Control: no-cache, no-store, must-revalidate` headers to prevent stale cached versions.

#### PWA Support
- **`GET /static/manifest.json`**: Web app manifest with correct MIME type
- **`GET /static/sw.js`**: Service worker for PWA compliance (versioned, cache-clearing on activation)

#### Static Files
- **Mount point**: `/static` serves files from `static/` directory
- **Main app**: `static/index.html` contains the entire frontend application
- **PWA assets**: Icons, manifest, and service worker files

### 5. Frontend Interface (`static/index.html`)

#### Design System
- **Themes**: Light/Dark toggle available in the Management (settings) panel for all users
  - Preference stored in `localStorage` (key: `theme`), defaults to light
  - Applied immediately via `data-theme` attribute on `<html>`
  - PWA meta tags (`theme-color`, `background-color`) updated dynamically
- **Dark Theme** (default):
  - Background: `#0e1117`, Cards: `#1a1d27`
  - Text: `#e8eaed` / `#9aa0a6` (secondary)
  - Accent: `#2196f3`, Borders: `#2d3240`
- **Light Theme** (Perplexity-inspired):
  - Background: `#FBFAF4` (Paper White), Cards: `#F1EFE8` (slightly darker paper white)
  - Text: `#091717` (Offblack) / `#5C6B6B` (secondary)
  - Accent: `#20808D` (True Turquoise), Borders: `#E0DED6`
- **Typography**: System font stack (-apple-system, BlinkMacSystemFont, Segoe UI, etc.)
- **Border radius**: 14px consistent rounded corners

#### Navigation System
- **Multi-View Interface**: Four distinct views accessible via header navigation
  - **Main Feed**: Primary channel content (default view, "Main" link on left)
  - **Saved Posts**: Bookmarked posts (bookmark icon in control buttons)
  - **Top 10**: AI-ranked most important posts (❗ icon in control buttons)
  - **Management Interface**: Feed and admin controls (⚙️ icon on top-right)
- **Navigation Layout**: Streamlined header without logo
  - **Left side**: Main navigation link + control buttons
  - **Right side**: Filter bar, status, settings icon, logout button
- **Context-Aware UI**: Different controls shown based on current view; management view hides feed-specific controls (sync, sort, stop/resume, filter) on mobile

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
  - Header edge padding: 8px horizontal for symmetric spacing between first/last icons and screen edges
- **View-Aware Header**: Management view hides feed-specific controls (sync, sort, stop, filter) to declutter the mobile header
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
- **Share button**: Share post via native share sheet or custom popup (see Share System below)
- **Save button**: Bookmark/unbookmark posts for later
- **Filter buttons**: Toggle between "All" and individual channels
- **Control buttons**: Manual sync, sort order toggle, and stop/resume scrolling
- **Navigation links**: Switch between Main (left), Saved (control bar), and Management (top-right) views
- **Logout button**: Clears session and returns to login form
- **Login form**: Username/password inputs with error message display
- **Hover effects**: Border color changes, text color transitions

#### Share System
Each post tile has a Share button in the footer (between views count and save button).

- **Native share** (`navigator.share`): On browsers supporting the Web Share API (iOS Safari, Chrome, etc.), tapping Share opens the native OS share sheet with the post URL. The promise resolves when the user completes or dismisses the share sheet.
- **Custom share popup fallback**: When `navigator.share` is unavailable (e.g., Brave on Android with Shields enabled), a custom popup appears with:
  - **WhatsApp**: Opens `https://api.whatsapp.com/send?text={url}`
  - **Telegram**: Opens `https://t.me/share/url?url={url}`
  - **Email**: Opens `mailto:?body={url}`
  - **Copy Link**: Copies the post URL to clipboard with "Copied!" feedback
- **Popup behavior**: Anchored near the share button, dismisses on outside tap, themed with CSS variables
- **Click safety**: `event.stopPropagation()` prevents triggering the tile's open-in-Telegram action
- **Feedback**: Button label briefly flashes "Shared" (native) or popup provides per-action feedback

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

#### AI Top 10 Important Posts
- **Trigger**: "!" icon button in the header control bar
- **Flow**: Sends all current feed posts to Groq-hosted LLM (`llama-3.3-70b-versatile`) for analysis, receives the top 10 most important posts ranked by impact, relevancy, engagement ratio, cross-references, depth, and media richness
- **View**: Dedicated `top10` view in the multi-view interface; shows AI-ranked posts in the standard feed layout
- **Loading State**: Spinner with "Analyzing posts with AI..." message during the API call
- **Status Bar**: Shows "Top 10 — {time}" after completion
- **Manual Sync**: Refresh button clears cached posts and re-runs the full analysis
- **Prompt**: System prompt stored in editable `top10_prompt.md` file; can be customized without code changes
- **Engagement Normalization**: Backend computes engagement ratio (views / channel subscribers * 100) per post so the LLM can compare fairly across channels of different sizes
- **Token Efficiency**: Backend strips heavy fields (HTML, base64 images) and truncates text before sending to the LLM
- **Error Handling**: Graceful error display if AI service is unavailable or misconfigured

#### Content Processing
- **HTML sanitization**: `sanitizeHtml()` function removes `onclick`, adds security attributes
- **Date formatting**: Relative time display (minutes/hours/days ago) with fallback to date
- **Image loading**: Lazy loading with `loading="lazy"` attribute
- **Error handling**: Loading states, error messages, empty state handling

### 6. Data Flow

#### Initialization and Configuration
1. **Server Lifespan**: `load_dotenv()` loads `.env`, then the lifespan handler initializes `Database`
2. **Auth Gate**: On page load, frontend checks `localStorage` for a JWT token; if missing, shows login form instead of the app
3. **Login**: User authenticates via `POST /api/login`; on success, token, username, and `is_admin` flag are stored and `init()` is called
4. **Frontend Startup**: `init()` function initializes multi-view interface (only called after successful auth)
5. **Event Listeners**: Set up navigation, control buttons, feed management buttons, private channel loading, logout, and PWA service worker
6. **Configuration**: Fetch `/api/config` (with auth header) to get `refresh_interval_minutes` and `scroll_speed`
7. **Default View**: Load Main feed as default view

#### View Data Management
- **View Switching**: `showView(view)` function manages interface state; resets inline display styles to avoid overriding CSS defaults (e.g. `display: contents` on mobile)
  - **Main View**: Fetches from `/api/posts` (user's channels from Supabase -- both public and private); restores all control buttons and filter
  - **Saved View**: Fetches from `/api/saved` (bookmarked posts); restores all control buttons and filter
  - **Management View**: Loads feed management interface (and admin settings + private channels if admin); hides sync, sort, stop/resume buttons and filter wrapper
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
1. **Feed Loading**: Fetch user's feeds via `GET /api/feeds` (returns objects with `feed_url`, `is_private`, `admin_only`)
2. **Feed Display**: Render feed list with badges (Private, Admin) and remove buttons
3. **Add Feed**: Input field + Add button; calls `POST /api/feeds` with duplicate detection (409 → "Feed already exists" message)
4. **Remove Feed**: Remove button calls `DELETE /api/feeds` and refreshes the list
5. **Private Channels** (admin only): "Load My Telegram Channels" button calls `GET /api/admin/channels`, displays available channels with "Add" buttons; adding sends `POST /api/feeds` with `is_private: true, admin_only: true`
6. **Admin Settings** (admin only): Load settings via `GET /api/admin/config`, save via `POST /api/admin/config`

#### PWA Integration
1. **Service Worker**: Register versioned service worker (v2.0) for PWA compliance; clears legacy caches on activation
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
| Column       | Type    | Description                                               |
|--------------|---------|-----------------------------------------------------------|
| `user_id`    | bigint  | Foreign key to `Users.id` (NOT NULL)                      |
| `feed_url`   | text    | Telegram channel URL or numeric channel ID (for private)  |
| `is_private` | boolean | Whether the channel requires Telethon API (default false) |
| `admin_only` | boolean | Whether only admin can have this feed (default false)     |

- **Foreign Key**: `user_id` references `Users.id` with `ON DELETE CASCADE`
- **Index**: `idx_feeds_user_id` on `user_id` for join/RLS performance
- **RLS**: Row Level Security requires appropriate policies for the API key to read/write rows
- **Duplicate Prevention**: Application-level check before insert (query for existing `user_id` + `feed_url` pair)
- **Migration SQL**: `ALTER TABLE feeds ADD COLUMN is_private boolean DEFAULT false; ALTER TABLE feeds ADD COLUMN admin_only boolean DEFAULT false;`

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
- `add_feed(user_id, feed_url, is_private=False, admin_only=False) -> dict | None` (async): Checks for existing duplicate; if none, inserts a new row with `is_private` and `admin_only` flags. Returns `None` if duplicate exists
- `remove_feed(user_id, feed_url) -> bool` (async): Deletes the matching feed row. Returns `True` if a row was deleted
- `is_feed_admin_only(feed_url) -> bool` (async): Checks if any feed row with this URL has `admin_only=true`
- `get_saved_posts(user_id) -> list[dict]` (async): Queries `save_for_later` for all rows matching the user, parses JSON `saved_post` column, returns list of post dicts with `saved_at` from `created_at`
- `save_post(user_id, post) -> dict | None` (async): Checks for duplicate (matching `channel` + `post_id` in existing saved posts); if none, inserts a new row with JSON-serialized post. Returns `None` if duplicate
- `unsave_post(user_id, channel, post_id) -> bool` (async): Finds and deletes the saved post row matching `channel` and `post_id` for the user. Returns `True` if a row was deleted

#### Startup Behavior
- On server startup (via FastAPI lifespan), the `Database` is initialized

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
telethon           # Telegram API client for channel search and private channel access
```

**Key imports in `server.py`**:
- `asyncio` - Concurrent channel fetching
- `base64` - Encoding private channel media as data URIs
- `io` - In-memory byte buffers for media downloads
- `json` - Config file parsing
- `logging` - Request/error logging
- `re` - Image URL extraction from CSS
- `secrets` - JWT secret generation
- `pathlib.Path` - File path handling
- `contextlib.asynccontextmanager` - FastAPI lifespan management
- `jwt` (PyJWT) - JWT token encoding/decoding for authentication
- `fastapi` - Web framework, responses, static files, `Depends` for auth middleware
- `dotenv.load_dotenv` - Environment variable loading
- `telethon` - Telegram API client for channel search, private channel message fetching, and dialog listing (TelegramClient, StringSession, functions.contacts.SearchRequest, Channel, MessageMediaPhoto, MessageMediaDocument)
- `database.Database` - Supabase database abstraction

## Technical Specifications

### Error Handling
- **Network failures**: 
  - Backend: `try/except` blocks log errors, return `None` for failed channels
  - Frontend: Failed API calls show error message and red status dot
- **Parse errors**: `BeautifulSoup` parsing errors don't crash the server
- **Empty responses**: Frontend shows "No posts to display" message
- **Graceful degradation**: Missing data fields handled with fallbacks
- **Private channel errors**: Failed Telethon fetches return empty list, errors logged

### Performance
- **Async I/O**: `httpx.AsyncClient` with `asyncio.gather()` for concurrent requests
- **No caching**: Fresh data on every request (trade-off: freshness vs speed)
- **Timeout**: 15-second timeout per HTTP request
- **Private channels**: Base64-encoded media increases response size but avoids separate media endpoints
- **Top 10 AI ranking**: 
  - Posts capped at 50 before sending to LLM to stay within token limits
  - Post data stripped to compact keys (`i`, `ch`, `t`, `dt`, `v`, `e`, `m`, `lp`) — no HTML, base64 images, or full URLs
  - Text truncated to 200 chars per post
  - Engagement ratio computed server-side to avoid burdening the LLM with subscriber math
  - Frontend caches main feed posts (`mainFeedPostsCache`) to avoid re-fetching when switching to Top 10 view
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
  - Admin endpoints (`/api/admin/*`) check `user_id == 1` and return 403 for non-admin users
  - Feed endpoints (`/api/feeds`) are scoped to the logged-in user's `user_id`; admin-only feeds rejected for non-admin users
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
- **Telegram API credentials**: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_SESSION` in `.env`; optional -- channel search and private channel access are disabled if not configured
- **Groq API credentials**: `GROK_API_KEY` in `.env`; optional -- Top 10 AI ranking feature returns 503 if not configured
- **Telegram session management**: Separate sessions recommended for dev and prod to avoid revocation; `generate_session.py` script supports creating labeled sessions ("TGUpdates-Dev" / "TGUpdates-Prod")
- **Startup logging**: No user or feed data is logged at startup
- **Service worker**: Does not intercept fetch requests (avoids stripping `Authorization` headers on mobile browsers); versioned with cache-clearing on activation
- **Cache control**: Root HTML response served with `no-cache, no-store, must-revalidate` to prevent stale frontend versions

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

1. **Rate limiting**: Subject to Telegram's rate limiting on public preview pages and API calls
2. **Content restrictions**: Some media may not be accessible in web preview format
3. **Network dependencies**: Requires outbound HTTP access to `t.me` (may be blocked in some hosting environments)
4. **No real-time updates**: Relies on periodic polling rather than WebSocket/SSE
5. **Memory usage**: All posts kept in memory (no pagination or cleanup)
6. **Single-threaded**: FastAPI runs in single process (no horizontal scaling)
7. **Keep-alive dependency**: Render.com free tier requires external pinging to prevent sleep
8. **PWA offline limitations**: No offline functionality - requires internet connection
9. **Supabase RLS**: Row Level Security must have appropriate policies for the API key to read/write the `Users` and `Feeds` tables
10. **Plain text passwords**: Passwords are stored and compared as plain text in Supabase (no hashing)
11. **JWT secret lifetime**: JWT secret is generated per server process; restarting the server invalidates all active sessions
12. **Admin detection**: Admin role is determined by `user_id == 1` (hardcoded); no dynamic role management
13. **Private channel media**: Photos from private channels are base64-encoded in API responses, increasing payload size
14. **Telegram session sharing**: Using the same Telegram session from multiple IPs simultaneously can cause revocation; separate dev/prod sessions recommended
15. **Share API availability**: Some mobile browsers (e.g., Brave with Shields) strip `navigator.share`; custom share popup used as fallback

## Implementation Details

### Key Functions

#### Backend (`server.py`)
- `require_auth(request) -> dict`: FastAPI dependency that verifies JWT `Authorization: Bearer` header; returns decoded payload with `user_id` and `user_name`
- `lifespan(app)`: Async context manager that initializes `Database` and Telegram client at startup
- `login(request)`: `POST /api/login` handler -- authenticates credentials, returns JWT token with `user_id`, and `is_admin` flag
- `get_posts(request, user)`: `GET /api/posts` -- fetches user's feeds from Supabase, splits into public/private, scrapes public channels and fetches private channels via Telethon, returns merged sorted posts
- `get_feeds(request, user)`: `GET /api/feeds` -- returns user's feeds as objects with `feed_url`, `is_private`, `admin_only`
- `add_feed(request, user)`: `POST /api/feeds` -- adds a feed with duplicate check, admin-only restriction enforcement, and `is_private`/`admin_only` support (409 on conflict, 403 on admin-only violation)
- `delete_feed(request, user)`: `DELETE /api/feeds` -- removes a feed for the user
- `search_channels(request, q, user)`: `GET /api/search-channels` -- searches Telegram for public channels via Telethon `contacts.SearchRequest`
- `get_admin_channels(request, user)`: `GET /api/admin/channels` -- lists all channels the Telethon session is a member of (admin-only)
- `get_full_config(user)`: `GET /api/admin/config` -- returns config.json (admin-only, 403 for non-admin)
- `update_config(request, user)`: `POST /api/admin/config` -- updates config.json (admin-only, 403 for non-admin)
- `get_top_posts(request, user)`: `POST /api/top-posts` -- receives posts array, computes per-post engagement ratio (views/subscribers), sends stripped-down versions to Groq LLM (`llama-3.3-70b-versatile`) with system prompt from `top10_prompt.md`, parses response indices, returns top 10 full post objects
- `normalize_channel(raw: str) -> str`: Extracts channel name from various URL formats
- `fetch_channel_html(client, channel) -> str | None`: Async HTTP request with error handling
- `extract_image_url(style: str) -> str | None`: Regex extraction from CSS `url()` values
- `parse_channel_posts(html: str, channel: str) -> list[dict]`: BeautifulSoup parsing logic
- `fetch_private_channel_posts(tg, channel_id, limit) -> list[dict]`: Telethon-based message fetching with base64 media encoding
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
- `Database.add_feed(user_id, feed_url, is_private, admin_only) -> dict | None`: Inserts a feed with duplicate check and private/admin-only flags; returns `None` if duplicate
- `Database.remove_feed(user_id, feed_url) -> bool`: Deletes a feed row; returns whether deletion occurred
- `Database.is_feed_admin_only(feed_url) -> bool`: Checks if any feed with this URL is marked admin_only
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
- `showView(view)`: Multi-view navigation and state management (`main`, `saved`, `management`); resets inline display styles to preserve CSS defaults; hides feed-specific controls in management view
- `startScrolling()`: Auto-scroll animation engine
- `renderFilters()`: Dynamic, scrollable filter button generation
- `renderPosts()`: Responsive post HTML generation with mobile support
- `handleShareClick(evt, postUrl, btn)`: Click handler for share button with propagation control
- `sharePost(postUrl, btn)`: Tries `navigator.share` first, falls back to custom share popup
- `showSharePopup(postUrl, btn)`: Creates and positions the custom share popup with WhatsApp/Telegram/Email/Copy options
- `hideSharePopup()`: Removes any open share popup and overlay
- `copyTextFallback(text)`: Clipboard fallback using `execCommand('copy')` for older browsers
- `flashShareButton(btn, label)`: Briefly changes the share button label for feedback
- `loadManagementInterface()`: Loads feed list, private channels section (admin), and settings (admin)
- `loadFeedList()`: Fetches and renders user's feeds from `/api/feeds` with Private/Admin badges
- `addFeed()`: Adds a feed via `POST /api/feeds` with duplicate error display
- `loadPrivateChannels()`: Fetches and displays available private channels from `GET /api/admin/channels` (admin only)
- `addPrivateChannel(channelId, btn)`: Adds a private channel as admin-only feed via `POST /api/feeds`
- `setupAutocomplete()`: Initializes channel search autocomplete on the feed input (debounce, keyboard navigation, blur handling)
- `searchChannels(query)`: Calls `GET /api/search-channels?q=` and populates the autocomplete dropdown
- `showAutocomplete(items)` / `hideAutocomplete()`: Renders or hides the autocomplete dropdown
- `selectAutocompleteItem(index)`: Populates the feed input with the selected channel's URL
- `fetchTop10Posts()`: Sends current feed posts to `POST /api/top-posts` for AI ranking; displays loading state, renders top 10 results, handles errors
- `removeFeed(feedUrl)`: Removes a feed via `DELETE /api/feeds`
- `saveAdminSettings()`: Saves global settings via `POST /api/admin/config` (admin only)
- `manualSync()`: Manual post refresh with loading states
- `toggleSortOrder()`: Client-side post sorting toggle
- `toggleStopScrolling()`: Stop/resume auto-scrolling control
- `setTheme(theme)`: Applies light/dark theme via `data-theme` attribute, updates meta tags, saves to `localStorage`, and highlights the active button in the management panel
- `updateThemeButtons(theme)`: Syncs the active state of theme toggle buttons in the management panel
- `formatDate(isoStr)`: Relative time formatting
- `sanitizeHtml(html)`: XSS prevention for post content

### File Structure
```
/
├── server.py              # FastAPI backend with PWA support, Supabase lifespan, and Telethon integration
├── database.py            # Database class encapsulating Supabase async client
├── config.json            # Global settings (refresh interval, max posts, scroll speed)
├── top10_prompt.md        # Editable system prompt for Grok LLM Top 10 ranking
├── generate_session.py    # Telethon StringSession generator (dev/prod labeled sessions)
├── .env                   # Environment variables (SUPABASE_URL, SUPABASE_KEY, TELEGRAM_*, GROK_API_KEY)
├── requirements.txt       # Python dependencies
├── static/
│   ├── index.html         # Complete frontend application with PWA and share system
│   ├── manifest.json      # PWA web app manifest
│   ├── sw.js              # Versioned service worker (v2.0) with cache-clearing
│   └── icons/             # PWA application icons
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
└── TelegramUpdates.md     # This comprehensive documentation
```

## Progressive Web App (PWA) Features

### Installation Support
- **Android**: "Add to Home Screen" creates standalone app experience
- **iOS**: "Add to Home Screen" with custom icon and splash screen
- **Desktop**: Chrome/Edge "Install App" option available
- **Standalone Mode**: Runs without browser UI elements when installed

### PWA Configuration
- **Web App Manifest**: Complete metadata for installation
- **Service Worker**: Versioned worker (v2.0) for PWA compliance; clears legacy caches on activation (no offline caching, no fetch interception)
- **Meta Tags**: Comprehensive mobile and desktop PWA support
- **Icons**: Full icon set (72px to 512px) for all platforms
- **Theme Integration**: Consistent dark theme across installed app

### Technical Implementation
- **Manifest Serving**: Proper MIME type (`application/manifest+json`)
- **Service Worker**: Versioned with `SW_VERSION` constant; `skipWaiting()` on install, cache-clearing + `clients.claim()` on activate; does not intercept fetch events (avoids stripping `Authorization` headers on mobile)
- **Icon Generation**: Automated icon creation from SVG template
- **Mobile Optimization**: Viewport settings for full-screen experience
- **Cache Busting**: Root HTML served with `no-cache` headers; service worker version bump forces re-activation

## Management Interface

### Feed Management (All Users)
- **Per-User Feeds**: Each user manages their own channel list stored in Supabase
- **Add Feed**: Text input with autocomplete + Add button; calls `POST /api/feeds` with server-side duplicate prevention
- **Channel Autocomplete**: As the user types in the feed input, the frontend queries `GET /api/search-channels?q=` (debounced 300ms) and displays a dropdown of matching public Telegram channels with title, @username, and member count. Selecting a result populates the input with the channel URL. Keyboard navigation (Arrow Up/Down, Enter, Escape) is supported. Autocomplete is skipped when the input looks like a URL.
- **Remove Feed**: Remove button next to each feed URL; calls `DELETE /api/feeds`
- **Feed Badges**: Each feed displays badges indicating its type:
  - **Private** (blue): Channel fetched via Telethon API
  - **Admin** (red): Channel restricted to admin user
- **Duplicate Detection**: Server returns 409 if feed already exists; frontend displays "Feed already exists" error
- **Real-time Updates**: Feed list refreshes immediately after add/remove operations

### Private Channels (Admin Only)
- **Discovery**: "Load My Telegram Channels" button fetches all channels the Telethon session is a member of via `GET /api/admin/channels`
- **Channel List**: Shows channel title, @username (if any), and member count
- **Add Button**: Each channel has an "Add" button that calls `POST /api/feeds` with `is_private: true, admin_only: true`
- **Already Added**: Channels already in the feed list show "Added" instead of the button
- **Requirements**: Telethon client must be connected (requires `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`)

### Appearance (All Users)
- **Theme Toggle**: Light/Dark buttons in the Management panel
- **Persistence**: Stored in `localStorage` (key: `theme`), no server round-trip
- **Default**: Light theme if no preference is stored
- **Light Theme**: Perplexity AI-inspired color scheme (Paper White, Offblack text, True Turquoise accents)

### Settings Management (Admin Only, `user_id = 1`)
- **Refresh Interval**: Configurable auto-refresh timing (1-60 minutes)
- **Max Posts**: Post limit configuration (5-100 posts)
- **Scroll Speed**: Auto-scroll speed adjustment (10-200 pixels/second)
- **Visibility**: Settings section is only shown when `localStorage.getItem('is_admin') === 'true'`
- **Persistence**: Saved via `POST /api/admin/config` which overwrites `config.json`
