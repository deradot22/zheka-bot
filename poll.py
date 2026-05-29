"""Локальный дев-режим: тянем апдейты через getUpdates вместо webhook.

На проде (Render) работает webhook из app.py. Локально удобнее polling:
    python poll.py
ВАЖНО: один токен — один getUpdates. Если параллельно крутится прод-webhook
на том же токене, Telegram будет отдавать апдейты то туда, то сюда.
"""
import time
import requests

from app import TG_API, handle_update, log


def main():
    # снимаем webhook, иначе getUpdates вернёт 409
    try:
        requests.post(f"{TG_API}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
    except requests.RequestException as e:
        log.warning("deleteWebhook failed: %s", e)

    offset = 0
    log.info("poll loop started")
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset, "timeout": 30, "allowed_updates": '["message"]'},
                timeout=40,
            )
            if not r.ok:
                log.warning("getUpdates http %s: %s", r.status_code, r.text)
                time.sleep(3)
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception:
                    log.exception("handle_update crashed")
        except requests.RequestException as e:
            log.warning("network: %s", e)
            time.sleep(3)
        except KeyboardInterrupt:
            log.info("bye")
            break


if __name__ == "__main__":
    main()
