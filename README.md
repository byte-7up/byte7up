# remnawave-switch-squads

Лёгкий Python‑сервис для обработки webhook от **Remnawave** —  
он автоматически:

- Переключает пользователя на резервный squad при статусах **EXPIRED**, **DISABLED**, **LIMITED**  
- Восстанавливает оригинальный squad при статусе **ACTIVE**  
- Сохраняет оригинальные squad’ы пользователей между перезапусками через Docker volume  
- Работает полностью в Docker без сторонних зависимостей

---

## 🔧 Установка

1. **Устанавливаем Docker**
```bash
sudo curl -fsSL https://get.docker.com | sh
```
Создаём рабочую папку
```
sudo mkdir -p /opt/remnawave-switch-squads && cd /opt/remnawave-switch-squads
```
Скачиваем файлы из репозитория
```
sudo wget -O .env https://raw.githubusercontent.com/byte-7up/byte7up/main/.env.example
```
```
sudo wget -O docker-compose.yml https://raw.githubusercontent.com/byte-7up/byte7up/main/docker-compose.yml
```

Заполняем переменные
```
sudo nano .env
```
Заполните:

RW_API_URL=https://panel.example.com/api

RW_API_TOKEN=ВАШ_API_TOKEN

BACKUP_SQUAD_UUID=uuid_резервного_squad

🐳 Запуск
```
docker compose up -d && docker compose logs -f -t
```
Контейнер запустит Python‑вебхук сервер, который будет обрабатывать события от Remnawave.

🔗 Настройка webhook в панели Remnawave

В .env панели Remnawave:

Webhook URL:
```
WEBHOOK_ENABLED=true
https://your-domain/api/v1/remnawave
```
Если стоит reverse proxy (nginx/traefik), проксируйте путь /api/v1/ на сервис с портом 3000.

Обновление:
```
cd /opt/remnawave-switch-squads && docker compose pull && docker compose down && docker compose up -d && docker compose logs -f
```
📌 Что делает сервис
Когда пользователь получает статус:

EXPIRED / DISABLED / LIMITED	Сохраняется оригинальный squad и переключается на резервный

при ACTIVE	Оригинальный squad восстанавливается

Оригинальные squad’ы хранятся в /data/original_squads.json в Docker volume.

⚠️ Важные замечания

Скрипт автономный — Python и зависимости встроены в контейнер.

Рекомендуется использовать HTTPS и reverse proxy для безопасности.
