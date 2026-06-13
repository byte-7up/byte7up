# remnawave-switch-squads

Лёгкий Python‑сервис для обработки webhook от **Remnawave** —  
он автоматически:

- Переключает пользователя на резервный squad при статусах **EXPIRED**, **LIMITED**  
- Поддерживает несколько резервных internal squads и опциональный external squad
- Временно переводит пользователя в **ACTIVE** на несколько дней 
- Временно даёт ограничение трафика
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
sudo wget -O docker-compose.yml https://raw.githubusercontent.com/byte-7up/byte7up/main/docker-compose.yml
```

Заполняем переменные
```bash
sudo nano .env
```

Заполните:

```env
RW_API_URL=https://panel.example.com/api
RW_API_TOKEN=ВАШ_API_TOKEN
WEBHOOK_SECRET_HEADER=aabbccddeeff00112233445566778899
BACKUP_SQUAD_UUID=uuid_резервного_squad
# можно несколько: BACKUP_SQUAD_UUID=uuid1,uuid2
BACKUP_EXTERNAL_SQUAD_UUID=
TEMP_ACTIVE_DAYS=3
TEMP_ACTIVE_TRAFFIC_LIMIT_MB=300
WEBHOOK_PATH=/api/v1/remnawave
WEBHOOK_MAX_AGE_SECONDS=300
MAX_WEBHOOK_BODY_BYTES=1048576
PORT=3000
```

🐳 Запуск
```bash
docker compose up -d && docker compose logs -f -t
```

Контейнер запустит Python‑вебхук сервер, который будет обрабатывать события от Remnawave.

🔗 Настройка webhook в панели Remnawave

В .env панели Remnawave:

Webhook URL:
```text
https://your-domain/api/v1/remnawave
```

`WEBHOOK_SECRET_HEADER` в `.env` этого сервиса должен совпадать с `WEBHOOK_SECRET_HEADER`
из `.env` панели Remnawave. Без корректной подписи `X-Remnawave-Signature` сервис
вернёт `401 Unauthorized` и не будет менять пользователя.

Если стоит reverse proxy (nginx/traefik), проксируйте путь `/api/v1/` на сервис с портом `3000`.

Обновление:
```
cd /opt/remnawave-switch-squads && docker compose pull && docker compose down && docker compose up -d && docker compose logs -f
```
📌 Что делает сервис

Когда пользователь получает статус:

`EXPIRED / LIMITED`  
Сохраняется original squad, пользователь переключается на резервный squad и временно переводится в `ACTIVE`

При покупке / продлении / изменении подписки  
Оригинальный squad восстанавливается

Если пользователь ничего не купил, он остаётся на резервном squad.

Состояние пользователей хранится в Docker volume, поэтому переживает перезапуски.

⚠️ Важные замечания

Скрипт автономный — Python и зависимости встроены в контейнер.

Рекомендуется использовать HTTPS и reverse proxy для безопасности.
