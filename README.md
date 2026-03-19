# remnawave-switch-squads

Лёгкий Python‑сервис для обработки webhook от **Remnawave** —  
он автоматически:

- Переключает пользователя на резервный squad при статусах **EXPIRED**, **DISABLED**, **LIMITED**
- Временно переводит пользователя в **ACTIVE** на несколько дней
- Возвращает оригинальный squad после покупки / продления / изменения подписки
- Сохраняет состояние пользователей между перезапусками через Docker volume
- Работает полностью в Docker без сторонних зависимостей

---

## 🔧 Установка

1. **Устанавливаем Docker**
```bash
sudo curl -fsSL https://get.docker.com | sh
```

Создаём рабочую папку
```bash
sudo mkdir -p /opt/remnawave-switch-squads && cd /opt/remnawave-switch-squads
```

Скачиваем файлы из репозитория
```bash
sudo wget -O .env https://raw.githubusercontent.com/byte-7up/byte7up/main/.env.example
```
```
sudo wget -O docker-compose.yml https://raw.githubusercontent.com/byte-7up/byte7up/main/docker-compose.yml
sudo wget -O webhook.py https://raw.githubusercontent.com/byte-7up/byte7up/main/webhook.py
sudo wget -O Dockerfile https://raw.githubusercontent.com/byte-7up/byte7up/main/Dockerfile
```

Заполняем переменные
```bash
sudo nano .env
```

Заполните:

```env
RW_API_URL=https://panel.example.com/api

RW_API_TOKEN=ВАШ_API_TOKEN

BACKUP_SQUAD_UUID=uuid_резервного_squad
TEMP_ACTIVE_DAYS=3
WEBHOOK_PATH=/api/v1/remnawave
PORT=3000
```

🐳 Запуск
```bash
docker compose up -d --build && docker compose logs -f -t
```

Контейнер запустит Python‑вебхук сервер, который будет обрабатывать события от Remnawave.

🔗 Настройка webhook в панели Remnawave

В .env панели Remnawave:

Webhook URL:
```text
https://your-domain/api/v1/remnawave
```

Если стоит reverse proxy (nginx/traefik), проксируйте путь `/api/v1/` на сервис с портом `3000`.

Обновление:
```bash
cd /opt/remnawave-switch-squads && docker compose up -d --build && docker compose logs -f
```

Обновление:
```
cd /opt/remnawave-switch-squads && docker compose pull && docker compose down && docker compose up -d && docker compose logs -f
```
📌 Что делает сервис

Когда пользователь получает статус:

`EXPIRED / DISABLED / LIMITED`  
Сохраняется original squad, пользователь переключается на резервный squad и временно переводится в `ACTIVE`

При покупке / продлении / изменении подписки  
Оригинальный squad восстанавливается

Если пользователь ничего не купил, он остаётся на резервном squad.

Состояние пользователей хранится в Docker volume, поэтому переживает перезапуски.

⚠️ Важные замечания

Скрипт автономный — Python и зависимости встроены в контейнер.

Рекомендуется использовать HTTPS и reverse proxy для безопасности.
