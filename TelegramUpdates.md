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
- **Enhanced configuration with dual feeds**:
  ```json
  {
    "channels": ["https://t.me/abualiexpress", "https://t.me/ziv710", "https://t.me/alexmehacarmel"],
    "paz_channels": [],
    "refresh_interval_minutes": 5,
    "max_posts": 20,
    "scroll_speed": 50
  }
  ```
- **Dual Feed Support**:
  - **Main Feed**: `channels` array for primary content
  - **Paz Feed**: `paz_channels` array for secondary content stream
  - **Independent Configuration**: Each feed can have different channels
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

#### `GET /api/paz/posts`
Returns the latest posts from Paz feed channels, sorted by datetime (newest first), limited to `max_posts`.

**Implementation**: Same as `/api/posts` but uses `paz_channels` configuration

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

#### `GET /api/admin/config`
Returns full configuration for management interface.

**Implementation**: Returns complete `config.json` content for admin interface

#### `POST /api/admin/config`
Updates configuration via management interface.

**Implementation**: Accepts JSON payload and overwrites `config.json`

#### `GET /api/admin/config/download`
Downloads current `config.json` file.

**Implementation**: Returns `config.json` as downloadable file attachment

#### `GET /health`
Health check endpoint returning `{"status": "ok"}`.

#### `GET /`
Serves the main application (`static/index.html`).

#### PWA Support
- **`GET /static/manifest.json`**: Web app manifest with correct MIME type
- **`GET /static/sw.js`**: Service worker for PWA compliance

#### Static Files
- **Mount point**: `/static` serves files from `static/` directory
- **Main app**: `static/index.html` contains the entire frontend application
- **PWA assets**: Icons, manifest, and service worker files

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

#### Enhanced Navigation System
- **Multi-View Interface**: Three distinct views accessible via header navigation
  - **Main Feed**: Primary channel content (default view)
  - **Paz Feed**: Secondary channel content (accessed via "P" link)
  - **Management Interface**: Configuration and admin controls (⚙️ icon)
- **Navigation Links**: Horizontal navigation with active state indicators
- **Context-Aware UI**: Different controls shown based on current view

#### Layout Structure
- **Header**: Fixed 44px height with enhanced navigation and controls
  - **Horizontally Scrollable**: Entire header scrolls when content overflows
  - **Navigation Links**: Main, Paz ("P"), and Management (⚙️)
  - **Control Buttons**: Manual sync and sort order toggle
  - **Scrollable Filter Bar**: Channel filter buttons with horizontal scroll
  - **Status Indicator**: Loading states and last update time
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
  - Post text: 2rem → 1.2rem
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
- **Control buttons**: Manual sync and sort order toggle
- **Navigation links**: Switch between Main, Paz, and Management views
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

### 5. Enhanced Data Flow

#### Initialization and Configuration
1. **Startup**: `init()` function initializes multi-view interface
2. **Event Listeners**: Set up navigation, control buttons, and PWA service worker
3. **Configuration**: Fetch `/api/config` to get `refresh_interval_minutes` and `scroll_speed`
4. **Default View**: Load Main feed as default view

#### Multi-View Data Management
- **View Switching**: `showView(view)` function manages interface state
  - **Main View**: Fetches from `/api/posts` (main channels)
  - **Paz View**: Fetches from `/api/paz/posts` (paz channels)  
  - **Management View**: Loads configuration interface
- **Context-Aware Refresh**: Auto-refresh adapts to current view
- **UI State Management**: Controls visibility of filters, buttons based on active view

#### Enhanced Rendering Pipeline
1. **Data Fetching**: View-specific API endpoints with loading states
2. **Sorting**: Client-side sorting based on user preference (newest/oldest first)
3. **Filtering**: Channel-specific filtering with scrollable filter bar
4. **Rendering**: 
   - `renderFilters()`: Dynamic, scrollable filter buttons
   - `renderPosts()`: Responsive post cards with mobile support
   - `startScrolling()`: Auto-scroll with pause/resume functionality

#### Management Interface Flow
1. **Configuration Loading**: Fetch full config via `/api/admin/config`
2. **Dynamic Forms**: Generate channel management forms for both feeds
3. **Real-time Updates**: Immediate UI updates for add/remove operations
4. **Persistence**: Save changes via `/api/admin/config` POST
5. **Download Support**: Export configuration via `/api/admin/config/download`

#### PWA Integration
1. **Service Worker**: Register minimal service worker for PWA compliance
2. **Manifest**: Serve web app manifest with proper MIME types
3. **Installation**: Support for "Add to Home Screen" functionality
4. **Standalone Mode**: Full-screen experience without browser UI

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

### Current production configuration with dual feeds:
```json
{
  "channels": [
    "https://t.me/abualiexpress",
    "https://t.me/ziv710", 
    "https://t.me/alexmehacarmel"
  ],
  "paz_channels": [],
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
5. **No real-time updates**: Relies on periodic polling rather than WebSocket/SSE
6. **Memory usage**: All posts kept in memory (no pagination or cleanup)
7. **Single-threaded**: FastAPI runs in single process (no horizontal scaling)
8. **Keep-alive dependency**: Render.com free tier requires external pinging to prevent sleep
9. **PWA offline limitations**: No offline functionality - requires internet connection
10. **Configuration persistence**: Management interface changes persist immediately but require page refresh for some settings

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
- `fetchPosts()`: Main feed data fetching with loading states
- `fetchPazPosts()`: Paz feed data fetching
- `showView(view)`: Multi-view navigation and state management
- `startScrolling()`: Auto-scroll animation engine
- `renderFilters()`: Dynamic, scrollable filter button generation
- `renderPosts()`: Responsive post HTML generation with mobile support
- `loadManagementInterface()`: Configuration interface loader
- `renderChannelList()`: Dynamic channel management forms
- `saveConfiguration()`: Configuration persistence via API
- `downloadConfig()`: Configuration file download
- `manualSync()`: Manual post refresh with loading states
- `toggleSortOrder()`: Client-side post sorting toggle
- `formatDate(isoStr)`: Relative time formatting
- `sanitizeHtml(html)`: XSS prevention for post content

### File Structure
```
/
├── server.py              # FastAPI backend with PWA support
├── config.json           # Enhanced channel configuration (dual feeds)
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
- **Service Worker**: Minimal worker for PWA compliance (no offline caching)
- **Meta Tags**: Comprehensive mobile and desktop PWA support
- **Icons**: Full icon set (72px to 512px) for all platforms
- **Theme Integration**: Consistent dark theme across installed app

### Technical Implementation
- **Manifest Serving**: Proper MIME type (`application/manifest+json`)
- **Service Worker**: Minimal implementation for PWA recognition
- **Icon Generation**: Automated icon creation from SVG template
- **Mobile Optimization**: Viewport settings for full-screen experience

## Management Interface

### Channel Management
- **Dual Feed Configuration**: Separate management for Main and Paz channels
- **Dynamic Forms**: Add/remove channels with real-time UI updates
- **Channel Validation**: URL format validation and normalization
- **Bulk Operations**: Multiple channel management capabilities

### Settings Management
- **Refresh Interval**: Configurable auto-refresh timing (1-60 minutes)
- **Max Posts**: Post limit configuration (5-100 posts)
- **Scroll Speed**: Auto-scroll speed adjustment (10-200 pixels/second)
- **Real-time Updates**: Immediate application of setting changes

### Configuration Export/Import
- **Download**: Export current configuration as JSON file
- **Backup**: Complete configuration backup capability
- **Restore**: Manual configuration restoration via file upload
- **Version Control**: Configuration change tracking