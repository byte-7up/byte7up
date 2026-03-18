# Remnawave Webhook Handler

Лёгкий Python-сервис для обработки webhook от **Remnawave**, позволяющий:

- Переключать пользователя на резервный squad при статусах **EXPIRED**, **DISABLED**, **LIMITED**  
- Восстанавливать оригинальный squad при статусе **ACTIVE**  
- Сохранять оригинальные squad’ы пользователей между перезапусками через Docker volume  
- Полностью работать через Docker без сторонних зависимостей  

---

## 🚀 Особенности

- Чистый Python 3 — без сторонних библиотек (`requests`, `flask`, и т.д.)  
- Хранение данных пользователей в JSON (`/data/original_squads.json`)  
- Легко проксируется через **nginx**, **traefik** или любой другой reverse proxy  
- Готов для запуска на сервере или локально через Docker Compose  

---

## 📁 Структура проекта


remnawave-webhook/
├── webhook.py # основной Python скрипт webhook
├── Dockerfile # контейнер для запуска
├── docker-compose.yml # поднимает сервис с volume
├── .env.example # пример переменных окружения
└── README.md # инструкции по использованию


---

## ⚙️ Настройка

1. Клонируем репозиторий:

```bash
git clone https://github.com/YOUR_USERNAME/remnawave-webhook.git
cd remnawave-webhook

Создаём .env на основе примера:

cp .env.example .env
nano .env  # заполни свои значения
Пример .env
# Панель Remnawave API
RW_API_URL=https://panel.example.com/api
RW_API_TOKEN=ВАШ_API_TOKEN

# UUID резервного squad
BACKUP_SQUAD_UUID=backup-squad-uuid

# Порт webhook
PORT=3000

# Путь для хранения оригинальных squad
DATA_PATH=/data/original_squads.json
🐳 Запуск через Docker Compose
docker compose up -d

Контейнер поднимет Python webhook сервер

JSON с оригинальными squad’ами будет сохраняться в volume webhook_data

🔗 Настройка Webhook в Remnawave

В панели Remnawave настройте Webhook URL:

https://your-domain-or-ip:443/webhook

Если используете reverse proxy (nginx/traefik), проксируйте этот путь на порт контейнера (по умолчанию 3000)

Webhook автоматически отправляет данные пользователя в контейнер, который переключает squad

📝 Логика работы

При событии пользователя со статусом EXPIRED, DISABLED или LIMITED:

Сохраняется оригинальный squad пользователя

Пользователь переключается на резервный squad (BACKUP_SQUAD_UUID)

При событии ACTIVE:

Пользователь возвращается в оригинальный squad

Запись в JSON удаляется

⚠️ Примечания

Не забудьте смонтировать volume /data в Docker, чтобы данные о squad’ах сохранялись между перезапусками.

Скрипт полностью автономен, никаких npm / Python зависимостей не требуется.

Рекомендуется использовать HTTPS и reverse proxy для безопасного подключения вебхука.

🛠 Поддержка и развитие

Этот проект можно расширять: добавлять динамический выбор резервных серверов по стране, логирование в Telegram/Discord, использование Redis/PostgreSQL вместо JSON.

Для предложений и улучшений можно создавать Pull Requests в репозитории.
