# -*- coding: utf-8 -*-
"""
snosbot.py — контроль пула сессий и сноса через телеграм-бота (pyrogram/kurigram, layer 229).

сессии добавляются командой /acc, лежат в pool.txt рядом с ботом.
запуск — на машине с доступом к DC телеграма.

зависимости: pip install kurigram tgcrypto
"""

import asyncio
import contextlib
import logging
import os
import pathlib
import random
import re
import sys
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

from pyrogram import Client, filters, enums
from pyrogram.errors import (
    FloodWait,
    RPCError,
    AuthKeyUnregistered,
    SessionRevoked,
    UserDeactivatedBan,
    UserAlreadyParticipant,
    UserNotParticipant,
    UsernameInvalid,
    UsernameNotOccupied,
    MessageNotModified,
)
from pyrogram.raw import functions as rawf, types as rawt

# ------------------------------------------------------------------ creds ---

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")

# 0 = доступ кому угодно; впиши свой tg id, чтобы бот слушал только тебя
OWNER_ID = 0

POOL_FILE = pathlib.Path(__file__).with_name("pool.txt")

# прокси ходит вся машина: TG_PROXY="socks5://user:pass@ip:port[,socks5://...]"
# случайные несколько через запятую — сессии пула разлетаются по ним по кругу
def _parse_proxy(uri: str) -> dict:
    u = urllib.parse.urlparse(uri.strip())
    if not u.hostname or not u.port:
        raise ValueError(f"bad proxy uri: {uri}")
    scheme = u.scheme.lower()
    if scheme not in ("socks4", "socks5", "http"):
        scheme = "socks5"
    return {
        "scheme": scheme,
        "hostname": u.hostname,
        "port": u.port or 1080,
        "username": u.username or None,
        "password": u.password or None,
    }


PROXIES: list[dict] = []
_raw = os.environ.get("TG_PROXY", "").strip()
if _raw:
    for part in _raw.split(","):
        part = part.strip()
        if part:
            try:
                PROXIES.append(_parse_proxy(part))
            except ValueError:
                pass

DEVICES = [
    ("PC 64bit", "Windows 10"),
    ("PC 64bit", "Windows 11"),
    ("SM-G991B", "SDK 31"),
    ("SM-A515F", "SDK 30"),
    ("iPhone", "iOS 16.5"),
    ("iPhone", "iOS 17.2"),
    ("Pixel 6", "SDK 33"),
]
LANGS = ("ru", "en")

# ----------------------------------------------------------------- тексты ---
# база — сами пункты ToS (https://telegram.org/tos):
#   1) send spam or scam users
#   2) promote violence on publicly viewable channels
#   3) post illegal pornographic content on publicly viewable channels
#   4) activities recognized as illegal in the majority of countries
#      (child abuse, illegal goods: drugs, firearms, forged documents...)
# кидается случайно по одному на выстрел, EN/RU вперемешку.

