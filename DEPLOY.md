# Деплой NewsBrief

## Railway (рекомендуется — бесплатный тир)

1. Зайти на https://railway.app → New Project → Deploy from GitHub
2. Подключить репозиторий с папкой `news-landing/`
3. Railway автоматически определит Python и запустит `app.py`
4. Добавить переменную окружения:
   - `DEEPSEEK_API_KEY` = ваш ключ API DeepSeek
5. Готово — Railway даст публичный URL

## Render (альтернатива)

1. https://render.com → New Web Service
2. Root Directory: `news-landing`
3. Build Command: *оставить пустым*
4. Start Command: `python3 app.py`
5. Environment variable: `DEEPSEEK_API_KEY`

## Локальный сервер

```bash
cd news-landing
DEEPSEEK_API_KEY=sk-... python3 app.py
# Открыть http://localhost:8080
```

## Структура проекта

```
news-landing/
├── app.py            # единый сервер: API + статика
├── index.html        # фронтенд
├── requirements.txt  # зависимости (только стандартная библиотека)
├── proxy.py          # старый прокси (для локальной разработки)
└── favorites.db      # SQLite база избранного (создаётся автоматически)
```

## API эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | index.html |
| GET | `/api/news` | 10 статей с эмоциями + is_favorited |
| GET | `/api/favorites` | список избранного |
| POST | `/api/favorite` | добавить в избранное `{link: "..."}` |
| DELETE | `/api/favorite` | убрать из избранного `{link: "..."}` |

## Примечания

- База данных — SQLite (файл `favorites.db`). На Railway/Render сохраняется между деплоями если использовать том (volume).
- Без `DEEPSEEK_API_KEY` эмоции не размечаются (везде `—`).
- RSS обновляется каждые 5 минут (кеш).
