"""
Steam Free Games & Points Shop Notifier -> Telegram Channel
=============================================================
این اسکریپت به‌صورت دوره‌ای موارد زیر رو چک می‌کنه:
  ۱) بازی‌هایی که «موقتاً» (نه دائمی) رایگان شدن (مثلاً تخفیف ۱۰۰٪)
  ۲) آیتم‌های Points Shop استیم که رایگان شدن
  ۳) جشنواره‌ها/رویدادهای استیم که معمولاً یه بج یا آیتم رایگان میدن
و برای هر مورد جدید، پیام به کانال تلگرام می‌فرسته.

نکته مهم: این اسکریپت فقط چیزهایی رو اعلام می‌کنه که "تازه" رایگان شدن،
نه بازی‌هایی که همیشه رایگان بودن (Free to Play دائمی).
این کار با نگه‌داشتن یه حافظه (state.json) از قیمت‌های قبلی انجام می‌شه.
"""

import json
import os
import time
import base64
import logging
import requests
import feedparser

# ---------------------------------------------------------------------------
# تنظیمات - این بخش رو باید خودت پر کنی
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "@your_channel_username")

STEAM_COUNTRY_CODE = "us"     # کشور مبنای قیمت‌گذاری (us چون همیشه در دسترسه)
CHECK_INTERVAL_SECONDS = 30 * 60   # هر ۳۰ دقیقه یک‌بار چک می‌کنه

STATE_FILE = "state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger("steam_notifier")


# ---------------------------------------------------------------------------
# مدیریت حافظه (State) - برای اینکه بفهمیم چی قبلاً دیده شده
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "game_prices": {},       # {appid: last_seen_final_price_in_cents}
        "notified_points_items": [],
        "notified_news_ids": [],
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# ارسال پیام به تلگرام
# ---------------------------------------------------------------------------
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        if not r.ok:
            log.error("ارسال پیام تلگرام شکست خورد: %s", r.text)
    except Exception as e:
        log.error("خطا در ارتباط با تلگرام: %s", e)


# ---------------------------------------------------------------------------
# بخش ۱: چک کردن بازی‌هایی که موقتاً رایگان شدن (تخفیف ۱۰۰٪)
# ---------------------------------------------------------------------------
def check_free_games(state):
    """
    از featuredcategories استفاده می‌کنیم که لیست specials رو برمی‌گردونه.
    اگه final_price == 0 ولی original_price > 0 باشه، یعنی موقتاً رایگان شده
    (نه اینکه از اول رایگان بوده).
    """
    url = "https://store.steampowered.com/api/featuredcategories"
    params = {"cc": STEAM_COUNTRY_CODE, "l": "english"}

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        log.error("خطا در گرفتن دیتای specials: %s", e)
        return

    specials = data.get("specials", {}).get("items", [])
    prices = state["game_prices"]

    for item in specials:
        appid = str(item.get("id"))
        name = item.get("name", "نامشخص")
        final_price = item.get("final_price", None)
        original_price = item.get("original_price", None)
        discount_pct = item.get("discount_percent", 0)

        if final_price is None or original_price is None:
            continue

        is_now_free = final_price == 0
        was_originally_paid = original_price > 0

        if is_now_free and was_originally_paid:
            last_price = prices.get(appid)
            if last_price != 0:
                link = f"https://store.steampowered.com/app/{appid}"
                msg = (
                    f"🎁 <b>بازی رایگان شد!</b>\n\n"
                    f"🎮 {name}\n"
                    f"💯 تخفیف: {discount_pct}٪\n"
                    f"🔗 {link}"
                )
                send_telegram_message(msg)
                log.info("اعلام شد: %s رایگان شد", name)

        prices[appid] = final_price

    state["game_prices"] = prices


# ---------------------------------------------------------------------------
# انکودر و دیکودر دستی پروتوباف (بدون نیاز به کتابخونه خارجی)
# ---------------------------------------------------------------------------
def _encode_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _encode_tag(field_num, wire_type):
    return _encode_varint((field_num << 3) | wire_type)


def _encode_varint_field(field_num, value):
    return _encode_tag(field_num, 0) + _encode_varint(value)


