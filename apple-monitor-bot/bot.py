"""
苹果 App Store 下架监控 Telegram 机器人
- 仅监控指定 App 是否被下架（从指定商店消失）
- 下架时向 TG 频道发送通知
- 支持通过命令增删监控的 App
- 支持管理员授权其他用户使用
"""

import os
import re
import html
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# 可选：本地手动运行时从 .env 读取环境变量（用 systemd 部署时不依赖它）
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------- 配置（从环境变量读取） ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]                       # @BotFather 给的 token
ADMIN_ID = int(os.environ["ADMIN_ID"])                    # 你自己的 Telegram 数字 ID
CHANNEL_ID = os.environ["CHANNEL_ID"]                     # 频道 @用户名 或 -100 开头的数字ID
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "180"))            # 检测间隔（秒）
REMOVAL_CONFIRMATIONS = int(os.environ.get("REMOVAL_CONFIRMATIONS", "2"))  # 连续几次查不到才判定下架
COUNTRY = os.environ.get("COUNTRY", "us")                 # 商店地区，默认美区
DB_PATH = os.environ.get("DB_PATH", "monitor.db")         # SQLite 数据库文件路径

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("apple-monitor")

APP_ID_RE = re.compile(r"id(\d+)")


# ---------------- 工具函数 ----------------
def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_time(s: str):
    """把存库的时间字符串解析回 datetime（UTC）。失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def format_duration(added_at: str) -> str:
    """以录入机器人的时间为起点，计算并格式化在架时长。"""
    start = parse_time(added_at)
    if start is None:
        return "未知"
    delta = datetime.now(timezone.utc) - start
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 0:
        total_minutes = 0
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分钟")
    return "".join(parts)


def esc(s) -> str:
    return html.escape(str(s))


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS apps(
            app_id TEXT PRIMARY KEY,
            name TEXT,
            added_by INTEGER,
            added_at TEXT,
            is_available INTEGER DEFAULT 1,
            fail_count INTEGER DEFAULT 0,
            notified_removed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            role TEXT,
            added_at TEXT
        );
        """
    )
    # 确保主管理员存在
    conn.execute(
        "INSERT OR REPLACE INTO users(user_id, role, added_at) VALUES(?,?,?)",
        (ADMIN_ID, "admin", now()),
    )
    conn.commit()
    conn.close()


def is_authorized(user_id: int) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    conn = db()
    row = conn.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None and row["role"] == "admin"


def parse_app_id(text: str):
    text = text.strip()
    if text.isdigit():
        return text
    m = APP_ID_RE.search(text)
    return m.group(1) if m else None


# ---------------- 苹果接口查询 ----------------
async def lookup_apps(app_ids):
    """
    返回 dict: app_id -> 状态
      - 字符串(App名称) 表示在架
      - False 表示成功拿到响应但里面没有这个ID（确认查不到）
      - None  表示请求出错/超时（状态未知，本轮跳过）
    """
    result = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; AppMonitorBot/1.0)"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        for i in range(0, len(app_ids), 100):  # 一次最多查 100 个
            chunk = app_ids[i : i + 100]
            params = {"id": ",".join(chunk), "country": COUNTRY, "entity": "software"}
            try:
                r = await client.get("https://itunes.apple.com/lookup", params=params)
                r.raise_for_status()
                data = r.json()
                returned = {
                    str(item.get("trackId")): item.get("trackName", "")
                    for item in data.get("results", [])
                }
                for cid in chunk:
                    result[cid] = returned.get(cid, False)
            except Exception as e:
                logger.warning("查询失败（本轮跳过该批）：%s", e)
                for cid in chunk:
                    result[cid] = None
            await asyncio.sleep(1)  # 批次之间稍作间隔，温柔对待接口
    return result


