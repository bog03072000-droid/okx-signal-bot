FROM python:3.11-slim

# non-root user — якщо хтось знайде RCE в одній із залежностей, процес не матиме
# root-прав всередині контейнера
RUN useradd -m -u 1000 botuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data && chown -R botuser:botuser /app

USER botuser

# СКЕПТИЧНИЙ КОМЕНТАР: цей healthcheck перевіряє лише, що asyncio event loop
# оновлює data/heartbeat.txt (див. heartbeat_loop() в main.py) — тобто що процес
# не завис. Він НЕ перевіряє, що Telethon-сесія жива, що control-бот відповідає
# на команди, чи що Claude/OKX API доступні. Контейнер може бути "healthy" і
# водночас мати відключений Telegram-listener — дивись логи, не покладайся
# лише на статус healthcheck.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "\
import os, time, sys; \
p = '/app/data/heartbeat.txt'; \
sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 90 else 1)"

CMD ["python", "main.py"]
