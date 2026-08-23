# TradeBack deployment

The production stack contains PostgreSQL, Redis, Django ASGI, one Telegram
listener worker, a Strategy training/execution worker, and React behind Nginx. For one user, use at least 2 vCPU,
4 GB RAM, and 40 GB SSD on Ubuntu 24.04 LTS.

## Jenkins one-click deployment

1. Install Docker Engine, Docker Compose, Jenkins, Git, and curl on the VPS.
2. Allow the Jenkins user to run Docker.
3. Create a Jenkins **Secret file** credential named
   `tradeback-production-env` from `.env.example`.
4. Set the job to **Pipeline script from SCM**, repository
   `https://github.com/Bavis0803/TradeBack_BE.git`, branch `main`, script path
   `Jenkinsfile`.
5. Press **Build Now**. The pipeline checks out FE, tests both projects, builds,
   deploys, and checks `/health/`.

Production values must include a strong `DJANGO_SECRET_KEY`, a stable
`EXCHANGE_CREDENTIAL_ENCRYPTION_KEY`, `DEBUG=False`, PostgreSQL credentials,
the real domain in `ALLOWED_HOSTS`, and HTTPS URLs in both origin lists.

`COPY_TRADING_LIVE_ENABLED=True` is enforced by Compose. This enables the
global LIVE capability only: each stream still requires explicit confirmation
and remains protected by symbol, balance, entry, leverage, daily-loss, TP/SL,
idempotency, and emergency-close checks.

Set `STRATEGY_LIVE_ENABLED=True` to permit trained strategies to place real
Binance Futures orders and set `STRATEGY_MAX_BUDGET_USDT` to an account-level
server cap. LIVE activation still requires a verified Binance connection and
an explicit confirmation. The `strategy-worker` keeps backtests and PAPER/LIVE
execution running when the browser is closed.

The frontend and WebSocket use the same public origin. Nginx forwards all API
and `/ws/` traffic to Django, so the browser never calls Binance or Telegram
directly and never exposes backend port 8000.

Back up the `postgres_data` and `media_data` Docker volumes regularly.
