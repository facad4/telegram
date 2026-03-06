# Telegram Channel Updates Dashboard

A real-time dashboard that displays the latest posts from configured Telegram channels in an auto-scrolling interface.

## Architecture

- **Backend**: FastAPI server (`server.py`) that scrapes public Telegram channel pages (`t.me/s/<channel>`)
- **Frontend**: Single-page vanilla HTML/CSS/JavaScript application (`static/index.html`) with auto-scrolling tiles
- **Configuration**: JSON-based channel configuration (`config.json`)
- **Dependencies**: FastAPI, httpx, BeautifulSoup4 (see `requirements.txt`)

## Core Features

### 1. Channel Configuration
- **File**: `config.json`
- **Current configuration**:
  ```json
  {
    "channels": ["https://t.me/abualiexpress", "https://t.me/ziv710", "https://t.me/alexmehacarmel"],
    "refresh_interval_minutes": 5,
    "max_posts": 20,
    "scroll_speed": 50
  }
  ```
- **Channel formats supported**:
  - Plain name: `"channelname"`
  - With @: `"@channelname"`
  - Full URL: `"https://t.me/channelname"`
  - Public preview URL: `"https://t.me/s/channelname"`

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

### 3. API Endpoints

#### `GET /api/posts`
Returns the latest posts from all configured channels, sorted by datetime (newest first), limited to `max_posts`.

**Implementation**: 
- Loads config from `config.json`
- Normalizes channel names
- Fetches all channels concurrently with 15s timeout
- Parses HTML for each channel
- Merges and sorts posts by datetime (descending)
- Returns top `max_posts` entries

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

#### `GET /api/config`
Returns client-specific configuration settings from `config.json`.

**Implementation**: Loads and returns subset of config relevant to frontend

**Response format**:
```json
{
  "refresh_interval_minutes": 5,
  "scroll_speed": 50
}
```

#### `GET /health`
Health check endpoint returning `{"status": "ok"}`.

#### `GET /`
Serves the main application (`static/index.html`).

#### Static Files
- **Mount point**: `/static` serves files from `static/` directory
- **Main app**: `static/index.html` contains the entire frontend application

### 4. Frontend Interface (`static/index.html`)

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

#### Layout Structure
- **Header**: Fixed 44px height with title, filters, and status indicator
- **Viewport**: Flex-grow scrollable area with thin scrollbars
- **Cards**: Horizontal layout with media side (40% width, max 500px) and content area
- **Responsive**: 12vw horizontal margins, min-height 50vh per card
- **Media handling**: 
  - With media: Full-size image/video thumbnail on left
  - Without media: 80px channel icon area with large avatar (52px)

#### Auto-scrolling Implementation
- **Engine**: `requestAnimationFrame` with delta-time calculations
- **Speed**: Configurable pixels per second from API config
- **Direction**: Top to bottom (newest posts first)
- **Loop behavior**: 
  - Scroll to bottom → pause 3 seconds → reset to top
  - Initial 2-second pause before starting
- **Pause triggers**:
  - Mouse wheel: 4-second pause
  - Touch start/end: 4-second pause
  - Manual scroll detection via event listeners

#### Typography Scale
- **Post text**: 2rem, line-height 1.55
- **Channel names**: 1.3rem, font-weight 600
- **Metadata**: 1rem (dates, views)
- **Link previews**: 1.15rem titles, 1.05rem descriptions
- **Filters**: 0.85rem buttons

#### Interactive Elements
- **Post cards**: Click to open in Telegram (`window.open`)
- **Channel links**: Click to open channel page (event propagation stopped)
- **Link previews**: Click to open external links
- **Filter buttons**: Toggle between "All" and individual channels
- **Hover effects**: Border color changes, text color transitions

#### Channel Filtering System
- **UI**: Dynamic filter bar in header (hidden if < 2 channels)
- **Buttons**: "All" + individual channel buttons with active states
- **State management**: `activeFilter` variable persists selection
- **Implementation**: Filter posts array and re-render on change

#### Content Processing
- **HTML sanitization**: `sanitizeHtml()` function removes `onclick`, adds security attributes
- **Date formatting**: Relative time display (minutes/hours/days ago) with fallback to date
- **Image loading**: Lazy loading with `loading="lazy"` attribute
- **Error handling**: Loading states, error messages, empty state handling

### 5. Data Flow
1. **Initialization**: `init()` function calls `fetchConfig()` then `fetchPosts()`
2. **Configuration**: Fetch `/api/config` to get `refresh_interval_minutes` and `scroll_speed`
3. **Data loading**: Fetch `/api/posts` with loading indicator and status updates
4. **Rendering pipeline**:
   - `renderFilters()`: Create filter buttons if multiple channels exist
   - `renderPosts()`: Generate HTML for filtered posts and start auto-scroll
