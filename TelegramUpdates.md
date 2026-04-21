# Telegram Channel Updates Dashboard

A real-time dashboard that displays the latest posts from configured Telegram channels in an auto-scrolling interface.

## Architecture

- **Backend**: FastAPI server (`server.py`) that scrapes public Telegram channel pages (`t.me/s/<channel>`) and fetches private channel posts via Telethon API
- **Database**: Supabase (PostgreSQL) via async Python SDK, encapsulated in `database.py`
- **Frontend**: Single-page vanilla HTML/CSS/JavaScript application (`static/index.html`) with auto-scrolling tiles
- **Configuration**: `config.json` for global settings; per-user channel feeds stored in Supabase `Feeds` table
- **Environment**: `.env` file for secrets (`SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`, `GROK_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `TAVILY_API_KEY`) loaded via `python-dotenv`
- **Dependencies**: FastAPI, httpx, BeautifulSoup4, supabase, python-dotenv, telethon (see `requirements.txt`)

## Core Features

### 1. Video Streaming
- **In-Feed Playback**: Videos play directly in the feed with custom controls (no external player required)
- **Fullscreen Support**: Dedicated fullscreen button with seamless playback continuation
- **Custom Controls**: Play/pause, mute/unmute, seek timeline, and fullscreen toggle
- **Progress Bar**: Interactive seek bar showing current playback position and total duration
- **Responsive Layout**: 
  - **Desktop/Landscape**: Controls positioned below the video in a horizontal toolbar
  - **Mobile Landscape**: Controls positioned vertically on the right side to utilize unused screen space
  - **Fullscreen Mode**: Adaptive positioning based on video orientation
    - **Landscape videos**: Controls on the right side (vertical) with black background and white icons
    - **Portrait videos**: Controls at the bottom (horizontal) to maximize video visibility
- **Mobile Fullscreen Enhancements**:
  - **Orientation Lock**: Best-effort landscape orientation lock for landscape videos (Android Chrome support)
  - **Portrait Video Support**: Portrait videos remain in portrait orientation with bottom controls
  - **Playback Resilience**: Multi-retry resume logic ensures video continues playing through fullscreen transitions
  - **Auto-Pause Protection**: Videos in fullscreen are never auto-paused by viewport visibility detection
- **Theme Integration**: Controls bar matches the app's theme in normal view (card color in both light and dark themes)
- **Playback Continuity**: Video continues playing when entering/exiting fullscreen mode with robust retry mechanism
- **Native Video API**: Uses HTML5 video element with custom UI overlay for consistent cross-browser experience

### 2. Better Image Handling
- **Dynamic Aspect Ratio Sizing**: Image containers automatically adapt to each image's natural dimensions (portrait, landscape, or square)
- **No Cropping**: All images displayed with `object-fit: contain` to show the full image without any cropping
- **Responsive Scaling**: Images scale dynamically based on orientation while respecting a 70vh maximum height cap for optimal viewing
- **In-App Fullscreen Viewer**: Tapping an image opens an in-app fullscreen viewer instead of navigating to the external Telegram post
  - **Back Button Integration**: Mobile back button closes the image viewer before exiting the app
  - **History State Management**: Image viewer integrates with browser history for proper navigation flow
- **Portrait Image Optimization**: Portrait images get larger dedicated vertical space instead of being constrained to landscape containers
- **Lazy Loading**: Images load on-demand with `loading="lazy"` for improved performance
- **Background Consistency**: Image containers use the theme's media background color for visual cohesion

### 3. Channel Configuration

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
- **Alternate feeds**: Feeds with `is_alternate=true` belong to a separate curated feed managed by the admin; the same channel URL can exist in both the main and alternate feed independently

#### Global Settings (`config.json`)
- **File**: `config.json`
- Contains only global settings (no channel lists):
  ```json
  {
    "refresh_interval_minutes": 5,
    "max_posts": 100,
    "scroll_speed": 50,
    "media_concurrency": 20,
    "ai_provider": "gemini",
    "ai_model": "gemini-2.5-flash",
    "context_provider": "gemini",
    "context_mistral_model": "mistral-large-latest",
    "context_tavily_depth": "basic",
    "context_tavily_max_results": 5,
    "alternate_feed_max_posts": 100
  }
  ```
- **Settings**:
  - `refresh_interval_minutes`: Auto-refresh interval for main feed (1-60 minutes)
  - `max_posts`: Maximum posts to display in main feed (5-100)
  - `scroll_speed`: Auto-scroll speed in pixels/second (10-200)
  - `media_concurrency`: Concurrent media downloads (1-20)
  - `ai_provider`: AI provider for Top 10 ranking - "gemini", "mistral", or "groq"
  - `ai_model`: Model name for the selected AI provider
  - `context_provider`: Context search provider for "More" button - "gemini" or "mistral"
  - `context_mistral_model`: Mistral model for context search (when `context_provider` is "mistral")
  - `context_tavily_depth`: Tavily search depth - "basic" or "advanced" (when `context_provider` is "mistral")
  - `context_tavily_max_results`: Number of search results to fetch (3-10, when `context_provider` is "mistral")
  - `alternate_feed_max_posts`: Maximum posts to return from the alternate feed endpoint (5-500)
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
- **Grouped media merging**: `_merge_grouped_posts()` detects consecutive post IDs where some lack text (media album groups) and merges them into a single post, keeping the first media and the text from whichever item has it

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
- **Grouped media merging**: `_merge_telethon_grouped()` uses Telethon's `grouped_id` attribute to detect album messages and merges them into a single post per group

#### Post Fetching Pipeline (`GET /api/posts`)
1. Load user's feeds from Supabase (includes `is_private` and `is_alternate` flags)
2. Filter out feeds where `is_alternate=true` (alternate feed has its own endpoint)
3. Split remaining feeds into public (`is_private=false`) and private (`is_private=true`)
4. Public channels: fetched via httpx scraping (unchanged)
5. Private channels: fetched via Telethon `get_messages()` (if Telegram client is available)
6. Merge all posts, sort by datetime (descending), return top `max_posts`

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
Returns the latest posts from the logged-in user's configured main-feed channels (both public and private), sorted by datetime (newest first), limited to `max_posts`. Feeds marked as `is_alternate=true` are excluded.

**Implementation**: 
- Extracts `user_id` from JWT payload
- Queries Supabase `Feeds` table for the user's feeds (including `is_private` flag)
- Filters out feeds where `is_alternate=true`
- Splits remaining feeds into public and private
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
Sends the user's current feed posts to a configurable AI provider (Google Gemini or Groq) for importance ranking, returns the top 10.

**Request body**:
```json
{ "posts": [ /* array of post objects from /api/posts */ ] }
```

**Implementation**:
- Reads the system prompt from `top10_prompt.md`
- Strips heavy fields (HTML, base64 images) and truncates text to reduce tokens
- Computes per-post engagement ratio (views / channel subscribers * 100) for fair cross-channel comparison
- Reads `ai_provider` and `ai_model` from `config.json`
- Gemini path: calls `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with `responseMimeType: "application/json"`
- Groq path: calls `https://api.groq.com/openai/v1/chat/completions`
- Parses the returned JSON array of indices and maps them back to full post objects
- On error: logs response body and forwards the API's error message to the frontend

**Success response** (200): JSON array of up to 10 post objects (same format as `/api/posts`)

**Error responses**:
- 400: `{ "detail": "Need at least 10 posts to rank" }` (insufficient posts)
- 502: `{ "detail": "AI service request failed" }` (API error)
- 502: `{ "detail": "Failed to parse AI response" }` (invalid response format)
- 503: `{ "detail": "AI ranking not configured (GOOGLE_API_KEY missing)" }` or `{ "detail": "AI ranking not configured (GROQ_API_KEY missing)" }` depending on provider

#### `POST /api/context-summary` (protected)
Generates contextual information for a Telegram post using web search. Supports two providers: Google Gemini (with built-in Google Search grounding) or Mistral AI + Tavily (explicit web search). Provider is configurable via `context_provider` in `config.json`.

**Request body**:
```json
{ "post_text": "The Telegram post text to analyze" }
```

**Implementation**:

**Gemini Provider** (`context_provider: "gemini"`):
- Uses Google Gemini API with Google Search grounding tool
- Reads system prompt from `context_summary_prompt.md`
- Calls `gemini-2.5-flash` model with search tool enabled
- Extracts sources from grounding metadata

**Mistral + Tavily Provider** (`context_provider: "mistral"`):
- Uses `WebSearch` class from `web_search.py`
- Step 1: Generates search terms using Mistral AI (reads `search_terms.md` prompt)
- Step 2: Searches web using Tavily API with configurable depth and max results
- Step 3: Summarizes results using Mistral AI (reads `summarize.md` prompt)
- Configuration: `context_mistral_model`, `context_tavily_depth`, `context_tavily_max_results` from `config.json`

**Success response** (200):
```json
{
  "summary": "Contextual information about the post",
  "sources": [
    { "title": "Source Title", "url": "https://example.com" }
  ]
}
```

**Error responses**:
- 400: `{ "detail": "post_text is required" }` (missing post text)
- 500: `{ "detail": "context_summary_prompt.md not found" }` (Gemini: missing prompt file)
- 502: `{ "detail": "Failed to generate context summary: ..." }` (API error)
- 503: `{ "detail": "Context summary not configured (GOOGLE_API_KEY missing)" }` (Gemini provider)
- 503: `{ "detail": "Context summary not configured (MISTRAL_API_KEY missing)" }` (Mistral provider)
- 503: `{ "detail": "Context summary not configured (TAVILY_API_KEY missing)" }` (Mistral provider)

#### `GET /api/alternate-posts` (protected, admin-only)
Returns the latest posts from all alternate-feed channels (feeds where `is_alternate=true`), sorted by datetime (newest first), limited to `alternate_feed_max_posts` from `config.json`.

**Implementation**:
- Admin-only (`user_id == 1`); returns 403 for non-admin users
- Queries all feeds where `is_alternate=true` (global, not user-scoped)
- Splits into public and private feeds
- Fetches posts via Telethon (if available) or httpx scraping (fallback)
- Returns merged, sorted posts capped at `alternate_feed_max_posts`

**Response format**: Same JSON array of post objects as `GET /api/posts`.

**Error responses**:
- 403: `{ "detail": "Admin access required" }` (non-admin user)

#### `GET /api/feeds` (protected)
Returns the logged-in user's feeds from Supabase as objects with metadata.

**Response format**:
```json
[
  { "feed_url": "https://t.me/channel1", "is_private": false, "admin_only": false, "is_alternate": false },
  { "feed_url": "1234567890", "is_private": true, "admin_only": true, "is_alternate": false }
]
```

#### `POST /api/feeds` (protected)
Adds a feed for the logged-in user with duplicate prevention and admin-only restriction.

**Request body**:
```json
{ "feed_url": "https://t.me/channelname", "is_private": false, "admin_only": false, "is_alternate": false }
```

The `is_private`, `admin_only`, and `is_alternate` fields are optional (default `false`). Setting `is_alternate` to `true` requires admin access. The same channel URL can exist as both a main feed and an alternate feed without conflict.

**Success response** (200):
```json
{ "status": "success", "feed_url": "https://t.me/channelname" }
```

**Error responses**:
- 409: `{ "detail": "Feed already exists" }` (duplicate within the same feed type)
- 403: `{ "detail": "This channel is restricted to admin" }` (non-admin trying to add admin-only feed)
- 403: `{ "detail": "Only admin can create admin-only feeds" }` (non-admin setting admin_only flag)
- 403: `{ "detail": "Only admin can create alternate feed channels" }` (non-admin setting is_alternate flag)

#### `DELETE /api/feeds` (protected)
Removes a feed for the logged-in user. The `is_alternate` flag ensures only the correct feed type is deleted (a channel can exist in both main and alternate feeds).

**Request body**:
```json
{ "feed_url": "https://t.me/channelname", "is_alternate": false }
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
  "max_posts": 100,
  "scroll_speed": 50,
  "media_concurrency": 20,
  "alternate_feed_max_posts": 100,
  "ai_provider": "gemini",
  "ai_model": "gemini-2.5-flash"
}
```

#### `POST /api/track/share` (protected)
Tracks share actions for analytics and logging purposes.

**Request body**:
```json
{
  "shared_to": "whatsapp",
  "post_url": "https://t.me/channel/12345"
}
```

**Success response** (200):
```json
{ "status": "success" }
```

**Implementation**: Frontend calls this endpoint when users share posts via native share or custom share popup (WhatsApp, Telegram, Email, Copy). Logs include username, share method, and post URL.

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
- **Multi-View Interface**: Five distinct views accessible via header navigation
  - **Main Feed**: Primary channel content (default view, "Home" link on left)
  - **Alternate Feed**: Admin-curated secondary feed (accessible via Home button popup, admin only)
  - **Saved Posts**: Bookmarked posts (bookmark icon in control buttons)
  - **Top 10**: AI-ranked most important posts (❗ icon in control buttons)
  - **Management Interface**: Feed and admin controls (⚙️ icon on top-right)
- **Home Button Popup** (admin only): Clicking the Home button shows a popup menu with "Main Feed" and "Alternate Feed" options; non-admin users go directly to the main feed. Clicking outside the popup dismisses it.
- **Navigation Layout**: Streamlined header without logo
  - **Left side**: Home navigation link + control buttons
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
- **Flow**: Sends all current feed posts to a configurable AI provider (default: Google Gemini `gemini-2.5-flash`) for analysis, receives the top 10 most important posts ranked by impact, relevancy, engagement ratio, cross-references, depth, and media richness
- **View**: Dedicated `top10` view in the multi-view interface; shows AI-ranked posts in the standard feed layout
- **Loading State**: Spinner with "Analyzing posts with AI..." message during the API call
- **Status Bar**: Shows "Top 10 — {time}" after completion
- **Floating Bubble**: On successful load, a floating bubble notification displays "AI filtered top 10 posts" for 3 seconds (CSS animation, auto-removed from DOM)
- **Manual Sync**: Refresh button clears cached posts and re-runs the full analysis
- **Frontend Caching**: Top 10 results are cached in `top10Cache`; pressing "!" again serves from cache unless the main feed was refreshed since the last analysis (`feedRefreshedSinceTop10` flag). Manual sync always forces a fresh API call.
- **Prompt**: System prompt stored in editable `top10_prompt.md` file; can be customized without code changes
- **Exclusions**: The prompt instructs the LLM to skip missile/rocket alert posts (real-time siren notifications) as they have no analytical value in a ranking
- **Multi-Provider Support**: Backend supports Google Gemini and Groq APIs; provider and model configurable via admin settings (`ai_provider`, `ai_model` in `config.json`)
- **Engagement Normalization**: Backend computes engagement ratio (views / channel subscribers * 100) per post so the LLM can compare fairly across channels of different sizes
- **Token Efficiency**: Backend strips heavy fields (HTML, base64 images) and truncates text before sending to the LLM
- **Error Handling**: Graceful error display if AI service is unavailable or misconfigured

#### Context Search ("More" Button)
- **Trigger**: "More" button in each post's footer (between views count and save button)
- **Purpose**: Provides additional context and background information for a post by searching the web and synthesizing results
- **UI**: Expandable section below the post that displays the summary and sources
- **Loading State**: Spinner with "Searching the web..." message during the API call
- **Multi-Provider Support**: Two providers available, configurable via admin settings (`context_provider` in `config.json`):
  - **Google Gemini** (default): Uses Gemini 2.5 Flash with built-in Google Search grounding tool
    - System prompt: `context_summary_prompt.md`
    - Automatically searches and grounds responses in web results
    - Sources extracted from grounding metadata
  - **Mistral AI + Tavily**: Uses Mistral AI for analysis and Tavily API for web search
    - Three-step process: generate search terms → search web → summarize results
    - Prompts: `search_terms.md` (term extraction), `summarize.md` (synthesis)
    - Configurable: model (`context_mistral_model`), search depth (`context_tavily_depth`), max results (`context_tavily_max_results`)
- **Language Support**: Both providers support Hebrew and English; responses match the input language
- **Display**: Summary text with bullet points, followed by clickable source links
- **Interaction**: Click "More" again or "Close" link to collapse the summary
- **Single Expansion**: Only one context summary can be open at a time; opening a new one closes the previous
- **Error Handling**: Graceful error display if API keys are missing or service is unavailable

#### Alternate Feed (Admin Only)
- **Purpose**: A separate curated feed of Telegram channels managed by the admin, independent of the main feed
- **Access**: Admin users access the alternate feed via a popup menu on the Home button; non-admin users see only the main feed
- **Home Button Popup**: Clicking Home shows a fixed popup with "Main Feed" and "Alternate Feed" options; clicking outside or selecting an option closes it; event listeners are registered once at the top-level script scope to avoid duplication
- **Alternate View**: Dedicated `showView('alternate')` branch fetches posts from `GET /api/alternate-posts` and renders them in the standard feed layout
- **Management**: A dedicated "Alternate Feed Channels" section in the admin settings allows adding/removing channels with the same autocomplete experience as the main feed
- **Post Limit**: Controlled by the `alternate_feed_max_posts` setting in `config.json` (separate from the main feed's `max_posts`)
- **Feed Isolation**: Main feed (`GET /api/posts`) filters out `is_alternate=true` feeds; alternate feed (`GET /api/alternate-posts`) only returns `is_alternate=true` feeds
- **Standalone Digest Script**: `generate_alternate_digest.py` fetches alternate feed posts, processes them through Mistral AI for deduplication/ranking/rephrasing, maintains persistent history in `digest_history.json` (with incremental updates and raw post tracking to avoid reprocessing), and generates a local HTML digest file showing the full archive (see Standalone Scripts section)

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
  - **Main View**: Fetches from `/api/posts` (user's main-feed channels from Supabase -- both public and private, excluding alternate feeds); restores all control buttons and filter
  - **Alternate View**: Fetches from `/api/alternate-posts` (admin only); displays posts from the alternate feed channels
  - **Saved View**: Fetches from `/api/saved` (bookmarked posts); restores all control buttons and filter
  - **Management View**: Loads feed management interface (and admin settings + private channels + alternate feed management if admin); hides sync, sort, stop/resume buttons and filter wrapper
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
1. **Feed Loading**: Fetch user's feeds via `GET /api/feeds` (returns objects with `feed_url`, `is_private`, `admin_only`, `is_alternate`)
2. **Main Feed Display**: Render main feed list (excluding `is_alternate=true`) with badges (Private, Admin) and remove buttons
3. **Add Feed**: Input field + Add button; calls `POST /api/feeds` with duplicate detection (409 → "Feed already exists" message)
4. **Remove Feed**: Remove button calls `DELETE /api/feeds` with appropriate `is_alternate` flag and refreshes the list
5. **Private Channels** (admin only): "Load My Telegram Channels" button calls `GET /api/admin/channels`, displays available channels with "Add" buttons; adding sends `POST /api/feeds` with `is_private: true, admin_only: true`
6. **Alternate Feed Management** (admin only): Separate section with its own channel list, autocomplete input, and add/remove buttons; all operations use `is_alternate: true`
7. **Admin Settings** (admin only): Load settings via `GET /api/admin/config`, save via `POST /api/admin/config`

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
| Column         | Type    | Description                                               |
|----------------|---------|-----------------------------------------------------------|
| `user_id`      | bigint  | Foreign key to `Users.id` (NOT NULL)                      |
| `feed_url`     | text    | Telegram channel URL or numeric channel ID (for private)  |
| `is_private`   | boolean | Whether the channel requires Telethon API (default false) |
| `admin_only`   | boolean | Whether only admin can have this feed (default false)     |
| `is_alternate` | boolean | Whether the feed belongs to the alternate feed (default false) |

- **Foreign Key**: `user_id` references `Users.id` with `ON DELETE CASCADE`
- **Index**: `idx_feeds_user_id` on `user_id` for join/RLS performance
- **RLS**: Row Level Security requires appropriate policies for the API key to read/write rows
- **Duplicate Prevention**: Application-level check before insert (query for existing `user_id` + `feed_url` + `is_alternate` tuple); the same channel URL can exist once as a main feed and once as an alternate feed
- **Migration SQL**: `ALTER TABLE feeds ADD COLUMN is_private boolean DEFAULT false; ALTER TABLE feeds ADD COLUMN admin_only boolean DEFAULT false; ALTER TABLE feeds ADD COLUMN is_alternate boolean DEFAULT false;`

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
- **70% Load Time Reduction**: Optimized frontend architecture and caching strategies significantly improved initial page load performance
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
  - Frontend caches Top 10 results (`top10Cache`); re-uses cache unless the main feed was refreshed (`feedRefreshedSinceTop10` flag), avoiding redundant API calls
- **Frontend optimization**: 
  - `requestAnimationFrame` for smooth scrolling
  - Lazy image loading with `loading="lazy"`
  - Event delegation for click handlers
  - Dynamic image sizing with CSS variables for optimal rendering performance

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
- **AI API credentials**: `GROK_API_KEY` (Groq), `GOOGLE_API_KEY` (Google Gemini), `MISTRAL_API_KEY` (Mistral AI), and `TAVILY_API_KEY` (Tavily search) in `.env`; optional -- Top 10 AI ranking and context search features return 503 if the configured provider's keys are missing
- **Telegram session management**: Separate sessions recommended for dev and prod to avoid revocation; `generate_session.py` script supports creating labeled sessions ("TGUpdates-Dev" / "TGUpdates-Prod")
- **Startup logging**: No user or feed data is logged at startup
- **Service worker**: Does not intercept fetch requests (avoids stripping `Authorization` headers on mobile browsers); versioned with cache-clearing on activation
- **Cache control**: Root HTML response served with `no-cache, no-store, must-revalidate` to prevent stale frontend versions

### User Action Logging
Comprehensive server-side logging of user actions for analytics and monitoring:

#### Authentication Events
- **Login**: `User 'username' (id=123) logged in successfully`

#### Channel Management
- **Add channel**: `User 'username' added channel 'channel_url' (private=True, admin_only=False)`
- **Remove channel**: `User 'username' removed channel 'channel_url'`

#### Feature Usage
- **Top 10 Analysis**: `User 'username' requested Top 10 analysis (50 posts)`
- **View Saved Posts**: `User 'username' viewed saved posts (12 posts)`
- **Save Post**: `User 'username' saved post from channel 'channelname' (post_id=12345)`
- **Unsave Post**: `User 'username' unsaved post from channel 'channelname' (post_id=12345)`
- **Context Summary**: `User 'username' requested context summary (text_length=245)`
- **Share Post**: `User 'username' shared post via 'whatsapp' (url=https://...)`
  - Tracks share method: `native` (browser share), `whatsapp`, `telegram`, `email`, `copy`
  - Frontend calls `POST /api/track/share` endpoint with share method and post URL
  - Tracking is fire-and-forget (silent failures) to avoid disrupting user experience

#### Log Configuration
- **Output**: stderr (default Python logging)
- **Level**: INFO
- **Format**: `levelname:name:message` (Python basicConfig default)
- **Deployment**: Logs can be redirected/collected by hosting environment (Docker, Render, etc.)

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
  "max_posts": 100,
  "scroll_speed": 50,
  "media_concurrency": 20,
  "ai_provider": "gemini",
  "ai_model": "gemini-2.5-flash",
  "context_provider": "gemini",
  "context_mistral_model": "mistral-large-latest",
  "context_tavily_depth": "basic",
  "context_tavily_max_results": 5,
  "alternate_feed_max_posts": 100
}
```

## Standalone Scripts

### Alternate Feed Digest (`generate_alternate_digest.py`)

A CLI tool that fetches posts from the alternate feed, processes them through Mistral AI, generates a standalone HTML digest file, and optionally posts new stories to a Telegram channel. Maintains persistent history in `digest_history.json` for incremental updates across runs.

**Usage**:
```bash
python generate_alternate_digest.py
```

**Environment variables** (from `.env` or shell):
- `DIGEST_SERVER_URL` – base URL of the TelegramUpdates server (e.g. `https://app.onrender.com`); auto-prepends `http://` if no protocol is specified
- `DIGEST_USERNAME` – admin username for login
- `DIGEST_PASSWORD` – admin password for login
- `MISTRAL_API_KEY` – Mistral API key for AI processing
- `DIGEST_TELEGRAM_CHANNEL` – target Telegram channel (`@username` or numeric ID); if not set, Telegram posting is skipped
- `TELEGRAM_API_ID` – Telegram API ID (reused from server config; required for posting)
- `TELEGRAM_API_HASH` – Telegram API hash (reused from server config; required for posting)
- `TELEGRAM_SESSION` – Telethon StringSession (reused from server config; required for posting)

**Pipeline**:
1. **Authentication**: Logs in via `POST /api/login` using admin credentials; verifies admin status
2. **Fetch Posts**: Calls `GET /api/alternate-posts` with JWT authentication
3. **Load History**: Reads `digest_history.json` containing accumulated stories and a set of previously processed raw post keys (`channel_postid`)
4. **Filter New Posts**: Compares fetched posts against `processed_post_keys` from history; only posts with a `channel_postid` key not in the set are sent to Mistral. If no new posts exist, the script regenerates HTML from existing history and exits
5. **Prepare for LLM**: Strips heavy fields (HTML, base64 images), truncates text, computes engagement ratio (views/subscribers)
6. **Mistral AI Processing**: Sends `{"new_posts": [...], "previously_generated": [...]}` with the system prompt from `alternate_feed_prompt.md`. The `previously_generated` array contains the last 100 stories from history (sorted by recency) for deduplication context. The LLM returns up to 10 new stories/updates, each with `source_indices` (referencing `new_posts`) and `history_index` (null for new, index for updates)
7. **Media URL Resolution**: Converts relative `/api/` media URLs to absolute URLs using the server base URL; includes `photo_url`, `video_thumb`, and `link_preview.image` fallbacks. Uses URL-path fingerprinting (Layer 1 dedup) to eliminate CDN variants of the same image before downloading
8. **Save History**: Appends new stories to `full_history`. Updates are appended with a `parent_index` linking to the original story. All raw post keys from the current batch are added to `processed_post_keys` (regardless of whether they produced output). Persists `posted_media_hashes` for cross-run media dedup. Writes to `digest_history.json`
9. **Video URL Resolution** (optional, only when Telegram posting is enabled): Scans source posts referenced by each story's `source_indices` for `has_video=true`. For public channels, scrapes the Telegram embed page (`https://t.me/{channel}/{post_id}?embed=1`) to extract the direct CDN video URL from the `<video>` tag. Private channel videos are skipped (their thumbnails are still posted as images). Collected URLs are stored in a `video_urls` list on each story
10. **Telegram Channel Posting** (optional): If `DIGEST_TELEGRAM_CHANNEL` is set and Telegram credentials are configured, posts each new story and update to the target channel via Telethon. Updates are prefixed with "**עדכון**". Media images are downloaded into memory, deduplicated by content hash (Layer 2: within-run, Layer 3: cross-run via persisted `posted_media_hashes`), and sent as photo albums. Videos are downloaded from CDN URLs (120s timeout) and uploaded individually with `supports_streaming=True`. Images are sent first (as album with caption), then videos (2s delay between each). Text-only stories are sent as plain messages. Posts are spaced 1.5s apart to avoid rate limits. Failures are logged but non-fatal
11. **HTML Generation**: Produces a self-contained `alternate_digest.html` rendering the **full history archive** (all accumulated stories), sorted by most recent first. Stories from the latest run display importance rank badges (#1, #2, etc.); updates display an "UPDATE" badge with distinct styling; older stories have no rank badge

**History file** (`digest_history.json`):
```json
{
  "stories": [ /* accumulated story objects with text, importance, media_urls, source_indices, history_index, parent_index, created_at, updated_at */ ],
  "processed_post_keys": [ /* sorted list of "channel_postid" strings for all raw posts ever sent to Mistral */ ],
  "posted_media_hashes": [ /* sorted list of MD5 hex strings for all media images posted to Telegram */ ],
  "last_updated": "2026-04-18T23:10:49"
}
```

**Logging**: All output uses a `log()` helper that prepends timestamps (e.g. `[2026-04-18 23:10:02]`). New stories are logged as `[NEW]`, updates as `[UPDATE for #N]`.

**Key functions**:
- `log(msg, error=False)`: Timestamped logging to stdout/stderr
- `login(base_url, username, password) -> str`: Authenticates and returns JWT
- `fetch_alternate_posts(base_url, token) -> list[dict]`: Fetches posts from the alternate-posts endpoint
- `load_history() -> tuple[list[dict], set[str], set[str]]`: Loads stories, processed post keys, and posted media hashes from `digest_history.json`
- `_post_key(post) -> str`: Builds unique key (`channel_postid`) for a raw Telegram post
- `prepare_slim_posts(posts) -> list[dict]`: Strips heavy fields, computes engagement ratio
- `call_mistral(api_key, model, prompt, user_content) -> list[dict]`: Sends to Mistral AI, parses structured JSON response (300s timeout)
- `_media_url_fingerprint(url) -> str`: Extracts a stable fingerprint from a URL by stripping query params and isolating the path tail (Layer 1 dedup)
- `resolve_media_urls(stories, original_posts, base_url) -> list[dict]`: Converts relative media URLs to absolute with URL-path fingerprint dedup
- `scrape_video_cdn_url(channel, post_id) -> str | None`: Scrapes Telegram's embed page to extract the direct CDN video URL from the `<video>` tag; public channels only
- `resolve_video_urls(stories, original_posts) -> list[dict]`: Scans source posts for `has_video=true`, scrapes CDN URLs, populates `video_urls` list on each story; skips private channels
- `connect_telegram() -> TelegramClient | None`: Creates and connects a Telethon client; returns `None` if credentials are missing or session is unauthorized
- `post_stories_to_telegram(stories, channel, posted_media_hashes) -> set[str]`: Posts stories to a Telegram channel with content-hash media dedup (Layers 2+3); downloads images and videos into memory, sends photo albums then individual videos with streaming support, returns updated hash set
- `save_history(new_stories, full_history, offset, filtered_posts, processed_keys, posted_media_hashes)`: Appends stories, tracks post keys and media hashes, writes to disk
- `generate_html(stories, output_path)`: Renders the full history archive as a dark-themed HTML file with clickable media, rank badges, and update badges

### Alternate Feed Prompt (`alternate_feed_prompt.md`)

System prompt used by the standalone digest script for Mistral AI processing. Contains six sections:

1. **Core Task**: Operational steps — deduplicate, select (up to 10), merge same-event posts, rephrase, strip source attribution, enforce temporal accuracy, topical coherence, source constraint, and media preservation
2. **Persona**: "The Proud Patriot" (הפטריוט הגאה) — constructive Zionist editorial voice with nationalism, directness, love for Israel, and a loyalty rule for internal challenges
3. **Ranking & Selection Criteria**: Impact, relevancy, engagement ratio, cross-references, depth & media; exclusions for missile alerts, promotional content, and donation appeals
4. **Input & Output Format**: Receives `{"new_posts": [...], "previously_generated": [...]}`. The `previously_generated` array is READ-ONLY context for deduplication — never a source for generating new content. Returns `{"stories": [...]}` with `history_index` (null for new stories, index for updates) and `source_indices` referencing `new_posts`
5. **Incremental History Updates**: Mandatory duplicate check against entire `previously_generated` array using event-based matching (WHO, WHAT, WHERE, WHEN — not wording). Includes: reactions/commentary = same event rule, check updates too, within-batch dedup, concrete Hebrew examples of same-event matches. Updates must contain genuinely new facts from `new_posts`; pure duplicates are skipped. Variable output count (0 to 10, never more)
6. **Final Verification** (5 steps, mandatory before returning): self-dedup, history cross-check, update content check, final count cap, source audit (every output item must have valid `source_indices` from `new_posts`)

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
- `get_posts(request, user)`: `GET /api/posts` -- fetches user's feeds from Supabase, filters out alternate feeds, splits into public/private, scrapes public channels and fetches private channels via Telethon, returns merged sorted posts
- `get_alternate_posts(request, user)`: `GET /api/alternate-posts` -- admin-only; fetches all alternate-feed channels, returns posts limited to `alternate_feed_max_posts`
- `get_feeds(request, user)`: `GET /api/feeds` -- returns user's feeds as objects with `feed_url`, `is_private`, `admin_only`, `is_alternate`
- `add_feed(request, user)`: `POST /api/feeds` -- adds a feed with duplicate check on `(user_id, feed_url, is_alternate)`, admin-only restriction enforcement, and `is_private`/`admin_only`/`is_alternate` support (409 on conflict, 403 on admin-only or alternate violation); logs channel addition with username and channel details
- `delete_feed(request, user)`: `DELETE /api/feeds` -- removes a feed for the user scoped by `is_alternate`; logs channel removal with username and channel name
- `search_channels(request, q, user)`: `GET /api/search-channels` -- searches Telegram for public channels via Telethon `contacts.SearchRequest`
- `get_admin_channels(request, user)`: `GET /api/admin/channels` -- lists all channels the Telethon session is a member of (admin-only)
- `get_full_config(user)`: `GET /api/admin/config` -- returns config.json (admin-only, 403 for non-admin)
- `update_config(request, user)`: `POST /api/admin/config` -- updates config.json (admin-only, 403 for non-admin)
- `get_top_posts(request, user)`: `POST /api/top-posts` -- receives posts array, computes per-post engagement ratio (views/subscribers), sends stripped-down versions to configured AI provider (Google Gemini or Groq, per `ai_provider`/`ai_model` in `config.json`) with system prompt from `top10_prompt.md`, parses response indices, returns top 10 full post objects
- `get_context_summary(request, user)`: `POST /api/context-summary` -- parses request body, logs user action, routes to appropriate context provider based on `context_provider` in `config.json`
- `_context_summary_gemini(body)`: Generates context using Google Gemini with Google Search grounding; reads `context_summary_prompt.md`, returns summary and sources
- `_context_summary_mistral(body)`: Generates context using Mistral AI + Tavily; instantiates `WebSearch` class with config parameters, returns summary and sources
- `track_share(request, user)`: `POST /api/track/share` -- logs share actions with username, share method, and post URL
- `normalize_channel(raw: str) -> str`: Extracts channel name from various URL formats
- `fetch_channel_html(client, channel) -> str | None`: Async HTTP request with error handling
- `extract_image_url(style: str) -> str | None`: Regex extraction from CSS `url()` values
- `parse_channel_posts(html: str, channel: str) -> list[dict]`: BeautifulSoup parsing logic
- `_merge_grouped_posts(posts) -> list[dict]`: Post-processing for HTML-scraped posts; detects consecutive post IDs with missing text (media album groups) and merges them into single posts
- `_merge_telethon_grouped(posts) -> list[dict]`: Post-processing for Telethon-fetched posts; uses `grouped_id` to merge album messages into single posts
- `fetch_private_channel_posts(tg, channel_id, limit) -> list[dict]`: Telethon-based message fetching with base64 media encoding
- `load_config() -> dict`: JSON config file loader
- `get_saved_posts(request, user)`: `GET /api/saved` -- returns the logged-in user's saved posts from Supabase; logs view action with username and post count
- `save_post(request, user)`: `POST /api/saved` -- saves a post for the logged-in user in Supabase (duplicate check); logs save action with username, channel, and post_id
- `unsave_post(channel, post_id, request, user)`: `DELETE /api/saved/{channel}/{post_id}` -- removes a saved post for the logged-in user; logs unsave action with username, channel, and post_id

#### WebSearch (`web_search.py`)
- `WebSearch.__init__(mistral_api_key, tavily_api_key, mistral_model, search_depth, max_results)`: Initializes the web search client with API keys and configuration
- `WebSearch.search(post_text) -> dict`: Main orchestration method that generates search terms, searches the web, and summarizes results
- `WebSearch._generate_search_terms(post_text) -> str`: Uses Mistral AI to extract 2-5 search terms from the post (reads `search_terms.md` prompt)
- `WebSearch._search_web(search_terms) -> list[dict]`: Searches Tavily API with the generated terms, returns list of results with title, url, content
- `WebSearch._summarize_results(post_text, web_results) -> str`: Uses Mistral AI to synthesize web results into a contextual summary (reads `summarize.md` prompt)
- `WebSearch._call_mistral(system_prompt, user_content) -> str`: Generic Mistral API caller with error handling

#### Database (`database.py`)
- `Database.create() -> Database`: Async factory that initializes the Supabase async client
- `Database.get_all_users() -> list[dict]`: Fetches all rows from the `Users` table
- `Database.get_all_feeds() -> list[dict]`: Fetches all rows from the `Feeds` table
- `Database.authenticate_user(user_name, password) -> dict | None`: Queries `Users` for matching credentials
- `Database.get_feeds_for_user(user_id) -> list[dict]`: Fetches feeds for a specific user
- `Database.add_feed(user_id, feed_url, is_private=False, admin_only=False, is_alternate=False) -> dict | None`: Inserts a feed with duplicate check on `(user_id, feed_url, is_alternate)` tuple and private/admin-only/alternate flags; returns `None` if duplicate
- `Database.remove_feed(user_id, feed_url, is_alternate=False) -> bool`: Deletes the matching feed row scoped by `is_alternate`; returns whether deletion occurred
- `Database.get_alternate_feeds() -> list[dict]`: Returns all feed rows where `is_alternate=true` (global, not user-scoped)
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
- `showView(view)`: Multi-view navigation and state management (`main`, `alternate`, `saved`, `management`); resets inline display styles to preserve CSS defaults; hides feed-specific controls in management view
- `startScrolling()`: Auto-scroll animation engine
- `renderFilters()`: Dynamic, scrollable filter button generation
- `renderPosts()`: Responsive post HTML generation with mobile support
- `handleShareClick(evt, postUrl, btn)`: Click handler for share button with propagation control
- `sharePost(postUrl, btn)`: Tries `navigator.share` first, falls back to custom share popup; tracks share via `trackShare()`
- `trackShare(sharedTo, postUrl)`: Calls `POST /api/track/share` to log share actions (fire-and-forget)
- `showSharePopup(postUrl, btn)`: Creates and positions the custom share popup with WhatsApp/Telegram/Email/Copy options; each option tracks via `trackShare()`
- `hideSharePopup()`: Removes any open share popup and overlay
- `copyTextFallback(text)`: Clipboard fallback using `execCommand('copy')` for older browsers
- `flashShareButton(btn, label)`: Briefly changes the share button label for feedback
- `loadManagementInterface()`: Loads feed list, private channels section (admin), alternate feed management (admin), and settings (admin)
- `loadFeedList()`: Fetches and renders user's main feeds from `/api/feeds` (excluding `is_alternate=true`) with Private/Admin badges
- `addFeed()`: Adds a feed via `POST /api/feeds` with duplicate error display
- `loadPrivateChannels()`: Fetches and displays available private channels from `GET /api/admin/channels` (admin only)
- `addPrivateChannel(channelId, btn)`: Adds a private channel as admin-only feed via `POST /api/feeds`
- `setupAutocomplete()`: Initializes channel search autocomplete on the feed input (debounce, keyboard navigation, blur handling)
- `searchChannels(query)`: Calls `GET /api/search-channels?q=` and populates the autocomplete dropdown
- `showAutocomplete(items)` / `hideAutocomplete()`: Renders or hides the autocomplete dropdown
- `selectAutocompleteItem(index)`: Populates the feed input with the selected channel's URL
- `fetchTop10Posts(forceRefresh)`: Sends current feed posts to `POST /api/top-posts` for AI ranking; serves from `top10Cache` if available and feed hasn't been refreshed; displays loading state, floating bubble notification, renders top 10 results, handles errors
- `showFloatingBubble(message)`: Creates a temporary floating bubble notification that fades in, displays for 3 seconds, and auto-removes from the DOM
- `updateAiModelOptions(provider)`: Populates the AI Model dropdown with models appropriate for the selected provider (Gemini or Groq)
- `fetchAlternatePosts()`: Fetches and renders posts from `GET /api/alternate-posts` (admin only)
- `loadAlternateFeedList()`: Fetches and displays only `is_alternate=true` feeds in the alternate feed management section
- `setupAltAutocomplete()`: Initializes channel search autocomplete for the alternate feed input
- `openAltAddChannelPopup()` / `closeAltAddChannelPopup()`: Opens/closes the autocomplete popup for adding alternate feed channels
- `searchAltChannels(query)`: Searches for channels to add to the alternate feed
- `addAltChannelFromPopup(index)` / `addAltFeedByUrl(url)`: Adds a channel to the alternate feed via `POST /api/feeds` with `is_alternate: true`
- `removeFeed(feedUrl, isAlternate=false)`: Removes a feed via `DELETE /api/feeds` with `is_alternate` flag
- `saveAdminSettings()`: Saves global settings via `POST /api/admin/config` (admin only)
- `manualSync()`: Manual post refresh with loading states
- `toggleSortOrder()`: Client-side post sorting toggle
- `toggleStopScrolling()`: Stop/resume auto-scrolling control
- `setTheme(theme)`: Applies light/dark theme via `data-theme` attribute, updates meta tags, saves to `localStorage`, and highlights the active button in the management panel
- `updateThemeButtons(theme)`: Syncs the active state of theme toggle buttons in the management panel
- `formatDate(isoStr)`: Relative time formatting
- `sanitizeHtml(html)`: XSS prevention for post content
- `toggleVideoFullscreen(btn)`: Async fullscreen toggle with orientation lock support and playback continuity
- `bindVideoAspect(container)`: Detects video orientation and applies `.is-portrait-video` class for adaptive fullscreen layout
- `updateFullscreenOrientationMode(container)`: Applies landscape orientation lock for landscape videos, unlocks for portrait videos
- `resumeVideoIfNeeded(container)`: Multi-retry playback resume logic for reliable fullscreen transitions
- `handleFullscreenChange()`: Unified fullscreen change handler for both standard and webkit events
- `getFullscreenElement()`: Cross-browser fullscreen element detection
- `requestElementFullscreen(element)`: Cross-browser fullscreen request with fallbacks
- `exitAnyFullscreen()`: Cross-browser fullscreen exit with fallbacks
- `openImageFullscreen(src, alt)`: Opens image in fullscreen lightbox with history state integration
- `closeImageFullscreen(evt, options)`: Closes image lightbox with history synchronization
- `applyDynamicImageSizing(imgEl)`: Computes and applies CSS variables for dynamic image container sizing based on natural dimensions

### File Structure
```
/
├── server.py              # FastAPI backend with PWA support, Supabase lifespan, and Telethon integration
├── database.py            # Database class encapsulating Supabase async client
├── web_search.py          # WebSearch class for Mistral AI + Tavily web search integration
├── config.json            # Global settings (refresh interval, max posts, scroll speed, AI provider/model, context provider, alternate feed max posts)
├── top10_prompt.md        # Editable system prompt for AI Top 10 ranking (with exclusion rules)
├── alternate_feed_prompt.md   # System prompt for Mistral AI alternate feed processing (dedup, rank, rephrase)
├── context_summary_prompt.md  # System prompt for Gemini context search with Google Search grounding
├── search_terms.md        # Prompt for Mistral AI to extract search terms from posts
├── summarize.md           # Prompt for Mistral AI to summarize web search results
├── generate_session.py    # Telethon StringSession generator (dev/prod labeled sessions)
├── generate_alternate_digest.py  # Standalone script: fetch alternate feed, process with Mistral AI, generate HTML digest, optionally post to Telegram channel
├── digest_history.json    # Persistent storage for generated stories, processed post keys, and posted media hashes (created/updated by generate_alternate_digest.py)
├── .env                   # Environment variables (SUPABASE_URL, SUPABASE_KEY, TELEGRAM_*, GROK_API_KEY, GOOGLE_API_KEY, MISTRAL_API_KEY, TAVILY_API_KEY, DIGEST_SERVER_URL, DIGEST_USERNAME, DIGEST_PASSWORD, DIGEST_TELEGRAM_CHANNEL)
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

### Alternate Feed Management (Admin Only)
- **Dedicated Section**: Separate "Alternate Feed Channels" section in the management interface (visible only for admin)
- **Add Channel**: Autocomplete-powered input for searching and adding channels to the alternate feed; calls `POST /api/feeds` with `is_alternate: true`
- **Remove Channel**: Remove button next to each alternate feed channel; calls `DELETE /api/feeds` with `is_alternate: true`
- **Independent Lists**: Main feed and alternate feed channel lists are displayed separately; the same channel can appear in both

### Settings Management (Admin Only, `user_id = 1`)
- **Refresh Interval**: Configurable auto-refresh timing (1-60 minutes)
- **Max Posts**: Main feed post limit configuration (5-100 posts)
- **Alternate Feed Max Posts**: Alternate feed post limit configuration (5-500 posts)
- **Scroll Speed**: Auto-scroll speed adjustment (10-200 pixels/second)
- **AI Provider**: Select between Google Gemini and Groq for AI ranking
- **AI Model**: Model selector populated dynamically based on selected provider (Gemini: gemini-2.0-flash-lite, gemini-2.0-flash, gemini-2.5-flash-lite, gemini-2.5-flash, gemini-2.5-pro; Groq: llama-3.3-70b-versatile, llama-3.1-8b-instant, etc.)
- **Visibility**: Settings section is only shown when `localStorage.getItem('is_admin') === 'true'`
- **Persistence**: Saved via `POST /api/admin/config` which overwrites `config.json`