def _encode_string_field(field_num, value):
    b = value.encode("utf-8")
    return _encode_tag(field_num, 2) + _encode_varint(len(b)) + b


def _encode_bytes_field(field_num, value_bytes):
    return _encode_tag(field_num, 2) + _encode_varint(len(value_bytes)) + value_bytes


def _read_varint(data, pos):
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _parse_message(data):
    result = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            val, pos = _read_varint(data, pos)
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            val = data[pos:pos + length]
            pos += length
        elif wire_type == 1:
            val = data[pos:pos + 8]
            pos += 8
        elif wire_type == 5:
            val = data[pos:pos + 4]
            pos += 4
        else:
            break
        result.setdefault(field_num, []).append(val)
    return result


def _get_int(fields, num, default=0):
    return fields[num][0] if num in fields else default


def _get_str(fields, num, default=""):
    if num in fields:
        v = fields[num][0]
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8")
            except Exception:
                return default
    return default


def _build_query_reward_items_request(appid, count=100, cursor="", language="english"):
    parts = _encode_varint_field(1, appid)
    parts += _encode_string_field(4, language)
    parts += _encode_varint_field(5, count)
    if cursor:
        parts += _encode_string_field(6, cursor)
    return parts


def _build_batched_request(single_requests):
    out = b""
    for req_bytes in single_requests:
        out += _encode_bytes_field(1, req_bytes)
    return out


def _call_loyalty_api(method, request_bytes):
    encoded = base64.b64encode(request_bytes).decode("ascii")
    url = f"https://api.steampowered.com/ILoyaltyRewardsService/{method}/v1"
    params = {
        "origin": "https://store.steampowered.com",
        "input_protobuf_encoded": encoded,
    }
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://store.steampowered.com",
        "referer": "https://store.steampowered.com/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    }
    resp = requests.get(url, params=params, headers=headers, timeout=25)
    resp.raise_for_status()
    return resp.content


def _parse_eligible_apps(raw):
    f = _parse_message(raw)
    apps = []
    for app_bytes in f.get(1, []):
        af = _parse_message(app_bytes)
        apps.append({
            "appid": _get_int(af, 1),
            "has_items": bool(_get_int(af, 2, 0)),
        })
    return apps


def _parse_definition(raw):
    f = _parse_message(raw)
    return {
        "appid": _get_int(f, 1),
        "defid": _get_int(f, 2),
        "community_item_class": _get_int(f, 4),
        "points_cost": _get_int(f, 6),
        "time_start": _get_int(f, 7),
        "time_end": _get_int(f, 8),
        "name": _get_str(f, 11),
    }


def _parse_batched_response(raw):
    f = _parse_message(raw)
    results = []
    for resp_bytes in f.get(1, []):
        rf = _parse_message(resp_bytes)
        defs = []
        next_cursor = ""
        if 2 in rf:
            inner = _parse_message(rf[2][0])
            next_cursor = _get_str(inner, 4, "")
            for def_bytes in inner.get(1, []):
                defs.append(_parse_definition(def_bytes))
        results.append((defs, next_cursor))
    return results


# ---------------------------------------------------------------------------
# بخش ۲: چک کردن آیتم‌های Points Shop که رایگان شدن
# ---------------------------------------------------------------------------
POINTS_SHOP_MAX_PAGES_PER_APP = 5

