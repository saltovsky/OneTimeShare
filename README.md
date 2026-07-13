# OneTimeShare

> Легковесный self-hosted сервис обмена файлами с режимом **burn-after-reading**:
> получатель скачивает файл(ы) — и ссылка вместе с физическими файлами
> навсегда удаляется с сервера. Если получатель прервал загрузку на полпути —
> файлы остаются, можно повторить попытку.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

## ✨ Возможности

- 🔐 **Авторизация для загрузки** — форма логина на главной странице
  + сессионная HMAC-кука (HttpOnly, SameSite=Lax, TTL 8 ч) + HTTP Basic для API
- 🔒 **Burn-after-reading** с точным определением успешности стриминга:
  файл удаляется **только** при штатном завершении HTTP-ответа. TCP-RST /
  обрыв соединения / закрытие вкладки — файлы **сохраняются**
- 📦 **ZIP-на-лету** для множественных файлов (без записи временного архива на диск)
- 🛡 **Argon2id** для хеширования паролей скачивания
- 🛠 **Админ-панель** со статистикой, ручным отзывом ссылок, управлением пользователями и настройками
- 🌓 **Светлая и тёмная темы** (auto-detect + ручной toggle, сохраняется в браузере)
- 🌐 **Двуязычный UI**: 🇷🇺 Русский (по умолчанию) / 🇬🇧 English с переключателем в шапке
- 🧹 Фоновая очистка осиротевших файлов
- 🐳 **Docker-ready**: non-root, healthcheck, log rotation, single command deploy

## 🏗 Стек

| Слой | Технология |
|---|---|
| Backend | Python 3.12 + FastAPI (async / StreamingResponse) |
| DB | SQLite через `aiosqlite` (один файл в Docker-томе) |
| Storage | Локальная ФС, мапнутая в Docker volume |
| Frontend | Jinja2 + TailwindCSS (CDN) + vanilla JS — без шаблонизатора/сборки |
| Hashing | `argon2-cffi` (Argon2id, OWASP recommendation) |
| Sessions / cookies | `itsdangerous` (HMAC-SHA256) |
| ZIP streaming | `zipstream-ng` в отдельном thread-производителе |

## 🚀 Быстрый старт

```bash
cd OneTimeShare
cp .env.example .env
# Обязательно отредактируйте .env — смените пароли и SECRET_KEY!
#   ADMIN_PASSWORD   — для доступа к /admin
#   UPLOADER_PASSWORD — для доступа к загрузке
#   SECRET_KEY       — для подписи session/download cookies

docker compose up -d --build
```

Откройте **<http://localhost:8000>** — увидите форму логина.

Панель администратора: **<http://localhost:8000/admin>**.

### Полезные команды

```bash
docker compose logs -f                 # логи
docker compose restart                 # рестарт
docker compose down                    # остановка (данные в volume сохранятся)
docker volume inspect onetimeshare_data # где лежат файлы и БД
docker compose down -v                 # ⚠️ удалить ВСЕ данные (БД + файлы)
```

## 👥 Роли и права

Пользователи хранятся в БД (таблица `users`). Начальные admin и uploader
создаются из переменных окружения при первом запуске. Дальнейшее управление —
через админ-панель (`/admin` → вкладка «Пользователи»).

| Роль | Права |
|---|---|
| **Admin** | Полный доступ в админку. Видит **все** ссылки всех пользователей. Может добавлять/удалять пользователей, менять текст подвала. Может загружать файлы. |
| **Uploader** | Может загружать файлы. В админке видит **только свои** ссылки (вкладки «Пользователи» и «Настройки» скрыты). |
| **Recipient** (получатель) | Только URL ссылки (и пароль, если задан). Учётная запись не нужна. |

### Каналы доступа к API

Веб-интерфейс использует **сессионные куки** (HttpOnly, SameSite=Lax).
Для `curl`-скриптов продублирован **HTTP Basic** на тех же эндпоинтах
(зависимость `_or_session` принимает оба варианта):

```bash
# Через сессию (cookie ставится после POST /login)
curl -b cookies.txt -F "files=@photo.jpg" http://host/api/upload

# Через HTTP Basic (для скриптов)
curl -u alice:alicepw -F "files=@photo.jpg" http://host/api/upload
```

