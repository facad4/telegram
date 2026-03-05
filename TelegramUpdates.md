# Telegram Channel Updates Dashboard

A real-time dashboard that displays the latest posts from configured Telegram channels in an auto-scrolling interface.

## Architecture

- **Backend**: FastAPI server that scrapes public Telegram channel pages (`t.me/s/<channel>`)
- **Frontend**: Single-page vanilla HTML/CSS/JavaScript application with auto-scrolling tiles
- **Deployment**: Docker-based deployment (Hugging Face Spaces, Render, etc.)

## Core Features

### 1. Channel Configuration
- **File**: `config.json`
- **Format**:
  ```json
  {
    "channels": ["channel1", "@channel2", "https://t.me/channel3"],
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

### 2. Data Scraping
- **Method**: HTTP requests to `https://t.me/s/<channel>` (public web preview)
- **Parser**: BeautifulSoup4 HTML parsing
- **Extracted data**:
  - Post text (HTML and plain text)
  - Publication date/time
  - View count
  - Channel name and avatar
  - Media (photos/video thumbnails)
  - Link previews
- **No caching**: Fresh data on every request
- **Concurrency**: Async fetching of all channels simultaneously

### 3. API Endpoints

#### `GET /api/posts`
Returns the latest posts from all configured channels.

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
Returns client configuration.

**Response format**:
```json
{
  "refresh_interval_minutes": 5,
  "scroll_speed": 50
}
```

### 4. Frontend Interface

#### Layout
- **Design**: Dark theme with modern card-based layout
- **Structure**: Auto-scrolling vertical feed of wide tiles
- **Tile format**: Horizontal layout with media on left (40% width), content on right
- **Responsive**: Adapts to screen width with configurable padding (12vw margins)

#### Auto-scrolling Behavior
- **Speed**: Configurable via `scroll_speed` (pixels per second)
- **Direction**: Top to bottom (newest posts first)
- **Loop**: After reaching bottom, pause 3 seconds then restart from top
- **Initial delay**: 2 seconds pause on first post before scrolling begins
- **Manual control**: 
  - Mouse wheel/touch pauses auto-scroll for 4 seconds
  - Hover over viewport pauses scrolling

#### Typography
- **Post text**: 2rem font size
- **Channel names**: 1.3rem font size
- **Dates/metadata**: 1rem font size
- **Line height**: 1.55 for optimal readability

#### Content Display
- **Posts per tile**: 1 post per tile, ~2 tiles visible per screen
- **Text handling**: Full text display (no truncation)
- **Media**: Photos/video thumbnails with 120px max height
- **Link previews**: Embedded cards with title and description
- **Interactions**: Click anywhere on tile to open post in Telegram

#### Channel Filtering
- **UI**: Filter buttons in header (when multiple channels configured)
- **Behavior**: Show all posts or filter by specific channel
- **State**: Maintains filter selection across refreshes

### 5. Data Flow
1. Frontend loads and fetches `/api/config` for settings
2. Frontend fetches `/api/posts` for initial data
3. Posts are rendered as scrolling tiles (newest first)
4. Auto-refresh every N minutes (configurable)
5. Backend scrapes all channels concurrently on each request
6. Posts are merged, sorted by datetime (newest first), and limited to max_posts

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

### 7. Dependencies
```
fastapi[standard]
httpx
beautifulsoup4
```

## Technical Specifications

### Error Handling
- **Network failures**: Silently skip failed channels, log errors
- **Parse errors**: Continue processing other posts
- **Empty responses**: Return empty array, frontend shows "No posts" message

### Performance
- **Async I/O**: All HTTP requests are concurrent
- **No caching**: Ensures fresh data but increases load time
- **Timeout**: 15-second timeout per HTTP request

### Security
- **Input sanitization**: HTML content is sanitized in frontend
- **CORS**: Not required (same-origin requests)
- **Rate limiting**: None implemented (relies on Telegram's rate limiting)

### Browser Compatibility
- **Modern browsers**: Chrome 90+, Firefox 88+, Safari 14+
- **Features used**: CSS Grid, Flexbox, async/await, fetch API
- **Fallbacks**: None implemented (assumes modern browser support)

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

### Multi-channel with custom settings:
```json
{
  "channels": [
    "https://t.me/abualiexpress",
    "@ziv710", 
    "alexmehacarmel"
  ],
  "refresh_interval_minutes": 3,
  "max_posts": 30,
  "scroll_speed": 75
}
```

## Known Limitations

1. **Public channels only**: Cannot access private channels or channels requiring authentication
2. **Rate limiting**: Subject to Telegram's rate limiting on public preview pages
3. **Content restrictions**: Some media may not be accessible in web preview format
4. **Network dependencies**: Requires outbound HTTP access to t.me (may be blocked in some hosting environments)
5. **No persistence**: Configuration changes require server restart or file modification