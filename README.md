# OKX Signal Bot (Solana, dry-run first)

Бот слухає Telegram-канал, парсить довільний текст сигналів через Claude API,
проганяє токен через скринінг на скам/honeypot, застосовує ризик-ліміти
і виконує (або симулює) своп через OKX DEX Aggregator API на Solana.

## Встановлення

```bash
pip install -r requirements.txt
cp .env.example .env
# відредагуй .env — впиши свої ключі
```

### Що потрібно отримати перед стартом

1. **Telegram API ID/Hash** — https://my.telegram.org → API development tools
2. **Claude API ключ** — https://console.anthropic.com
3. **OKX DEX API ключі** (для реальних угод, не потрібні для dry-run) —
   https://web3.okx.com → Developer Portal
4. **Solana гаманець** — окремий "робочий" гаманець з обмеженим капіталом,
   НЕ основний гаманець з заощадженнями
5. **Telegram control-бот** — токен від @BotFather (`/newbot`) і твій
   особистий `user_id` (не username) — дивись розділ "Telegram control-бот" нижче

## Запуск (dry-run — за замовчуванням)

```bash
python main.py
```

При першому запуску Telethon попросить увійти в Telegram-акаунт (код з СМС) —
це нормально, так бот отримує доступ читати канал від твого імені.

В `.env` за замовчуванням `DRY_RUN=true` — бот робить усе: слухає, парсить,
скринить, рахує розмір позиції, отримує quote від OKX — але **не відправляє**
жодної реальної транзакції. Всі "угоди" пишуться в `data/bot.db` з позначкою
`dry_run=True`, і ти отримуєш сповіщення з префіксом `🧪 [DRY RUN]`.

## Перевір результати dry-run

```bash
python -c "
from core.storage import get_session, SignalLog, Trade
s = get_session()
print('Всього сигналів:', s.query(SignalLog).count())
print('Виконано:', s.query(SignalLog).filter_by(executed=True).count())
print('Відхилено:', s.query(SignalLog).filter_by(executed=False).count())
for t in s.query(Trade).all():
    print(t.action, t.token_symbol, t.amount_usd, t.status)
"
```

Раджу прогнати dry-run **мінімум 1-2 тижні** і подивитись:
- Скільки сигналів парсер розпізнає правильно (звіряй `raw_text` з `reasoning` вручну)
- Скільки токенів не проходять скринінг і чому
- Чи адекватні розміри позицій і чи спрацьовують ризик-ліміти як очікується

## Перехід з dry-run на реальні угоди

Це **не рекомендується робити відразу**. Коли вирішиш переходити:

1. Заповни `SOLANA_PRIVATE_KEY`, `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`, `OKX_PROJECT_ID` в `.env`
2. Постав `DRY_RUN=false`
3. Реалізуй метод `_sign_and_broadcast()` в `core/okx_dex_client.py` — він навмисно
   залишений як `NotImplementedError`, щоб підключення реального гаманця було
   свідомим окремим кроком, а не випадковим. Потрібно:
   ```python
   from solders.keypair import Keypair
   from solana.rpc.api import Client
   from solders.transaction import VersionedTransaction

   keypair = Keypair.from_base58_string(settings.solana_private_key)
   rpc_client = Client(settings.solana_rpc_url)
   # tx_data містить серіалізовану транзакцію від OKX — десеріалізувати,
   # підписати keypair, відправити через rpc_client.send_raw_transaction()
   ```
4. Почни з **мінімального капіталу** ($50-200) на кілька днів
5. Постав дуже консервативні ліміти (`MAX_POSITION_PCT=1`, `MAX_OPEN_POSITIONS=3`)
   і поступово розширюй

## Telegram control-бот

Окремий бот-акаунт (Bot API через `aiogram`, НЕ той самий обліковий запис, що
слухає канал через Telethon) для керування ботом прямо з Telegram, без доступу
до сервера. Працює в тому самому процесі, що й listener.

