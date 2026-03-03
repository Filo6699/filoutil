# filoutil – Telegram Bot

A feature-rich Telegram bot built with **python-telegram-bot** that provides monitoring, Moodle integration, notifications, and administrative capabilities.

## Features

- **Monitoring System**: Track and monitor various services and endpoints
- **Moodle Integration**:
  - Course watching and notifications
  - Session management and automatic refresh
  - Quiet hours configuration
- **Notification Management**: Customizable notification settings with blacklist words
- **User Management**: Whitelist-based access control with role-based permissions
- **Admin Tools**: Database queries, shell commands, and user administration
- **Health Monitoring**: Bot heartbeat tracking and downtime notifications

## Setup

### 1) Create your `.env` file

Create a `.env` file in the project root with the following variables:

```bash
# Required
TELEGRAM_BOT_TOKEN=your_bot_token_here
DATABASE_URL=postgresql://postgres:postgres@db:5432/filoutil

# Optional
TIMEZONE=Asia/Almaty
LOG_LEVEL=INFO
HTTPX_LOG_LEVEL=WARNING
HEARTBEAT_INTERVAL=60
```

**Note:** For Docker Compose, the default `DATABASE_URL` connects to the `db` service defined in `docker-compose.yaml`.

### 2) Start services

```bash
docker compose up -d --build
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | - | Telegram bot token from @BotFather |
| `DATABASE_URL` | Yes* | `postgresql://postgres:postgres@localhost:5001/filoutil` | PostgreSQL connection string |
| `TIMEZONE` | No | `Asia/Almaty` | Application timezone (e.g., `UTC`, `America/New_York`) |
| `LOG_LEVEL` | No | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `HTTPX_LOG_LEVEL` | No | `WARNING` | HTTPX library logging level |
| `HEARTBEAT_INTERVAL` | No | `60` | Heartbeat update interval in seconds |
| `USER_AGENT` | No | `fizbuz` | HTTP `User-Agent` header for outgoing requests |
| `OIDC_RECOVERY_USER_AGENT` | No | `fizbuz-session-recovery` | HTTP `User-Agent` header used only for OIDC session recovery |

## Development

### Pre-commit hooks

Install pre-commit hooks to ensure code quality:

```bash
pre-commit install
```

### Code formatting

The project uses `black` and `isort` for code formatting. Configuration is in `pyproject.toml`.

## Commands

- `/start` - Initialize bot and show main menu
- `/menu` - Show main menu
- `/monitor` or `/m` - Access monitoring features
- `/notifications` or `/n` - View notifications
- `/notification_settings` - Configure notification preferences
- `/moodle` - Access Moodle features
- `/refresh_session` - Manually refresh Moodle session
- `/refresh_settings` - Configure session refresh settings

Admin-only commands:
- `/admin` - User administration
- `/shell` - Execute shell commands
- `/db` - Execute database queries