# ---------------- 定时检测任务 ----------------
async def check_apps(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute("SELECT * FROM apps").fetchall()
    conn.close()
    if not rows:
        return

    status = await lookup_apps([r["app_id"] for r in rows])

    conn = db()
    for r in rows:
        aid = r["app_id"]
        st = status.get(aid)

        if st is None:
            # 网络/接口异常，状态未知，跳过避免误报
            continue

        if st is False:
            # 确认这次查不到
            new_fail = r["fail_count"] + 1
            conn.execute("UPDATE apps SET fail_count=? WHERE app_id=?", (new_fail, aid))
            if new_fail >= REMOVAL_CONFIRMATIONS and not r["notified_removed"]:
                conn.execute(
                    "UPDATE apps SET is_available=0, notified_removed=1 WHERE app_id=?",
                    (aid,),
                )
                conn.commit()
                await notify_removed(context, r)
        else:
            # 在架：重置失败计数；如果之前判定过下架，说明重新上架了
            if r["fail_count"] != 0 or r["notified_removed"] or not r["is_available"]:
                conn.execute(
                    "UPDATE apps SET fail_count=0, is_available=1, notified_removed=0 WHERE app_id=?",
                    (aid,),
                )
            if st and st != r["name"]:
                conn.execute("UPDATE apps SET name=? WHERE app_id=?", (st, aid))
    conn.commit()
    conn.close()


async def notify_removed(context: ContextTypes.DEFAULT_TYPE, row):
    name = row["name"] or row["app_id"]
    url = f"https://apps.apple.com/{COUNTRY}/app/id{row['app_id']}"
    text = (
        "<b>【App 下架提醒】</b>\n\n"
        f"名称：{esc(name)}\n"
        f"App ID：<code>{row['app_id']}</code>\n"
        f"商店：{COUNTRY.upper()}\n"
        f"链接：{url}\n"
        f"录入时间：{esc(row['added_at'])}\n"
        f"监控时长：{format_duration(row['added_at'])}\n"
        f"检测时间：{now()}"
    )
    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info("已通知下架：%s (%s)", name, row["app_id"])
    except Exception as e:
        logger.error("发送频道消息失败：%s", e)


# ---------------- 使用说明文案 ----------------
GUIDE_OVERVIEW = (
    "<b>📖 使用说明</b>\n\n"
    "本机器人用于监控指定苹果 App 是否在 App Store 被下架，"
    "一旦检测到下架会立即在频道发送提醒。\n\n"
    "点击下方按钮查看对应说明 👇"
)

GUIDE_BASIC = (
    "<b>🟢 基础使用</b>\n\n"
    "<b>1. 添加监控</b>\n"
    "发送 <code>/add App链接</code> 或 <code>/add 数字ID</code>\n"
    "例：<code>/add https://apps.apple.com/us/app/id123456789</code>\n"
    "或：<code>/add 123456789</code>\n"
    "添加后会从<b>录入这一刻</b>开始计算监控时长。\n\n"
    "<b>2. 删除监控</b>\n"
    "发送 <code>/remove App的数字ID</code>\n"
    "例：<code>/remove 123456789</code>\n\n"
    "<b>3. 查看监控列表</b>\n"
    "发送 <code>/list</code>，可看到全部 App 的在架/下架状态及监控时长。\n\n"
    "<b>4. 查看你的 ID</b>\n"
    "发送 <code>/myid</code> 获取自己的 Telegram 数字 ID。"
)

GUIDE_NOTIFY = (
    "<b>🔔 下架提醒说明</b>\n\n"
    f"• 机器人每 {CHECK_INTERVAL} 秒检测一次美区（默认）商店。\n"
    f"• 为避免网络抖动误报，需连续 {REMOVAL_CONFIRMATIONS} 次查不到才判定下架。\n"
    "• 判定下架后会自动在频道推送提醒，内容包含名称、ID、链接、录入时间与监控时长。\n"
    "• 若 App 之后重新上架，状态会自动复位，下次再下架仍会提醒。\n"
    "• 仅监控“下架”，不监控版本更新或价格变动。"
)

GUIDE_ADMIN = (
    "<b>👑 管理员功能</b>\n\n"
    "<b>授权他人使用本机器人：</b>\n"
    "1. 让对方对机器人发送 <code>/myid</code> 获取其 ID\n"
    "2. 你发送 <code>/adduser 对方ID</code> 即可授权\n\n"
    "<b>取消授权：</b>\n"
    "<code>/removeuser 对方ID</code>\n\n"
    "<b>查看已授权用户：</b>\n"
    "<code>/users</code>\n\n"
    "未授权用户无法添加或删除监控。"
)


def guide_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 基础使用", callback_data="guide_basic")],
            [InlineKeyboardButton("🔔 下架提醒说明", callback_data="guide_notify")],
            [InlineKeyboardButton("👑 管理员功能", callback_data="guide_admin")],
        ]
    )


def back_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ 返回目录", callback_data="guide_home")]]
    )


# ---------------- 命令处理 ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 欢迎使用苹果 App 下架监控机器人！\n\n"
        "发送 /help 查看可用命令，或点击下方按钮查看图文使用说明。",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📖 查看使用说明", callback_data="guide_home")]]
        ),
    )


async def cmd_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        GUIDE_OVERVIEW, parse_mode=ParseMode.HTML, reply_markup=guide_keyboard()
    )