### Налаштування

1. У Telegram напиши [@BotFather](https://t.me/BotFather) → `/newbot`,
   отримай токен → впиши в `.env` як `TG_BOT_TOKEN`
2. Дізнайся свій `user_id` (не username!) — напр. напиши
   [@userinfobot](https://t.me/userinfobot) → впиши в `.env` як `TG_OWNER_USER_ID`
3. Перезапусти бота — control-бот стартує автоматично разом з listener'ом

**Команди від будь-кого, крім `TG_OWNER_USER_ID`, мовчки ігноруються** (без
відповіді в чат) — навіть якщо хтось випадково знайде юзернейм бота.

### Команди

| Команда | Що робить |
|---|---|
| `/status` | Режим (dry-run/live), пауза так/ні, сигнали/угоди за сьогодні |
| `/balance` | Баланс гаманця (SOL + USD), сума у відкритих позиціях |
| `/positions` | Відкриті позиції по токенах (PnL н/д — див. застереження нижче) |
| `/history [N]` | Останні N угод з `data/bot.db` (за замовчуванням 10) |
| `/stop` | Негайно ставить бота на паузу — сигнали далі логуються, але жодних свопів |
| `/start` | Знімає паузу |
| `/limits` | Поточні ризик-ліміти (з .env або перевизначені) |
| `/setlimit НАЗВА значення` | Змінює ліміт на льоту, без перезапуску (напр. `/setlimit MAX_OPEN_POSITIONS 5`) |

`/setlimit НАЗВА default` скидає ліміт назад до значення з `.env`.
Перевизначення зберігаються в `data/runtime_state.json` (не в git).

**Застереження:**
- `/stop` блокує виконання угод на рівні `risk_manager.check_paused()` — це
  окрема перевірка в тому самому ланцюжку, що й інші risk-check'и в
  `main.py`, а не просто "вимкнення" тг-бота.
- `/positions` показує суму USD, вкладену в токен (buy − sell), **не** реальний
  PnL — для цього потрібен окремий моніторинг поточної ціни, якого зараз нема.
- `/balance` без підключеного гаманця (`SOLANA_PRIVATE_KEY` порожній) показує
  той самий mock-баланс, що dry-run використовує для розрахунку розміру
  позиції — це НЕ реальні кошти.
- Авторизація — лише Telegram `user_id`. Якщо акаунт власника скомпрометовано,
  зловмисник отримує й контроль над ботом. Для реальних грошей варто додати
  другий фактор підтвердження критичних команд — зараз цього нема.

## Структура проєкту

```
core/
  config.py          — вся конфігурація з .env + ефективні ліміти (get_limit)
  runtime_state.py    — пауза торгівлі і перевизначені ліміти (data/runtime_state.json)
  control_bot.py       — Telegram control-бот (aiogram): /status /stop /setlimit тощо
  wallet.py            — баланс гаманця (SOL через RPC + курс через CoinGecko)
  signal_parser.py    — Claude API парсинг тексту → структурований сигнал
  token_screener.py   — перевірка на скам (DexScreener + GoPlus)
  risk_manager.py     — ліміти позицій, cooldown, денний loss limit, пауза
  okx_dex_client.py   — quote + swap через OKX DEX API (dry-run підтримка)
  storage.py          — SQLite лог сигналів і угод
main.py               — Telegram listener + control-бот + оркестрація пайплайну
data/bot.db           — створюється автоматично при першому запуску
data/runtime_state.json — пауза і перевизначені ліміти, створюється control-ботом
```

## Важливі застереження

- **Приватний ключ гаманця ніколи не комітьте в git.** `.env` вже в `.gitignore`
  (перевір це перед першим комітом).
- **Бот НЕ шукає токен-адресу за тікером автоматично.** Якщо сигнал містить лише
  назву токена без адреси контракту — бот навмисно відхиляє угоду. Це захист
  від скам-токенів з однаковою назвою. Можна розширити функцію
  `resolve_contract_address()` в `main.py`, додавши пошук через DexScreener
  search API, але це підвищує ризик помилково купити не той токен.
- **Sell-side логіка спрощена.** Зараз бот продає, лише якщо в сигналі явно
  вказана адреса контракту. Take-profit/stop-loss за замовчуванням (незалежно
  від сигналів каналу) описані в плані, але ще не реалізовані в коді —
  додай окремий модуль моніторингу відкритих позицій, якщо це критично для тебе.
- **Розрахунок розміру позиції в `process_signal()` (main.py) досі замоканий**
  (`MOCK_WALLET_BALANCE_USD`) — навіть попри те, що команда `/balance` в
  control-боті вже вміє показувати реальний баланс через Solana RPC
  (`core/wallet.py`), сам пайплайн виконання угод його ще не використовує.
  Перед реальним запуском заміни `MOCK_WALLET_BALANCE_USD` в `main.py` на
  виклик `core.wallet.get_wallet_balance()`.
- Курс SOL/USD для розрахунку суми свопу в `main.py` (рядок з
  `amount_raw = ...`) досі захардкоджений як приблизний ($150) — `core/wallet.py`
  вже тягне реальний курс з CoinGecko для `/balance`, але сам розрахунок суми
  свопу цей код поки не перевикористовує.

## Деплой на DigitalOcean

Автодеплой при `git push` в `main` через GitHub Actions + SSH + Docker. Регіон
за замовчуванням — Frankfurt. Інструкція розрахована на те, що ти раніше не
деплоїв нічого на сервер — виконуй команди по порядку, дослівно.

### Docker чи systemd?

У репозиторії є ОБИДВА варіанти (`Dockerfile`/`docker-compose.yml` і
`deploy/okx-bot.service`), але **автодеплой нижче налаштований під Docker** —
він простіший саме для тебе, бо:
- не треба вручну ставити Python 3.11, venv і системні залежності на сервері —
  все це вже описано в `Dockerfile`, і буде однаково на будь-якому droplet'і;
- `git pull && docker compose build && docker compose up -d` — весь деплой у
  трьох командах, легко повторити вручну, якщо CI зламається;
- перезапуск при падінні (`restart: unless-stopped`) з коробки, без окремого
  налаштування systemd.

systemd-варіант лишається як запасний, якщо колись захочеш прибрати Docker
(напр. на дуже слабкому droplet'і, де оверхед контейнера відчутний) — тоді
дивись `deploy/okx-bot.service` і онови GitHub Actions workflow під нього.

### 1. Створи droplet

1. Зареєструйся/увійди на https://cloud.digitalocean.com
2. **Create → Droplets**
3. Image: **Ubuntu 22.04 (LTS) x64**
4. Plan: **Basic → Regular → $6/міс** (1 GB RAM цілком достатньо для цього бота)
5. Region: **Frankfurt**
6. Authentication: **SSH Key** (якщо ще нема ключа — дивись крок 2 нижче, спочатку згенеруй його, потім повернись сюди і додай)
7. Натисни **Create Droplet**, зачекай ~1 хвилину, скопіюй його IP-адресу

### 2. Згенеруй SSH-ключ і додай на droplet + в GitHub Secrets

На своєму комп'ютері:

```bash
ssh-keygen -t ed25519 -C "okx-signal-bot-deploy" -f ~/.ssh/okx_bot_deploy_key -N ""
```

Це створить два файли: `~/.ssh/okx_bot_deploy_key` (приватний, НІКОМУ не показуй)
і `~/.ssh/okx_bot_deploy_key.pub` (публічний, його додаєш на сервер).

- При створенні droplet'а (крок 1.6) встав вміст `okx_bot_deploy_key.pub` у
  DigitalOcean → SSH Keys → New SSH Key
- Якщо droplet вже створено раніше — залогінься на нього паролем/іншим ключем і додай рядок з `.pub`-файлу в `~/.ssh/authorized_keys`

Тепер додай секрети в GitHub: відкрий репозиторій на GitHub →
**Settings → Secrets and variables → Actions → New repository secret**,
додай три секрети:

| Назва секрету | Значення |
|---|---|
| `DROPLET_HOST` | IP-адреса droplet'а (з кроку 1) |
| `DROPLET_USER` | `root` (або інший користувач, якщо створив окремого) |
| `DROPLET_SSH_KEY` | **весь вміст** приватного файлу `~/.ssh/okx_bot_deploy_key` (від `-----BEGIN...` до `-----END...`) |

### 3. Базовий firewall

Підключись на droplet: `ssh root@<IP_droplet>`, потім:

```bash
ufw allow 22/tcp
ufw enable
ufw status
```

Бот сам ні до кого не приймає вхідних з'єднань (тільки виходить назовні —
до Telegram, Claude, OKX, DexScreener), тому окрім SSH (22) більше нічого
відкривати не треба.

### 4. Встанови Docker і зроби перший (ручний) деплой

Все ще на droplet'і (`ssh root@<IP_droplet>`):

```bash
# Docker
curl -fsSL https://get.docker.com | sh

# git
apt update && apt install -y git nano

# клонуй репозиторій
mkdir -p /opt && cd /opt
git clone https://github.com/<твій_github_логін>/okx-signal-bot.git
cd okx-signal-bot

# .env створюється ВРУЧНУ прямо на сервері — він НІКОЛИ не комітиться в git
cp .env.example .env
nano .env
# заповни всі ключі реальними значеннями, для першого разу лиши DRY_RUN=true
# збережи: Ctrl+O, Enter, вийди: Ctrl+X

# права на data/ під non-root user з Dockerfile (uid 1000)
mkdir -p data
chown -R 1000:1000 data

# перша збірка і запуск
docker compose build
docker compose up -d

# перевір, що піднявся
docker ps
docker logs -f okx-signal-bot
# Ctrl+C щоб вийти з перегляду логів (контейнер продовжить працювати)
```

При першому запуску Telethon попросить код підтвердження з Telegram —
дивись його прямо у виводі `docker logs -f okx-signal-bot` і вводь через
`docker attach okx-signal-bot` (або одноразово запусти
`docker compose run --rm bot python main.py` в інтерактивному режимі саме для
першого логіну, потім `Ctrl+C` і вже `docker compose up -d` для постійної роботи).

### 5. Перевір автодеплой

На своєму комп'ютері, у репозиторії:

```bash
git commit --allow-empty -m "test: перевірка автодеплою"
git push origin main
```

Далі на GitHub → вкладка **Actions** → відкрий запущений workflow **Deploy to
DigitalOcean** і подивись логи. Якщо все ок — крок з `docker inspect` покаже
`healthy`. Якщо ні — там же будуть останні логи контейнера з droplet'а.

### Логи на сервері

```bash
docker logs -f okx-signal-bot          # логи в реальному часі
docker logs --tail 100 okx-signal-bot  # останні 100 рядків
```

(Якщо колись перейдеш на systemd-варіант — `journalctl -u okx-bot -f`.)

## Наступні кроки, які варто обговорити зі мною

- Реалізація `_sign_and_broadcast` з реальним гаманцем
- Take-profit/stop-loss моніторинг відкритих позицій (окремий фоновий процес) —
  також дав би реальний PnL для `/positions` замість поточного "USD вкладено"
- Розширення на ETH/BSC після стабілізації на Solana
- Другий фактор підтвердження для критичних команд control-бота (`/stop`,
  `/setlimit`) — зараз єдиний захист це перевірка `TG_OWNER_USER_ID`
- Build-and-push Docker-образу в GitHub Actions (замість збірки прямо на
  droplet'і) — швидше й безпечніше, якщо образ стане важким