5. **Auto-refresh**: `setInterval` timer calls `fetchPosts()` every N minutes
6. **Backend processing**: On each `/api/posts` request:
   - Load `config.json`
   - Normalize channel names with `normalize_channel()`
   - Fetch all channels concurrently with `asyncio.gather()`
   - Parse HTML with `parse_channel_posts()` for each channel
   - Merge results, sort by datetime (descending), limit to `max_posts`
7. **Error handling**: Failed requests show error message, retry on next refresh cycle

### 6. Deployment Configuration

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

### 7. Dependencies (`requirements.txt`)
```
fastapi[standard]  # Web framework with built-in ASGI server
httpx              # Async HTTP client for Telegram scraping
beautifulsoup4     # HTML parsing for post extraction
```

**Key imports in `server.py`**:
- `asyncio` - Concurrent channel fetching
- `json` - Config file parsing
- `logging` - Request/error logging
- `re` - Image URL extraction from CSS
- `pathlib.Path` - File path handling
- `fastapi` - Web framework, responses, static files

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
- **Input sanitization**: 
  - `sanitizeHtml()` removes `onclick` attributes
  - External links get `target="_blank"` and `rel="noopener noreferrer"`
- **XSS prevention**: HTML content sanitized before DOM insertion
- **CORS**: Not required (same-origin requests)
- **Rate limiting**: None implemented (relies on Telegram's rate limiting)
- **User-Agent spoofing**: Uses Chrome UA to avoid bot detection

### Browser Compatibility
- **Target browsers**: Modern browsers with ES2017+ support
- **Required features**: 
  - CSS Custom Properties (variables)
  - CSS Grid and Flexbox
  - `async/await` and `fetch` API
  - `requestAnimationFrame`
- **No fallbacks**: Assumes modern browser environment

## Configuration Examples

### Minimal setup (2 channels):
```json
{
  "channels": ["telegram", "durov"],
  "refresh_interval_minutes": 5,
  "max_posts": 20,
  "scroll_speed": 50
}
```

### Current production configuration:
```json
{
  "channels": [
    "https://t.me/abualiexpress",
    "https://t.me/ziv710", 
    "https://t.me/alexmehacarmel"
  ],
  "refresh_interval_minutes": 5,
  "max_posts": 20,
  "scroll_speed": 50
}
```

## Known Limitations

1. **Public channels only**: Cannot access private channels or channels requiring authentication
2. **Rate limiting**: Subject to Telegram's rate limiting on public preview pages
3. **Content restrictions**: Some media may not be accessible in web preview format
4. **Network dependencies**: Requires outbound HTTP access to `t.me` (may be blocked in some hosting environments)
5. **No persistence**: Configuration changes require server restart or file modification
6. **No real-time updates**: Relies on periodic polling rather than WebSocket/SSE
7. **Memory usage**: All posts kept in memory (no pagination or cleanup)
8. **Single-threaded**: FastAPI runs in single process (no horizontal scaling)
9. **Keep-alive dependency**: Render.com free tier requires external pinging to prevent sleep

## Implementation Details

### Key Functions

#### Backend (`server.py`)
- `normalize_channel(raw: str) -> str`: Extracts channel name from various URL formats
- `fetch_channel_html(client, channel) -> str | None`: Async HTTP request with error handling
- `extract_image_url(style: str) -> str | None`: Regex extraction from CSS `url()` values
- `parse_channel_posts(html: str, channel: str) -> list[dict]`: BeautifulSoup parsing logic
- `load_config() -> dict`: JSON config file loader

#### Frontend (`static/index.html`)
- `fetchConfig()`: Loads client settings from `/api/config`
- `fetchPosts()`: Main data fetching with loading states
- `startScrolling()`: Auto-scroll animation engine
- `renderFilters()`: Dynamic filter button generation
- `renderPosts()`: Post HTML generation and DOM insertion
- `formatDate(isoStr)`: Relative time formatting
- `sanitizeHtml(html)`: XSS prevention for post content

### File Structure
```
/
├── server.py              # FastAPI backend
├── config.json           # Channel configuration
├── requirements.txt      # Python dependencies
├── static/
│   └── index.html        # Complete frontend application
├── .github/
│   └── workflows/
│       └── keep-alive.yml # GitHub Actions keep-alive workflow
└── TelegramUpdates.md    # This documentation
```