async def on_guide_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "guide_home":
        text, kb = GUIDE_OVERVIEW, guide_keyboard()
    elif data == "guide_basic":
        text, kb = GUIDE_BASIC, back_keyboard()
    elif data == "guide_notify":
        text, kb = GUIDE_NOTIFY, back_keyboard()
    elif data == "guide_admin":
        text, kb = GUIDE_ADMIN, back_keyboard()
    else:
        return
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    base = (
        "<b>可用命令</b>\n"
        "/add &lt;App链接或数字ID&gt; - 添加监控\n"
        "/remove &lt;App ID&gt; - 删除监控\n"
        "/list - 查看监控列表\n"
        "/guide - 查看图文使用说明\n"
        "/myid - 查看你自己的 Telegram ID\n"
    )
    admin = (
        "\n<b>管理员命令</b>\n"
        "/adduser &lt;用户ID&gt; - 授权用户使用\n"
        "/removeuser &lt;用户ID&gt; - 取消授权\n"
        "/users - 查看已授权用户\n"
    )
    text = base + (admin if is_admin(uid) else "")
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📖 查看使用说明", callback_data="guide_home")]]
        ),
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"你的 Telegram ID：<code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("你没有权限，请联系管理员授权。")
        return
    if not context.args:
        await update.message.reply_text("用法：/add <App Store链接 或 纯数字ID>")
        return

    app_id = parse_app_id(" ".join(context.args))
    if not app_id:
        await update.message.reply_text("无法识别 App ID，请发送完整链接或纯数字ID。")
        return

    conn = db()
    if conn.execute("SELECT 1 FROM apps WHERE app_id=?", (app_id,)).fetchone():
        conn.close()
        await update.message.reply_text("该 App 已在监控列表中。")
        return
    conn.close()

    status = await lookup_apps([app_id])
    st = status.get(app_id)
    if st is None:
        await update.message.reply_text("查询超时，请稍后重试。")
        return
    if st is False:
        await update.message.reply_text("在该商店未找到此 App（ID 可能有误，或它已经下架）。")
        return

    name = st or app_id
    conn = db()
    conn.execute(
        "INSERT INTO apps(app_id, name, added_by, added_at) VALUES(?,?,?,?)",
        (app_id, name, uid, now()),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"已添加监控：\n{esc(name)}\nID：<code>{app_id}</code>\n"
        f"已开始计算在架时长（起点：{now()}）",
        parse_mode=ParseMode.HTML,
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("你没有权限。")
        return
    if not context.args:
        await update.message.reply_text("用法：/remove <App ID>")
        return

    app_id = parse_app_id(" ".join(context.args))
    if not app_id:
        await update.message.reply_text("无法识别 App ID。")
        return

    conn = db()
    cur = conn.execute("DELETE FROM apps WHERE app_id=?", (app_id,))
    conn.commit()
    conn.close()
    if cur.rowcount:
        await update.message.reply_text(
            f"已删除监控：<code>{app_id}</code>", parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("列表中没有该 App。")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("你没有权限。")
        return

    conn = db()
    rows = conn.execute("SELECT * FROM apps ORDER BY added_at").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("监控列表为空。")
        return

    msg = f"共监控 {len(rows)} 个 App：\n\n"
    for idx, r in enumerate(rows, 1):
        if r["is_available"]:
            flag = "在架"
            extra = f"    监控时长：{format_duration(r['added_at'])}\n"
        else:
            flag = "已下架"
            extra = ""
        line = (
            f"{idx}. [{flag}] {esc(r['name'])}\n"
            f"    ID：<code>{r['app_id']}</code>\n"
            f"{extra}"
        )
        if len(msg) + len(line) > 3500:  # 防止超过 TG 单条消息长度上限
            await update.message.reply_text(
                msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            msg = ""
        msg += line
    if msg.strip():
        await update.message.reply_text(
            msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("仅管理员可用。")
        return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("用法：/adduser <用户的数字ID>\n（让对方发送 /myid 获取）")
        return

    target = int(context.args[0])
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users(user_id, role, added_at) VALUES(?,?,?)",
        (target, "user", now()),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"已授权用户：<code>{target}</code>", parse_mode=ParseMode.HTML
    )


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("仅管理员可用。")
        return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("用法：/removeuser <用户ID>")
        return

    target = int(context.args[0])
    if target == ADMIN_ID:
        await update.message.reply_text("不能移除主管理员。")
        return

    conn = db()
    cur = conn.execute("DELETE FROM users WHERE user_id=?", (target,))
    conn.commit()
    conn.close()
    if cur.rowcount:
        await update.message.reply_text(
            f"已取消授权：<code>{target}</code>", parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("该用户不在授权列表中。")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("仅管理员可用。")
        return
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY added_at").fetchall()
    conn.close()
    lines = [f"- <code>{r['user_id']}</code>（{r['role']}）" for r in rows]
    await update.message.reply_text(
        "已授权用户：\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


# ---------------- 启动 ----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("guide", cmd_guide))
    app.add_handler(CallbackQueryHandler(on_guide_button, pattern=r"^guide_"))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))

    app.job_queue.run_repeating(check_apps, interval=CHECK_INTERVAL, first=15)

    logger.info("机器人已启动，检测间隔 %s 秒", CHECK_INTERVAL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
