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
INTERVAL = 5
OAI_KEY = os.environ["OAI_KEY"]

TARGETS = [
    {
        "name": "ММКФ расписание",
        "url": "https://moscowfilmfestival.ru/miff48/schedule/",
        "context": "Московский Международный Кинофестиваль (ММКФ). Пользователь ждёт начала продажи билетов.",
    },
    {
        "name": "Каро 10 — 19 апреля 2026",
        "url": "https://karofilm.ru/cinema/10?date=2026-04-19",
        "context": "Кинотеатр Каро 10, расписание на 19 апреля 2026. Пользователь ждёт появления сеансов ММКФ и начала продажи билетов.",
    },
]

MSK = timezone(timedelta(hours=3))

oai = OpenAI(api_key=OAI_KEY)

# state per target (keyed by url)
state = {
    t["url"]: {
        "prev_text": None,
        "prev_hash": None,
        "last_check_time": None,
        "last_check_status": None,
        "last_change_time": None,
        "consecutive_errors": 0,
    }
    for t in TARGETS
}


def now_msk():
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S MSK")


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


def ask_gpt(page_text, target):
    """Use GPT to analyze the current page state in Russian."""
    try:
        trimmed = page_text[:8000]
        system = (
            f"Ты помощник, который анализирует страницу: {target['name']}. "
            f"Контекст: {target['context']} "
            f"Проанализируй текст страницы и объясни кратко по-русски: "
            f"что сейчас на странице, есть ли расписание, открыта ли продажа билетов. "
            f"Если появилась информация о продаже билетов — выдели это БОЛЬШИМИ БУКВАМИ."
        )
        resp = oai.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Текст страницы ({target['name']}):\n\n{trimmed}"}
            ],
            max_tokens=1000,
        )
        return resp.choices[0].message.content
    except Exception as e:
        print(f"GPT error: {e}")
        send_telegram(f"❌ <b>Ошибка GPT</b> ({target['name']}):\n{e}")
        return None


def fetch_page(url):
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def handle_test_fire(chat_id):
    """Fetch all pages, ask GPT for a report, send to chat."""
    send_telegram("⏳ Загружаю страницы и спрашиваю GPT...", chat_id=chat_id)
    for target in TARGETS:
        try:
            text = fetch_page(target["url"])
            analysis = ask_gpt(text, target)
            msg = f"🧪 <b>Тестовый отчёт: {target['name']}</b>\n⏰ {now_msk()}\n\n{analysis}\n\n🔗 {target['url']}"
            if len(msg) > 4000:
                msg = msg[:4000] + "\n...(обрезано)"
            send_telegram(msg, chat_id=chat_id)
        except Exception as e:
            send_telegram(f"❌ Ошибка test_fire ({target['name']}): {e}", chat_id=chat_id)
            print(f"test_fire ошибка {target['name']}: {e}")


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
                cmd = text.strip().split("@")[0]  # strip @botname suffix in groups
                if cmd == "/check":
                    reply = f"📊 <b>Статус мониторинга</b>\n⏰ {now_msk()}\n"
                    for target in TARGETS:
                        s = state[target["url"]]
                        reply += f"\n<b>{target['name']}</b>\n"
                        if s["last_check_time"]:
                            reply += f"Проверка: {s['last_check_time']}\n"
                            reply += f"Статус: {s['last_check_status']}\n"
                        else:
                            reply += "Ещё не проверялось.\n"
                        if s["last_change_time"]:
                            reply += f"Последнее изменение: {s['last_change_time']}\n"
                        else:
                            reply += "Изменений пока нет.\n"
                        reply += f"🔗 {target['url']}\n"
                    send_telegram(reply, chat_id=chat)
                elif cmd == "/test_fire":
                    handle_test_fire(chat)
        except Exception as e:
            print(f"Ошибка polling: {e}")
            send_telegram(f"❌ <b>Ошибка polling команд</b> ({now_msk()}):\n{e}")
            time.sleep(5)


def monitor_target(target):
    """Monitor one target in a loop."""
    s = state[target["url"]]
    print(f"Начинаю мониторинг: {target['name']} -> {target['url']}")

    while True:
        try:
            text = fetch_page(target["url"])
            s["consecutive_errors"] = 0
            h = hashlib.md5(text.encode()).hexdigest()
            now = now_msk()
            s["last_check_time"] = now

            if s["prev_hash"] is None:
                s["prev_hash"] = h
                s["prev_text"] = text
                s["last_check_status"] = f"✅ Первый снимок (hash: {h[:12]})"
                print(f"[{target['name']}] Первый снимок, hash={h[:12]}")
            elif h != s["prev_hash"]:
                print(f"[{target['name']}] Изменение! {s['prev_hash'][:12]} -> {h[:12]}")
                s["last_check_status"] = "🔔 Обнаружено изменение!"
                s["last_change_time"] = now

                gpt_analysis = ask_gpt(text, target)

                old_lines = s["prev_text"].splitlines()
                new_lines = text.splitlines()
                added = [l for l in new_lines if l not in old_lines]
                removed = [l for l in old_lines if l not in new_lines]

                msg = f"🔔 <b>Изменилось: {target['name']}</b>\n⏰ {now}\n"
                if gpt_analysis:
                    msg += f"\n<b>Анализ GPT:</b>\n{gpt_analysis}\n"
                if added:
                    msg += "\n<b>Добавлено:</b>\n" + "\n".join(added[:20])
                if removed:
                    msg += "\n\n<b>Убрано:</b>\n" + "\n".join(removed[:20])
                msg += f"\n\n🔗 {target['url']}"

                if len(msg) > 4000:
                    msg = msg[:4000] + "\n...(обрезано)"

                send_telegram(msg)
                s["prev_hash"] = h
                s["prev_text"] = text
            else:
                s["last_check_status"] = f"✅ Без изменений (hash: {h[:12]})"
                print(f"[{target['name']}] Без изменений, hash={h[:12]}")

        except Exception as e:
            s["consecutive_errors"] += 1
            s["last_check_status"] = f"❌ Ошибка: {e}"
            print(f"[{target['name']}] Ошибка: {e}")
            if s["consecutive_errors"] == 1 or s["consecutive_errors"] % 60 == 0:
                send_telegram(
                    f"❌ <b>Ошибка мониторинга</b> ({target['name']}, {now_msk()}):\n{e}\n\n"
                    f"Ошибок подряд: {s['consecutive_errors']}"
                )

        time.sleep(INTERVAL)


def main():
    urls_list = "\n".join(f"• {t['name']}: {t['url']}" for t in TARGETS)
    print(f"Начинаю мониторинг {len(TARGETS)} страниц")
    send_telegram(f"🤖 Бот запущен ({now_msk()}).\nМониторю:\n{urls_list}")

    # start command polling
    threading.Thread(target=poll_commands, daemon=True).start()

    # start one monitor thread per target, main thread waits on the first
    threads = []
    for target in TARGETS[1:]:
        t = threading.Thread(target=monitor_target, args=(target,), daemon=True)
        t.start()
        threads.append(t)

    # run first target on main thread (blocks forever)
    monitor_target(TARGETS[0])


if __name__ == "__main__":
    main()
