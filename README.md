# Remnawave Webhook Handler

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

Создаём рабочую папку

sudo mkdir -p /opt/remnawave-webhook && cd /opt/remnawave-webhook

Скачиваем файлы из репозитория

sudo wget -O .env https://raw.githubusercontent.com/byte-7up/byte7up/main/.env.example
sudo wget -O docker-compose.yml https://raw.githubusercontent.com/byte-7up/byte7up/main/docker-compose.yml
sudo wget -O webhook.py https://raw.githubusercontent.com/byte-7up/byte7up/main/webhook.py
sudo wget -O Dockerfile https://raw.githubusercontent.com/byte-7up/byte7up/main/Dockerfile

Заполняем переменные

sudo nano .env

Заполните:

RW_API_URL=https://panel.example.com/api
RW_API_TOKEN=ВАШ_API_TOKEN
BACKUP_SQUAD_UUID=uuid_резервного_squad
PORT=3000
DATA_PATH=/data/original_squads.json
🐳 Запуск
docker compose up -d

Контейнер запустит Python‑вебхук сервер, который будет обрабатывать события от Remnawave.

🔗 Настройка webhook в панели Remnawave

В панели Remnawave:

Webhook URL:
https://your-domain/webhook

Если стоит reverse proxy (nginx/traefik), проксируйте путь /webhook на сервис с портом 3000.

📌 Что делает сервис
Когда пользователь получает статус:
Статус	Действие
EXPIRED / DISABLED / LIMITED	Сохраняется оригинальный squad и переключается на резервный
ACTIVE	Оригинальный squad восстанавливается

Оригинальные squad’ы хранятся в /data/original_squads.json в Docker volume.

⚠️ Важные замечания

Не забудьте смонтировать volume /data, чтобы данные о squad’ах сохранялись между перезапусками.

Скрипт автономный — Python и зависимости встроены в контейнер.

Рекомендуется использовать HTTPS и reverse proxy для безопасности.
