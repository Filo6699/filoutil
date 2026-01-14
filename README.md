# filoutil – Telegram hello-world bot

This repo contains a minimal **python-telegram-bot** polling bot.

## Setup

Create a bot token via [@BotFather](https://t.me/BotFather).

### Create venv + install deps

```bash
cd /home/filo/github/filoutil
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python -m filoutil.app
```

## Production (single VPS, all Docker)

### 1) Create your `.env`

Copy `env.example` to `.env` and fill in your bot token:

```bash
cp env.example .env
```

Required:
- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL` (default for compose: `postgresql://postgres:postgres@db:5432/filoutil`)

### 2) Start

```bash
docker compose up -d --build
```

### 3) Logs / status

```bash
docker compose ps
docker compose logs -f bot
```

## Development

###

Install convex `npm` package to setup the developer environment to work with convex
```bash
npm install
npx convex dev
```

### Pre-commit procedures

Install pre-commit hooks:

```bash
pre-commit install
```
