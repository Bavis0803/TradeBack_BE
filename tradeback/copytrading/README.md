# Copy trading runtime

Copy trading uses two long-running processes:

```powershell
daphne -b 127.0.0.1 -p 8000 tradeback.asgi:application
python manage.py run_telegram_worker
```

Run both commands from `TradeBack_BE/tradeback`. Restart the Telegram worker after
adding a brand-new Telegram account. New strategies on an already connected
account are picked up without a restart.

Production must set a shared `REDIS_URL`; the in-memory channel layer is intended
only for one-process development and tests. The REST polling fallback still keeps
the chat window current when WebSocket delivery is temporarily unavailable.

Live trading has two independent gates:

1. Set `COPY_TRADING_LIVE_ENABLED=True` on the backend.
2. The user must explicitly select and confirm `LIVE` mode for the strategy.

Keep the kill-switch off while validating a new signal format. Optional safety
configuration:

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
COPY_TRADING_LIVE_ENABLED=False
COPY_TRADING_MAX_ALLOCATION_USDT=1000
```

Imported Telegram history is parsed for display only and can never execute an
order. Only messages received by the worker after a strategy is active can reach
the execution engine.
