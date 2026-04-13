import os
import requests
import time
import hashlib
import threading
from datetime import datetime
from bs4 import BeautifulSoup
from openai import OpenAI

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = "5233511234"
URL = "https://moscowfilmfestival.ru/miff48/schedule/"
INTERVAL = 5
OAI_KEY = os.environ["OAI_KEY"]

oai = OpenAI(api_key=OAI_KEY)

last_check_time = None
last_check_status = None
last_change_time = None


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
        print(f"GPT error: {e}")
        return None


def send_telegram(text, chat_id=CHAT_ID):
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(api_url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    print(f"Telegram send to {chat_id}: {resp.status_code}")
    return resp


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
        msg = f"🧪 <b>Тестовый отчёт по странице ММКФ</b>\n\n{analysis}\n\n🔗 {URL}"
        if len(msg) > 4000:
            msg = msg[:4000] + "\n...(обрезано)"
        send_telegram(msg, chat_id=chat_id)
        print(f"test_fire отправлен в {chat_id}")
    except Exception as e:
        send_telegram(f"❌ Ошибка test_fire: {e}", chat_id=chat_id)
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
            time.sleep(5)


def main():
    global last_check_time, last_check_status, last_change_time

    print(f"Начинаю мониторинг: {URL}")
    send_telegram(f"🤖 Бот запущен. Мониторю расписание ММКФ.\nИщу информацию о начале продажи билетов.\n{URL}")

    t = threading.Thread(target=poll_commands, daemon=True)
    t.start()

    prev_text = None
    prev_hash = None

    while True:
        try:
            text = fetch_page()
            h = hashlib.md5(text.encode()).hexdigest()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

                msg = "🔔 <b>Расписание ММКФ изменилось!</b>\n"

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
            last_check_status = f"❌ Ошибка: {e}"
            print(f"Ошибка: {e}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