REPORT_TEXTS = {
    "spam": [
        "Spam: this account/channel sends unsolicited bulk messages to users who never asked "
        "for them. This violates the Telegram Terms of Service clause: «Use our service to send "
        "spam or scam users». Requesting a restriction of this account/channel.",
        "Незапрашиваемые массовые рассылки (спам) пользователям, которые не подписаны и не "
        "предоставляли согласия. Нарушение пункта правил Telegram: «Use our service to send "
        "spam or scam users». Прошу принять меры и ограничить нарушителя.",
        "Unsolicited advertising mailings and bulk spam. Violates the Telegram Terms of Service "
        "(https://telegram.org/tos). Requesting moderation and restriction.",
    ],
    "fraud": [
        "Scam/phishing: this account/channel obtains money or personal data from users under "
        "false pretenses (fake investments, fake giveaways, phishing links). Violates the Telegram "
        "Terms of Service clause: «Use our service to send spam or scam users». Requesting a "
        "restriction of this account/channel.",
        "Мошенничество (скам): обманом выманиваются деньги и личные данные пользователей — "
        "фейковые инвестиции, фейк-розыгрыши, фишинговые ссылки. Нарушение пункта ToS Telegram: "
        "«Use our service to send spam or scam users». Прошу ограничить нарушителя.",
    ],
    "violence": [
        "Violence: this publicly viewable channel promotes violence, threats and incitement "
        "against people. Violates the Telegram Terms of Service clause: «Promote violence on "
        "publicly viewable Telegram channels, bots, etc». Requesting a restriction of this channel.",
        "Публичный канал продвигает насилие: угрозы, списки на расправу, призывы к агрессии. "
        "Нарушение пункта ToS Telegram: «Promote violence on publicly viewable Telegram channels, "
        "bots, etc». Прошу ограничить канал.",
    ],
    "child": [
        "Child safety: this channel contains sexualized content involving minors (CSAM). Telegram "
        "ToS has zero tolerance: activities recognized as illegal in the majority of countries "
        "include child abuse. Requesting immediate removal and restriction of this channel.",
        "На канале контент с сексуализацией несовершеннолетних (CSAM). По ToS Telegram это "
        "«activities recognized as illegal in the majority of countries — child abuse», нулевая "
        "толерантность. Прошу немедленного удаления и ограничения канала.",
    ],
    "porn": [
        "Porn: this publicly viewable channel posts illegal pornographic content and unsolicited "
        "porn ads to users who are not subscribed. Violates the Telegram Terms of Service clause: "
        "«Post illegal pornographic content on publicly viewable Telegram channels, bots, etc». "
        "Requesting a restriction of this channel.",
        "В публичном канале нелегальный порноконтент и непрошеная реклама 18+ пользователям, не "
        "подписанным на канал. Нарушение пункта ToS: «Post illegal pornographic content on "
        "publicly viewable Telegram channels». Прошу ограничить.",
    ],
    "drugs": [
        "Illegal goods: this account/channel offers controlled substances (drugs) for sale with "
        "contact lists. Violates the Telegram Terms of Service clause: «Engage in activities that "
        "are recognized as illegal in the majority of countries... selling or offering illegal "
        "goods and services (drugs, firearms, forged documents)». Requesting a restriction.",
        "Канал предлагает к продаже наркотические вещества и оставляет контакты заказов. Нарушение "
        "пункта ToS Telegram: «Engage in activities that are recognized as illegal in the majority "
        "of countries — selling or offering illegal goods and services (drugs...)». Прошу ограничить.",
    ],
    "fake": [
        "Fake account/impersonation: this profile pretends to be a well-known person/official "
        "entity and misleads users (classic prelude to scam). Violates the Telegram Terms of "
        "Service clause: «Use our service to... scam users». Requesting a restriction of this account.",
        "Фейковый аккаунт: выдаёт себя за другое лицо/официальный ресурс, вводит пользователей "
        "в заблуждение. Нарушение правил Telegram в части обмана пользователей (scam). Прошу ограничить.",
    ],
    "geo": [
        "Location-based report: content of this channel is irrelevant to the reported location and "
        "is used to mislead local users. Violating Telegram's rules on misleading content. "
        "Requesting a review.",
        "Контент канала не относится к заявленной локации и дезинформирует пользователей. "
        "Прошу проверить канал согласно правилам Telegram.",
    ],
    "other": [
        "This account/channel violates the Telegram Terms of Service (https://telegram.org/tos): "
        "illegal/misleading content distributed to users. Requesting moderation and a restriction "
        "of this account/channel.",
        "Аккаунт/канал нарушает условия использования Telegram (https://telegram.org/tos) — "
        "распространяет незаконный/вводящий в заблуждение контент. Прошу проверить и ограничить.",
    ],
}

# сырые причины layer 229
RAW_REASON = {
    "spam": "InputReportReasonSpam",
    "violence": "InputReportReasonViolence",
    "child": "InputReportReasonChildAbuse",
    "porn": "InputReportReasonPornography",
    "drugs": "InputReportReasonIllegalDrugs",
    "fraud": "InputReportReasonCopyright",
    "fake": "InputReportReasonFake",
    "geo": "InputReportReasonGeoIrrelevant",
    "other": "InputReportReasonOther",
}


def raw_reason(key: str):
    cls = rawt.__dict__.get(RAW_REASON.get(key, "InputReportReasonOther"), rawt.InputReportReasonOther)
    return cls()


# ---------------------------------------------------------------- helpers ---