## 🌍 Локализация

- **По умолчанию**: 🇷🇺 Русский
- **Переключение**: кнопка `RU | EN` в правом верхнем углу шапки
- **Автоопределение**: если пользователь ничего не выбрал — берётся язык браузера (`en` → English, остальное → Русский)
- **Deep-link**: `?lang=en` или `?lang=ru` в URL — запоминается в `localStorage`
- **Сохранение**: `localStorage['ots-lang']`, переживает рестарт браузера
- **Словарь**: ~70 ключей × 2 языка в `app/static/i18n.js`
- **Без зависимостей**: нативный JS, без библиотек i18n
- **Pluralization**: корректные формы русского (`1 файл`, `2 файла`, `5 файлов`, `11 файлов`)
- **Локализация единиц**: `1.5 МБ` ↔ `1.5 MB`

## 🌓 Темы

- **По умолчанию**: системная (`prefers-color-scheme`)
- **Переключение**: кнопка ☀/🌙 в правом верхнем углу
- **Сохранение**: `localStorage['ots-theme']`
- **Без flash**: pre-render inline-скрипт в `<head>` ставит класс `dark` ДО первого пейнта
- **Анимация**: rotate+scale+opacity transition 400ms (cubic-bezier)
- **Reactive**: если пользователь не делал явный выбор — тема следует за системой в реальном времени

## 🔌 API

| Method | Path | Auth | Описание |
|---|---|---|---|
| `GET` | `/` | session (uploader/admin) | Главная: login-форма или upload-форма |
| `POST` | `/login` | — | Логин. Устанавливает `ots_session` cookie |
| `GET` | `/logout` | — | Очищает сессию |
| `POST` | `/upload` | session OR Basic | Форма загрузки → редирект на страницу успеха |
| `GET` | `/upload/success` | session | Страница «Ссылка готова» с URL и кнопкой «Создать новую» |
| `POST` | `/api/upload` | session OR Basic | Загрузка через API. Возвращает JSON с `link_id` |
| `GET` | `/d/{id}` | — (анонимно) | Страница скачивания (или форма пароля) |
| `POST` | `/d/{id}/unlock` | — | Проверка пароля. Ставит `ots_dl_auth` cookie |
| `GET` | `/d/{id}/file` | — (+ cookie если есть пароль) | **Стрим файла + burn-after** |
| `GET` | `/admin` | session | Админ-панель: вкладка «Ссылки» (uploader видит только свои) |
| `GET` | `/admin/users` | session (admin) | Вкладка «Пользователи»: список, добавить, удалить |
| `GET` | `/admin/settings` | session (admin) | Вкладка «Настройки»: текст подвала |
| `POST` | `/admin/{id}/revoke` | session OR Basic | Ручное удаление ссылки |
| `POST` | `/admin/users` | session (admin) | Добавить пользователя |
| `POST` | `/admin/users/{username}/delete` | session (admin) | Удалить пользователя |
| `POST` | `/admin/settings/footer` | session (admin) | Изменить текст подвала |
| `GET` | `/health` | — | `{"status":"ok"}` (для Docker healthcheck) |
| `GET` | `/static/*` | — | Статические файлы (i18n.js) |

### Пример: загрузка + скачивание через curl

```bash
# 1) Загрузить
RESP=$(curl -s -u alice:alicepw -F "files=@doc.pdf" -F "password=secret" \
  http://localhost:8000/api/upload)
LINK=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['link_id'])")

# 2) Открыть страницу
xdg-open "http://localhost:8000/d/$LINK"

# 3) Ввести пароль и скачать
curl -c /tmp/c -d "password=secret" "http://localhost:8000/d/$LINK/unlock" >/dev/null
curl -b /tmp/c -o doc.pdf "http://localhost:8000/d/$LINK/file"
# После этого скачивания ссылка и файл уничтожены — второй запрос вернёт 404
```

## ⚙️ Конфигурация (env)