def check_points_shop_items(state):
    try:
        apps_raw = _call_loyalty_api("GetEligibleApps", b"")
        apps = _parse_eligible_apps(apps_raw)
    except Exception as e:
        log.error("خطا در گرفتن لیست بازی‌های دارای پوینت‌شاپ: %s", e)
        return

    eligible_appids = [a["appid"] for a in apps if a.get("has_items")]
    log.info("تعداد بازی‌های دارای پوینت‌شاپ: %d", len(eligible_appids))

    all_items = {}
    for appid in eligible_appids:
        cursor = ""
        for _ in range(POINTS_SHOP_MAX_PAGES_PER_APP):
            req = _build_query_reward_items_request(appid, count=100, cursor=cursor)
            batch_req = _build_batched_request([req])
            try:
                raw = _call_loyalty_api("BatchedQueryRewardItems", batch_req)
            except Exception as e:
                log.error("خطا در گرفتن آیتم‌های appid=%s: %s", appid, e)
                break

            results = _parse_batched_response(raw)
            if not results:
                break
            defs, next_cursor = results[0]
            for d in defs:
                all_items[d["defid"]] = d

            if not next_cursor or not defs:
                break
            cursor = next_cursor

    prev_prices = state.get("points_item_prices", {})
    is_first_run = len(prev_prices) == 0
    known_time_starts = set(state.get("points_known_time_starts", []))
    new_time_starts = set()

    for defid, item in all_items.items():
        defid_str = str(defid)
        price = item["points_cost"]
        prev_price = prev_prices.get(defid_str)

        if not is_first_run and price == 0 and prev_price not in (None, 0):
            link = "https://store.steampowered.com/points/shop/"
            msg = (
                f"🎁 <b>یه آیتم پوینت‌شاپ رایگان شد!</b>\n\n"
                f"🏷️ {item['name']}\n"
                f"🔗 {link}"
            )
            send_telegram_message(msg)
            log.info("اعلام شد: آیتم %s رایگان شد", item["name"])

        prev_prices[defid_str] = price
        if item["time_start"]:
            new_time_starts.add(item["time_start"])

    if not is_first_run:
        brand_new_starts = new_time_starts - known_time_starts
        for ts in brand_new_starts:
            names = [it["name"] for it in all_items.values() if it["time_start"] == ts]
            if len(names) >= 2:
                sample = "، ".join(names[:5])
                msg = (
                    f"🏆 <b>یه کالکشن/رویداد جدید به پوینت‌شاپ اضافه شد!</b>\n\n"
                    f"🏷️ نمونه آیتم‌ها: {sample}\n"
                    f"🔗 https://store.steampowered.com/points/shop/"
                )
                send_telegram_message(msg)
                log.info("اعلام شد: کالکشن جدید (%d آیتم)", len(names))

    state["points_item_prices"] = prev_prices
    state["points_known_time_starts"] = list(known_time_starts | new_time_starts)

    if is_first_run:
        log.info("اولین اجرا: %d آیتم به‌عنوان مرجع ذخیره شد (بدون اعلام)", len(all_items))


# ---------------------------------------------------------------------------
# بخش ۳: چک کردن جشنواره‌ها/رویدادهای استیم
# ---------------------------------------------------------------------------
FESTIVAL_KEYWORDS = [
    "festival", "sale", "event", "free", "giveaway",
    "جشنواره", "رویداد", "رایگان",
]

def check_steam_festivals(state):
    feed_url = "https://store.steampowered.com/feeds/news.xml"

    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        log.error("خطا در خوندن فید اخبار استیم: %s", e)
        return

    notified = set(state["notified_news_ids"])

    for entry in feed.entries:
        entry_id = entry.get("id") or entry.get("link")
        title = entry.get("title", "")
        title_lower = title.lower()

        if entry_id in notified:
            continue

        if any(k in title_lower for k in FESTIVAL_KEYWORDS):
            link = entry.get("link", "")
            msg = (
                f"🏆 <b>یه رویداد/جشنواره جدید در استیم!</b>\n\n"
                f"📰 {title}\n"
                f"🔗 {link}\n\n"
                f"(معمولاً تو این رویدادها بج یا آیتم رایگان میدن، برو چک کن!)"
            )
            send_telegram_message(msg)
            log.info("اعلام جشنواره: %s", title)

        notified.add(entry_id)

    state["notified_news_ids"] = list(notified)


# ---------------------------------------------------------------------------
# اجرای اصلی
# ---------------------------------------------------------------------------
def main():
    log.info("شروع بررسی...")
    state = load_state()

    try:
        check_free_games(state)
        check_points_shop_items(state)
        check_steam_festivals(state)
    except Exception as e:
        log.error("خطای غیرمنتظره: %s", e)
    finally:
        save_state(state)

    log.info("بررسی تمام شد.")


if __name__ == "__main__":
    main()