RE_INVITE = re.compile(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([A-Za-z0-9_-]+)")
RE_UNAME_URL = re.compile(r"(?:t\.me|telegram\.me)/@?([A-Za-z][A-Za-z0-9_]{3,31})\Z")
RE_UNAME = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{3,31})\Z")


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_target(t: str):
    t = t.strip()
    m = RE_INVITE.search(t)
    if m:
        return "invite", "https://t.me/joinchat/" + m.group(1)
    m = RE_UNAME_URL.search(t.replace("https://", "").replace("http://", ""))
    if m:
        return "username", m.group(1)
    m = RE_UNAME.fullmatch(t)
    if m:
        return "username", m.group(1)
    if re.fullmatch(r"-?\d+", t):
        return ("chatid" if t.startswith("-") else "userid"), int(t)
    return None, None


def load_pool() -> list[str]:
    if not POOL_FILE.is_file():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for line in POOL_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def save_pool(strings: list[str]) -> None:
    POOL_FILE.write_text("\n".join(strings) + ("\n" if strings else ""), encoding="utf-8")


# ------------------------------------------------------------------ engine ---

async def leave_chat_quiet(client: Client, chat_id: int) -> None:
    try:
        await client.leave_chat(chat_id)
    except (UserNotParticipant, RPCError):
        pass


async def throw_one(client: Client, peer, reason: str) -> None:
    await client.invoke(
        rawf.account.ReportPeer(
            peer=peer,
            reason=raw_reason(reason),
            message=random.choice(REPORT_TEXTS.get(reason) or REPORT_TEXTS["other"]),
        )
    )
    if reason == "spam":
        with contextlib.suppress(RPCError):
            await client.invoke(rawf.messages.ReportSpam(peer=peer))


async def report_cycle(client: Client, peer, per: int, reason: str, stats: dict, state: dict, tag: str) -> None:
    for i in range(per):
        if state["stop"].is_set():
            return
        try:
            await throw_one(client, peer, reason)
            stats["ok"] += 1
            log(f"{tag} [+] {i + 1}/{per}")
        except FloodWait as fw:
            stats["flood"] += 1
            if fw.value > 900:
                log(f"{tag} [-] flood {fw.value}s — в кулдаун")
                return
            await asyncio.sleep(min(fw.value + random.uniform(1, 4), 900))
        except RPCError as e:
            stats["errors"] += 1
            log(f"{tag} [x] rpc: {e}")
        except OSError as e:
            stats["errors"] += 1
            log(f"{tag} [x] сеть: {e}")
            return
        await asyncio.sleep(random.uniform(3.0, 7.0))


async def channel_flow(client: Client, chat_id: int, per: int, reason: str, stats, state, tag) -> None:
    peer = await client.resolve_peer(chat_id)
    await report_cycle(client, peer, per, reason, stats, state, tag)
    await leave_chat_quiet(client, chat_id)


async def user_flow(client: Client, target_id: int, per: int, reason: str, stats, state, tag) -> None:
    peer = await client.resolve_peer(target_id)
    await report_cycle(client, peer, per, reason, stats, state, tag)