| Переменная | По умолчанию | Описание |
|---|---|---|---|
| `ADMIN_USERNAME` | `admin` | Логин начального админа (сид в БД при первом запуске) |
| `ADMIN_PASSWORD` | `changeme` | Пароль начального админа (**ОБЯЗАТЕЛЬНО сменить**) |
| `UPLOADER_USERNAME` | `uploader` | Логин начального загрузчика (сид в БД при первом запуске) |
| `UPLOADER_PASSWORD` | `changeme` | Пароль начального загрузчика (**ОБЯЗАТЕЛЬНО сменить**) |
| `SECRET_KEY` | (random) | Секрет для HMAC-подписи cookies (**ОБЯЗАТЕЛЬНО сменить**; если не задан — генерируется при старте, cookies не переживают рестарт) |

> После первого запуска пользователи хранятся в БД. Управление — через
> админ-панель: `/admin` → вкладка «Пользователи». Env-переменные
> `ADMIN_*` / `UPLOADER_*` используются только для начального сида.

| `MAX_FILE_SIZE` | `104857600` | Макс. размер одного файла, байт (100 МБ) |
| `MAX_TOTAL_SIZE` | `524288000` | Макс. суммарный размер загрузки, байт (500 МБ) |
| `CLEANUP_INTERVAL_SECONDS` | `3600` | Интервал фоновой очистки осиротевших файлов |
| `APP_URL` | `http://localhost:8000` | Публичный URL (для отображения ссылки в UI) |
| `BIND_ADDR` | `127.0.0.1` | Адрес, на котором compose публикует порт (`0.0.0.0` для reverse-proxy) |
| `LOG_LEVEL` | `INFO` | Уровень логирования (DEBUG/INFO/WARNING/ERROR) |

## 🛡 Безопасность

### Аутентификация
- **Session cookies** подписаны `itsdangerous.URLSafeTimedSerializer` (HMAC-SHA256)
- `HttpOnly` + `SameSite=Lax` — JS не может прочитать, CSRF с других сайтов не работает
- `secrets.compare_digest` — constant-time сравнение credentials
- Open-redirect protection на `?next=` параметре (только same-site path)
- Логирование всех успешных/неуспешных попыток с IP

### Защита от обхода
- **Path Traversal:** все оригинальные имена файлов проходят через
  `os.path.basename()` + whitelist-регексп `[A-Za-z0-9._\- ]`. Дополнительная
  проверка через `os.path.realpath()` — итоговый путь обязан лежать внутри
  `UPLOAD_DIR`. На диск файлы сохраняются как `<uuid4_hex>_<safe_name>`,
  перечисление невозможно.

### Burn-after-reading
Ключевая инвариантность реализована через async-генератор `burn_after_stream`:

```python
async def burn_after_stream(source, on_success):
    success = False
    try:
        async for chunk in source:
            yield chunk
        success = True
    except (asyncio.CancelledError, GeneratorExit):
        # Starlette выкидывает CancelledError когда клиент закрыл соединение
        raise
    except (ConnectionResetError, BrokenPipeError):
        raise
    finally:
        if success:
            await on_success()  # удалить файлы + пометить ссылку consumed
```

Callback `on_success` вызывается **только** при штатном завершении итерации.
TCP-RST, закрытие вкладки, `kill` curl'а — файлы остаются, получатель может
повторить попытку.

`on_success` использует **свежую DB-сессию** (не request-session), потому что
request-session может быть закрыта к моменту `finally` блока генератора.

### Хеширование паролей скачивания
- **Argon2id** через `argon2-cffi` (OWASP 2024+ рекомендация)
- Оригинал пароля **нигде не логируется**
- Per-link, не глобальный

### HTTP-заголовки
- `X-Content-Type-Options: nosniff`
- `Cache-Control: no-store` (на streaming endpoint)
- `Content-Disposition` с `filename*` (RFC 5987) для корректной UTF-8
- `WWW-Authenticate: Basic realm=...` на API 401 ответах

### Контейнер
- Non-root user (`uid 1001`)
- `no-new-privileges:true`
- Лимит логов: `json-file` 10 МБ × 3 файла
- Read-only filesystem *вне* `/app/data` (TODO: сделать в compose)

### Production deployment
- **Обязательно** reverse-proxy с TLS (Caddy / Traefik / nginx) — иначе
  пароли и куки передаются открытым текстом
- Выставьте `BIND_ADDR=0.0.0.0` в `.env`
- `APP_URL` должен указывать на публичный HTTPS-URL
- За reverse-proxy куки автоматически ставятся `secure=True` (TODO: доработка)

