import os
import requests
import time
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from openai import OpenAI

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = "-5233511234"
URL = "https://moscowfilmfestival.ru/miff48/schedule/"
INTERVAL = 5
OAI_KEY = os.environ["OAI_KEY"]

MSK = timezone(timedelta(hours=3))

oai = OpenAI(api_key=OAI_KEY)

last_check_time = None
last_check_status = None
last_change_time = None


def now_msk():
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S MSK")


def ask_gpt(page_text):
    """Use GPT to analyze the current page state in Russian."""
    try:
        trimmed = page_text[:8000]
        resp = oai.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "Ты помощник, который анализирует страницу расписания Московского Международного Кинофестиваля. Пользователь ждёт начала продажи билетов. Проанализируй текст страницы и объясни кратко по-русски: что сейчас на странице, есть ли расписание, открыта ли продажа билетов. Если появилась информация о продаже билетов — выдели это БОЛЬШИМИ БУКВАМИ."},
                {"role": "user", "content": f"Текст страницы расписания ММКФ:\n\n{trimmed}"}
            ],
            max_tokens=1000,
        )
        return resp.choices[0].message.content
    except Exception as e:
        error_msg = f"GPT error: {e}"
        print(error_msg)
        send_telegram(f"❌ <b>Ошибка GPT:</b>\n{e}")
        return None


def send_telegram(text, chat_id=CHAT_ID):
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(api_url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        print(f"Telegram send to {chat_id}: {resp.status_code}")
        if resp.status_code != 200:
            print(f"Telegram error body: {resp.text}")
        return resp
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return None


def fetch_page():
    resp = requests.get(URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def handle_test_fire(chat_id):
    """Fetch page, ask GPT if ticket sales are open, send report."""
    send_telegram("⏳ Загружаю страницу и спрашиваю GPT...", chat_id=chat_id)
    try:
        text = fetch_page()
        trimmed = text[:8000]
        resp = oai.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "Ты помощник, который анализирует страницу расписания Московского Международного Кинофестиваля (ММКФ). Пользователь ждёт начала продажи билетов. Проанализируй текст страницы и ответь по-русски: 1) Открыта ли продажа билетов? 2) Есть ли кнопки/ссылки на покупку? 3) Какие фильмы/события есть в расписании? 4) Общее состояние страницы. Если продажа билетов открыта — выдели это БОЛЬШИМИ БУКВАМИ."},
                {"role": "user", "content": f"Вот текст страницы расписания ММКФ:\n\n{trimmed}"}
            ],
            max_tokens=1000,
        )
        analysis = resp.choices[0].message.content
        msg = f"🧪 <b>Тестовый отчёт по странице ММКФ</b>\n⏰ {now_msk()}\n\n{analysis}\n\n🔗 {URL}"
        if len(msg) > 4000:
            msg = msg[:4000] + "\n...(обрезано)"
        send_telegram(msg, chat_id=chat_id)
        print(f"test_fire отправлен в {chat_id}")
    except Exception as e:
        send_telegram(f"❌ Ошибка test_fire: {e}", chat_id=chat_id)
        send_telegram(f"❌ <b>Ошибка test_fire</b> ({now_msk()}):\n{e}")
        print(f"test_fire ошибка: {e}")


def poll_commands():
    """Poll for commands from users."""
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params, timeout=35
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat = msg.get("chat", {}).get("id")
                if not chat:
                    continue
                cmd = text.strip()
                if cmd == "/check":
                    reply = "📊 <b>Статус мониторинга</b>\n\n"
                    if last_check_time:
                        reply += f"Последняя проверка: <b>{last_check_time}</b>\n"
                        reply += f"Статус: {last_check_status}\n"
                    else:
                        reply += "Ещё ни одной проверки не было.\n"
                    if last_change_time:
                        reply += f"\nПоследнее изменение: <b>{last_change_time}</b>"
                    else:
                        reply += "\nИзменений пока не обнаружено."
                    reply += f"\n\n🔗 {URL}"
                    send_telegram(reply, chat_id=chat)
                elif cmd == "/test_fire":
                    handle_test_fire(chat)
        except Exception as e:
            print(f"Ошибка polling: {e}")
            send_telegram(f"❌ <b>Ошибка polling команд</b> ({now_msk()}):\n{e}")
            time.sleep(5)


def main():
    global last_check_time, last_check_status, last_change_time

    print(f"Начинаю мониторинг: {URL}")
    send_telegram(f"🤖 Бот запущен ({now_msk()}).\nМониторю расписание ММКФ.\nИщу информацию о начале продажи билетов.\n{URL}")

    t = threading.Thread(target=poll_commands, daemon=True)
    t.start()

    prev_text = None
    prev_hash = None
    consecutive_errors = 0

    while True:
        try:
            text = fetch_page()
            consecutive_errors = 0
            h = hashlib.md5(text.encode()).hexdigest()
            now = now_msk()
            last_check_time = now

            if prev_hash is None:
                prev_hash = h
                prev_text = text
                last_check_status = f"✅ Первый снимок сохранён (hash: {h[:12]})"
                print(f"Первый снимок сохранён, hash={h[:12]}")
            elif h != prev_hash:
                print(f"Обнаружено изменение! {prev_hash[:12]} -> {h[:12]}")
                last_check_status = f"🔔 Обнаружено изменение!"
                last_change_time = now

                gpt_analysis = ask_gpt(text)

                old_lines = prev_text.splitlines()
                new_lines = text.splitlines()
                added = [l for l in new_lines if l not in old_lines]
                removed = [l for l in old_lines if l not in new_lines]

                msg = f"🔔 <b>Расписание ММКФ изменилось!</b>\n⏰ {now}\n"

                if gpt_analysis:
                    msg += f"\n<b>Анализ GPT:</b>\n{gpt_analysis}\n"

                if added:
                    msg += "\n<b>Добавлено:</b>\n" + "\n".join(added[:20])
                if removed:
                    msg += "\n\n<b>Убрано:</b>\n" + "\n".join(removed[:20])

                msg += f"\n\n🔗 {URL}"

                if len(msg) > 4000:
                    msg = msg[:4000] + "\n...(обрезано)"

                send_telegram(msg)
                prev_hash = h
                prev_text = text
            else:
                last_check_status = f"✅ Без изменений (hash: {h[:12]})"
                print(f"Без изменений, hash={h[:12]}")

        except Exception as e:
            consecutive_errors += 1
            last_check_status = f"❌ Ошибка: {e}"
            print(f"Ошибка: {e}")
            # notify on first error and then every 60 errors (~5 min at 5s interval)
            if consecutive_errors == 1 or consecutive_errors % 60 == 0:
                send_telegram(f"❌ <b>Ошибка мониторинга</b> ({now_msk()}):\n{e}\n\nОшибок подряд: {consecutive_errors}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
