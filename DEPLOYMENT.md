# Как запустить бота

Два способа: у себя на компьютере, чтобы попробовать, и на сервере, чтобы работало постоянно.

> ℹ️ **Перед запуском:** у бота должен быть включён **Secretary Mode** в [@BotFather](https://t.me/BotFather), иначе Telegram не даст подключить его к аккаунту. Как это сделать, описано в [README](README.md#3-включите-режим-секретаря).

> ⚠️ **Особенность Telegram Business API:** Telegram хранит события, пока бот их не заберёт, но не дольше **24 часов**. Если бот был выключен дольше суток, пропущенные за это время удаления и правки теряются. Поэтому для полноценной работы бот должен быть запущен постоянно.

- [Локально, чтобы попробовать](#локально-чтобы-попробовать)
- [На сервере, чтобы работало постоянно](#на-сервере-чтобы-работало-постоянно)
- [Как обновлять код на сервере](#как-обновлять-код-на-сервере)

---

## Локально, чтобы попробовать

Нужен Python 3.10 или новее и интернет. Бот будет работать, пока открыт терминал и включён компьютер: закрыл ноутбук, и удаления за это время прошли мимо. Для постоянной работы переходи к разделу про сервер.

### Windows

Скачай Python с [python.org](https://www.python.org/downloads/), при установке обязательно поставь галочку «Add Python to PATH». Дальше в PowerShell:

```powershell
git clone https://github.com/arianabagdasaryan02-pixel/tg-bot-anti-delete.git
cd tg-bot-anti-delete

py -m venv venv
venv\Scripts\python -m pip install -r requirements.txt

copy config.example.py config.py
notepad config.py   # впиши BOT_TOKEN и BOT_USERNAME
venv\Scripts\python bot.py
```

Путь перед `python` не опечатка: так вызывается Python из папки `venv`, и активировать окружение не нужно. Команда активации в Windows разная для `cmd` и PowerShell, а при ошибке срабатывает молча, поэтому надёжнее обойтись без неё.

Без `git` можно скачать ZIP: на странице репозитория «Code» → «Download ZIP» → распаковать.

### macOS / Linux

Python обычно уже стоит, проверить можно командой `python3 --version`. Если нет: на macOS `brew install python`, на Ubuntu и Debian `sudo apt install python3 python3-venv python3-pip`.

```bash
git clone https://github.com/arianabagdasaryan02-pixel/tg-bot-anti-delete.git
cd tg-bot-anti-delete

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py
nano config.py   # впиши BOT_TOKEN и BOT_USERNAME
python bot.py
```

### Чтобы не держать терминал открытым

```bash
nohup python bot.py > bot.log 2>&1 &
```

Бот продолжит работать после закрытия терминала. Остановить: `pkill -f bot.py`.

---

## На сервере, чтобы работало постоянно

### Какой сервер нужен

Подойдёт любой самый дешёвый VPS с **Ubuntu 22.04 или новее**: хватит 1 ядра, 512 МБ памяти и 10 ГБ диска. Бот расходует около 30 МБ памяти и почти не занимает места, потому что файлы у себя не хранит.

Белый IP, проброс портов и домен не нужны. Бот сам стучится в Telegram, а не принимает входящие соединения, так что достаточно обычного исходящего интернета. По той же причине вместо VPS годится любая машина, которая постоянно включена, например домашний мини-компьютер.

### Пошаговая настройка на Ubuntu

Дальше считаем, что сервер заказан и ты знаешь его IP, логин `root` и пароль.

**1. Подключись по SSH**

```bash
ssh root@ВАШ_IP
```

В Windows для этого есть встроенный `ssh` в PowerShell.

**2. Обнови систему и поставь нужное**

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git
```

**3. Заведи отдельного пользователя**

Под `root` бота держать не стоит.

```bash
adduser botuser
# задай пароль, остальное можно пропустить клавишей Enter

su - botuser
```

**4. Скачай код и настрой**

```bash
cd ~
git clone https://github.com/arianabagdasaryan02-pixel/tg-bot-anti-delete.git bot
cd bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py
nano config.py
# впиши BOT_TOKEN и BOT_USERNAME, сохрани: Ctrl+O, Enter, Ctrl+X
```

**5. Проверь, что запускается**

```bash
python bot.py
```

Если в журнале появилось «Бот запущен», всё в порядке. Останови через `Ctrl+C`.

**6. Включи автозапуск**

В проекте уже лежит готовый файл службы `avtozapusk.service`. Не пиши свой: две службы с одним токеном Telegram не разрешит, и обе копии начнут выбивать друг друга из связи.

```bash
exit   # возвращаемся из botuser под root

cp /home/botuser/bot/avtozapusk.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now avtozapusk
systemctl status avtozapusk
```

Должно быть `active (running)`. Теперь бот поднимется сам и при падении, и при перезагрузке сервера.

Внутри файла стоит `RuntimeMaxSec=1d`: раз в сутки systemd перезапустит бота. Это страховка на случай, когда процесс жив, но перестал отвечать.

Если ты поставил бота не в `/home/botuser/bot` или под другим пользователем, поправь в файле `User`, `WorkingDirectory` и `ExecStart`.

Журнал смотреть так:

```bash
journalctl -u avtozapusk -f
```

**7. Настрой ежедневную очистку**

```bash
su - botuser
crontab -e
```

Добавь строку, она запускает очистку каждую ночь в 04:00:

```
0 4 * * * cd /home/botuser/bot && /home/botuser/bot/venv/bin/python cleanup.py >> cleanup.log 2>&1
```

По умолчанию бот помнит сутки. Чтобы хранить дольше, добавь в `config.py` строку вида `DAYS_TO_KEEP = 7`. Подробнее в [README](README.md).

**Готово.** Бот работает круглосуточно, поднимается после сбоев и перезагрузок и каждую ночь чистит старые записи.

---

## Как обновлять код на сервере

```bash
su - botuser
cd ~/bot
git pull

# если менялись зависимости:
source venv/bin/activate
pip install -r requirements.txt

exit
systemctl restart avtozapusk
```
