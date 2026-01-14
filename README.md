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
python bot.py
```

Then open your bot chat in Telegram and try:
- `/start`
- `/hello`
- send any text message (it will echo)

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