async def snos_account(sess: str, idx: int, target, per: int, reason: str, stats, state) -> None:
    tag = f"acc[{idx:02d}]"
    device_model, sysver = random.choice(DEVICES)
    client = Client(
        f"snoser-mem-{idx}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=sess,
        proxy=PROXIES[idx % len(PROXIES)] if PROXIES else None,
        device_model=device_model,
        system_version=sysver,
        app_version=f"{random.randint(3, 10)}.{random.randint(0, 15)}.0",
        lang_code=random.choice(LANGS),
    )
    kind, value = target
    try:
        await client.start()
    except (AuthKeyUnregistered, SessionRevoked):
        stats["dead"] += 1
        log(f"{tag} [!] сессия мертва")
        return
    except UserDeactivatedBan:
        stats["dead"] += 1
        log(f"{tag} [!] аккаунт забанен")
        return
    except (OSError, RPCError) as e:
        stats["errors"] += 1
        log(f"{tag} [!] не подключиться: {e}")
        return

    try:
        await asyncio.sleep(random.uniform(1.0, 4.0))
        if state["stop"].is_set():
            return

        if kind == "invite":
            chat_id = None
            try:
                chat = await client.join_chat(value)
                chat_id = chat.id
            except UserAlreadyParticipant:
                async for d in client.get_dialogs(limit=200):
                    if d.chat and value in str(getattr(d.chat, "invite_link", "")):
                        chat_id = d.chat.id
                        break
            if chat_id is None:
                stats["errors"] += 1
                log(f"{tag} [x] канал по инвойсу не добыт")
                return
            await channel_flow(client, chat_id, per, reason, stats, state, tag)

        elif kind == "username":
            c = await client.get_chat(value)
            if c.type in (enums.ChatType.CHANNEL, enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                chat_id = c.id
                try:
                    joined = await client.join_chat(value)
                    chat_id = joined.id
                except UserAlreadyParticipant:
                    pass
                except RPCError as e:
                    log(f"{tag} [!] join не прошел ({e})")
                await channel_flow(client, chat_id, per, reason, stats, state, tag)
            else:
                await user_flow(client, c.id, per, reason, stats, state, tag)

        elif kind == "chatid":
            try:
                c = await client.get_chat(value)
            except RPCError:
                c = None
                async for d in client.get_dialogs(limit=200):
                    if d.chat and d.chat.id == value:
                        c = d.chat
                        break
                if c is None:
                    stats["errors"] += 1
                    log(f"{tag} [x] chat id не знаком сессии")
                    return
            if c.type in (enums.ChatType.CHANNEL, enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                await channel_flow(client, c.id, per, reason, stats, state, tag)
            else:
                await user_flow(client, c.id, per, reason, stats, state, tag)

        elif kind == "userid":
            target_id = int(value)
            try:
                u = await client.get_users(target_id)
                target_id = u.id
            except RPCError:
                found = None
                async for d in client.get_dialogs(limit=200):
                    if d.chat and d.chat.id == int(value):
                        found = d.chat.id
                        break
                if found is None:
                    stats["errors"] += 1
                    log(f"{tag} [x] user id не знаком сессии")
                    return
                target_id = found
            await user_flow(client, target_id, per, reason, stats, state, tag)

    except FloodWait:
        stats["flood"] += 1
    except (UsernameNotOccupied, UsernameInvalid):
        stats["errors"] += 1
        log(f"{tag} [x] таргет не резолвится")
    except RPCError as e:
        stats["errors"] += 1
        log(f"{tag} [x] rpc на подготовке: {e}")
    except OSError as e:
        stats["errors"] += 1
        log(f"{tag} [!] сеть: {e}")
    finally:
        with contextlib.suppress(Exception):
            await client.stop()


# ---------------------------------------------------------------- run state ---

RUN: dict = {}


def fmt_status() -> str:
    s = RUN.get("stats", {})
    done = s.get("done", 0)
    total = s.get("total", 0)
    target = RUN.get("target_str", "?")
    reason = RUN.get("reason", "?")
    st = "стоп" if RUN["stop"].is_set() else "идёт"
    return (
        f"{target} | {reason} | {st}\n"
        f"ok {s.get('ok', 0)} | flood {s.get('flood', 0)} | "
        f"err {s.get('errors', 0)} | dead {s.get('dead', 0)}\n"
        f"аккаунты: {done}/{total}"
    )


async def status_rail(chat_id: int, msg_id: int) -> None:
    app: Client = RUN["app"]
    while RUN["active"]:
        await asyncio.sleep(8)
        try:
            await app.edit_message_text(chat_id, msg_id, fmt_status())
        except MessageNotModified:
            continue
        except RPCError:
            continue


async def run_snos(app: Client, chat_id: int, target_str: str, reason: str, per: int) -> None:
    global RUN
    pool = load_pool()
    if not pool:
        await app.send_message(chat_id, "пул пуст")
        return

    kind, value = parse_target(target_str)
    if kind is None:
        await app.send_message(chat_id, "таргет не разобран")
        return

    RUN = {
        "app": app,
        "active": True,
        "stop": asyncio.Event(),
        "tasks": [],
        "stats": {"ok": 0, "flood": 0, "errors": 0, "dead": 0, "done": 0, "total": len(pool)},
        "target_str": target_str,
        "kind": kind,
        "value": value,
        "reason": reason,
        "chat_id": chat_id,
    }
    msg = await app.send_message(chat_id, fmt_status())
    rail = asyncio.create_task(status_rail(chat_id, msg.id))

    queue: asyncio.Queue = asyncio.Queue()
    for i, sess in enumerate(pool):
        queue.put_nowait((sess, i))

    async def worker() -> None:
        while True:
            sess, i = await queue.get()
            try:
                if RUN["stop"].is_set():
                    continue
                await snos_account(sess, i, (kind, value), per, reason, RUN["stats"], RUN)
            except Exception as e:
                RUN["stats"]["errors"] += 1
                log(f"acc[{i:02d}] [!] фатал: {type(e).__name__}: {e}")
            finally:
                RUN["stats"]["done"] += 1
                queue.task_done()

    w = min(8, len(pool))
    RUN["tasks"] = [asyncio.create_task(worker()) for _ in range(w)]
    await queue.join()
    for t in RUN["tasks"]:
        t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*RUN["tasks"], return_exceptions=True)

    RUN["active"] = False
    rail.cancel()
    with contextlib.suppress(asyncio.CancelledError, RPCError):
        await rail
    s = RUN["stats"]
    await app.send_message(
        chat_id,
        f"=== done ===\nok {s['ok']} | flood {s['flood']} | err {s['errors']} | dead {s['dead']}\nаккаунты: {s['done']}/{s['total']}",
    )


# ------------------------------------------------------------------ bot ui ---

app = Client(
    "snosbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    proxy=PROXIES[0] if PROXIES else None,
)


def gate(_, __, message):
    return OWNER_ID == 0 or (message.from_user and message.from_user.id == OWNER_ID)


FLTR = filters.command(["acc", "accs", "rm", "sn", "stop", "stat", "reasons"]) & filters.create(gate)


@app.on_message(FLTR)
async def dispatcher(client: Client, message):
    log(f"<- {message.from_user.id if message.from_user else '?'} {message.text}")
    parts = (message.text or "").split()
    cmd = parts[0].split("@")[0].lstrip("/").lower()
    args = parts[1:]

    if cmd == "acc":
        # строки сессий — остальной текст сообщения без первой строки-команды
        body = "\n".join((message.text or "").splitlines()[1:])
        got = []
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                got.append(line)
        pool = load_pool()
        old = set(pool)
        add = [s for s in got if s not in old]
        save_pool(pool + add)
        await message.reply(f"+{len(add)} (пул {len(pool) + len(add)})")

    elif cmd == "accs":
        await message.reply(f"пул {len(load_pool())}")

    elif cmd == "rm":
        pool = load_pool()
        if args and args[0] == "all":
            save_pool([])
            await message.reply("очищен")
            return
        try:
            i = int(args[0]) - 1
        except (IndexError, ValueError):
            await message.reply("rm <номер|all>")
            return
        if 0 <= i < len(pool):
            save_pool(pool[:i] + pool[i + 1:])
            await message.reply(f"-{i + 1} (пул {len(pool) - 1})")
        else:
            await message.reply("нет такого")

    elif cmd == "reasons":
        await message.reply(" | ".join(RAW_REASON))

    elif cmd == "sn":
        if RUN.get("active"):
            await message.reply("уже идёт")
            return
        if not args:
            await message.reply("sn <таргет> [причина] [per]")
            return
        reason = "spam"
        per = 2
        if len(args) >= 2 and args[1].lower() in RAW_REASON:
            reason = args[1].lower()
            rest = args[2:]
        else:
            rest = args[1:]
        if rest and rest[0].isdigit():
            per = max(1, min(int(rest[0]), 5))
        await message.reply("идёт")
        asyncio.create_task(run_snos(client, message.chat.id, args[0], reason, per))

    elif cmd == "stop":
        if RUN.get("active"):
            RUN["stop"].set()
            await message.reply("стоп")
        else:
            await message.reply("не идёт")

    elif cmd == "stat":
        await message.reply(fmt_status() if RUN.get("active") else "не идёт")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    log("[*] snosbot запущен")
    app.run()



if __name__ == "__main__":
    main()