## 🗃 Схема БД

```sql
users
  username          TEXT PRIMARY KEY     -- логин
  password_hash     TEXT NOT NULL        -- Argon2id hash
  role              TEXT NOT NULL        -- "admin" | "uploader"
  created_at        DATETIME

settings
  key               TEXT PRIMARY KEY     -- "footer_text" и др.
  value             TEXT NOT NULL

links
  id                TEXT PRIMARY KEY     -- UUID4 hex (32 chars)
  password_hash     TEXT NULL            -- Argon2id hash (опционально)
  created_at        DATETIME
  is_downloaded     BOOLEAN              -- True => ссылка "сожжена"
  total_size_bytes  BIGINT
  uploader_id       TEXT NULL FK         -- users.username, кто загрузил

files
  id                INTEGER PRIMARY KEY (autoincrement)
  link_id           TEXT FK -> links.id ON DELETE CASCADE
  original_filename TEXT
  stored_filepath   TEXT
  size_bytes        BIGINT
```

**TTL у ссылок нет** — они живут вечно, пока не будут скачаны получателем
или отозваны администратором. Схема автоматически мигрирует через `ALTER TABLE`
при обновлении (добавление `uploader_id` и новых таблиц).

## 📂 Где хранятся данные

Всё (SQLite БД + загруженные файлы) лежит в Docker-томе `onetimeshare_data`,
смонтированном в `/app/data` внутри контейнера:

```
/app/data/
├── onetimeshare.db          # SQLite (≈10 КБ пустая)
└── uploads/                 # загруженные файлы
    ├── 0641917ba1a14123..._report.pdf
    └── 5fe2a3c0b88e4a8d..._photo.jpg
```

Для бэкапа достаточно снапшотить том (`docker volume inspect onetimeshare_data`)
или скопировать `/app/data` из запущенного контейнера:

```bash
docker cp onetimeshare:/app/data ./backup-$(date +%F)
```

## 🏛 Архитектура: поток burn-after-reading

```
Browser                FastAPI                    Disk
   │  GET /d/abc/file    │                            │
   │ ────────────────────>                            │
   │                     │  load link from DB         │
   │                     │  check password cookie     │
   │                     │  select ZIP or single stream
   │                     │ ───────────────────────────> read chunks
   │  chunk 1            │ <─                          │
   │ <────────────────────                            │
   │  chunk 2            │ <─                          │
   │ <────────────────────                            │
   │                     │                            │
   │ ╳ TCP RST            │                            │
   │ ────────────────────>                            │
   │                     │  CancelledError raised     │
   │                     │  in burn_after_stream      │
   │                     │  finally { success=False } │
   │                     │  >>> NO DELETE <<<         │
   │                     │                            │
   │                     │                            │
   │ (повторный GET)      │                            │
   │ ────────────────────>                            │
   │  chunk 1            │ <─                          │
   │ <────────────────────                            │
   │  ...                │                            │
   │  chunk N (last)     │ <─                          │
   │ <────────────────────                            │
   │  end of stream       │                            │
   │ <────────────────────                            │
   │                     │  finally { success=True }  │
   │                     │  on_success() runs:        │
   │                     │  - unlink files ───────────> os.remove()
   │                     │  - mark is_downloaded=True │
   │                     │  - commit (fresh session)  │
   │  link gone (404)     │                            │
   │ <────────────────────                            │
```

## 🚧 Известные ограничения

1. **SQLite + single worker** — для multi-worker нужен PostgreSQL (`DATABASE_URL=postgresql+asyncpg://...`)
   и вынос cleanup-таски в отдельный контейнер
2. **Rate limiting** не реализован — добавьте `slowapi` или rate-limit на reverse-proxy
3. **Глобальная квота диска** не ограничена — только per-upload лимиты
4. **`SECRET_KEY` автогенерация** — при отсутствии в `.env` генерируется случайный;
   cookies не переживают рестарт. Всегда задавайте явно.
5. **Куки без `Secure` флага** для работы за plain-HTTP reverse-proxy;
   переключите на `True` когда TLS гарантирован

## 🛠 Разработка

```bash
# Локальный запуск без Docker
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## 📄 Лицензия

MIT
