"""
Telegram Reminder Bot
---------------------
A personal reminder bot you talk to in natural language. It nags you
on the schedule you set and only stops when you confirm the task is done.

Add a task: just send a message like
    "meeting with John tomorrow 5pm"
    "submit tax forms friday morning"
    "call mom in 2 days evening"
The bot asks you for a reminder schedule (e.g. "1d 1h 0m"), saves
it, and pings you on those offsets before the due time. Each reminder
has Done / Snooze / Delete buttons.
"""

import os
import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("reminder-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN env var. Get one from @BotFather.")

# Default timezone for parsing relative dates ("tomorrow evening").
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Dubai")
TZ = ZoneInfo(TIMEZONE)

# Where to keep the SQLite db. On Railway/Render with a persistent disk you
# can mount a volume here. Locally it just lives next to bot.py.
DB_PATH = os.environ.get("DB_PATH", "reminders.db")
DB_URL = f"sqlite:///{DB_PATH}"

# --------------------------------------------------------------------------- #
# Database models                                                             #
# --------------------------------------------------------------------------- #
Base = declarative_base()
engine = create_engine(DB_URL, future=True)
Session = sessionmaker(bind=engine, future=True)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer, nullable=False, index=True)
    title = Column(Text, nullable=False)
    due_at = Column(DateTime(timezone=True), nullable=False)
    # Comma-separated minutes-before-due, e.g. "1440,60,0"
    reminder_offsets = Column(String, nullable=False, default="1440,60,0")
    done = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(TZ))
    reminders = relationship(
        "Reminder", back_populates="task", cascade="all, delete-orphan"
    )


class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"))
    fire_at = Column(DateTime(timezone=True), nullable=False)
    sent = Column(Boolean, default=False, nullable=False)
    task = relationship("Task", back_populates="reminders")


class Draft(Base):
    """A task awaiting its reminder schedule. Persists across bot restarts."""
    __tablename__ = "drafts"
    chat_id = Column(Integer, primary_key=True)  # one draft per chat at a time
    title = Column(Text, nullable=False)
    due_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(TZ))


Base.metadata.create_all(engine)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
_DATE_SETTINGS = {
    "TIMEZONE": TIMEZONE,
    "RETURN_AS_TIMEZONE_AWARE": True,
    "PREFER_DATES_FROM": "future",
}


_REMIND_PREFIX = re.compile(
    r"^\s*(?:please\s+)?remind\s+me\s+(?:to|about|at|on|that|for)?\s*",
    re.IGNORECASE,
)


def parse_natural_date(text: str):
    """
    Scan free-form text for a date phrase and return (datetime, matched_text).
    Returns (None, None) if no date phrase is found.
    """
    # Strip leading "remind me to/about/..." so the parser sees just the content.
    cleaned = _REMIND_PREFIX.sub("", text, count=1).strip()
    if not cleaned:
        cleaned = text

    results = search_dates(cleaned, languages=["en"], settings=_DATE_SETTINGS)
    if not results:
        dt = dateparser.parse(cleaned, settings=_DATE_SETTINGS)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt, cleaned if dt else None

    # Prefer the longest matched phrase (most specific).
    matched_text, dt = max(results, key=lambda r: len(r[0]))
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt, matched_text


_ENGLISH_NUMS = {
    "a": "1", "an": "1", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
}
_ENGLISH_NUM_PAT = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _ENGLISH_NUMS) + r")\b", re.IGNORECASE
)


def _normalize_offset_text(text: str) -> str:
    """Pre-process schedule input: turn 'an hour' -> '1 hour', etc."""
    text = text.lower().strip()
    # Strip flavor words that don't change meaning.
    text = re.sub(r"\b(before|prior|ahead|ago|earlier|in advance)\b", "", text)
    # Common phrases.
    text = re.sub(r"\bhalf\s+an?\s+hour\b", "30 minutes", text)
    text = re.sub(r"\bquarter\s+(?:of\s+)?an?\s+hour\b", "15 minutes", text)
    # Word-to-digit substitution.
    text = _ENGLISH_NUM_PAT.sub(lambda m: _ENGLISH_NUMS[m.group(0).lower()], text)
    return re.sub(r"\s+", " ", text).strip()


def parse_offsets(text: str) -> list:
    """
    Parse a reminder schedule into a sorted list of minutes-before-due.

    Returns None if nothing parseable was found, [1440, 60, 0] for
    "default"/empty, or a sorted-descending list of minutes otherwise.
    """
    text = _normalize_offset_text(text)
    if not text or text == "default":
        return [1440, 60, 0]

    out = set()
    UNIT_RE = re.compile(
        r"^(\d+)\s*(d|day|days|h|hr|hour|hours|m|min|mins|minute|minutes)$"
    )

    def _emit(tok: str):
        tok = tok.strip()
        if not tok:
            return
        if tok in {"0", "now", "due"}:
            out.add(0)
            return
        if tok.isdigit():
            out.add(int(tok))
            return
        # Mixed-unit token like "2h30m" or "1d12h": sum the parts.
        total = 0
        for num, unit in re.findall(
            r"(\d+)\s*(d|day|days|h|hr|hour|hours|m|min|mins|minute|minutes)",
            tok,
        ):
            n = int(num)
            if unit.startswith("d"):
                total += n * 1440
            elif unit.startswith("h"):
                total += n * 60
            else:
                total += n
        if total > 0:
            out.add(total)

    for chunk in re.split(r"[,/]| and ", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk in {"at time", "at the time"}:
            out.add(0)
            continue
        # Split each chunk on whitespace so "1d 1h 0" becomes three offsets.
        # But keep "2 hours" / "30 min" together by re-joining number+unit pairs.
        tokens = chunk.split()
        i = 0
        while i < len(tokens):
            t = tokens[i]
            # If a bare number is followed by a unit word, merge them.
            if (t.isdigit() and i + 1 < len(tokens)
                    and tokens[i + 1] in {
                        "d", "day", "days", "h", "hr", "hour", "hours",
                        "m", "min", "mins", "minute", "minutes",
                    }):
                _emit(t + tokens[i + 1])
                i += 2
            else:
                _emit(t)
                i += 1
    if not out:
        return None
    return sorted(out, reverse=True)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%a %d %b, %I:%M %p")


def _aware(dt):
    """Ensure a datetime is timezone-aware. SQLite strips tzinfo on round-trip."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt


def fmt_offsets(offsets) -> str:
    parts = []
    for m in offsets:
        if m == 0:
            parts.append("at due time")
        elif m % 1440 == 0:
            parts.append(f"{m // 1440}d before")
        elif m % 60 == 0:
            parts.append(f"{m // 60}h before")
        else:
            d, rem = divmod(m, 1440)
            h, mm = divmod(rem, 60)
            bits = []
            if d:
                bits.append(f"{d}d")
            if h:
                bits.append(f"{h}h")
            if mm:
                bits.append(f"{mm}m")
            parts.append(" ".join(bits) + " before")
    return ", ".join(parts)


def clean_title(text: str, matched: str | None = None) -> str:
    """
    Strip the date phrase from the task title.

    Preferred path: pass `matched` (the substring search_dates pulled out)
    and we remove just that. If not provided, fall back to regex heuristics.
    """
    cleaned = text
    if matched:
        # Case-insensitive removal of the matched phrase, plus optional
        # leading prepositions like "at", "on", "by", "in".
        import re as _re
        pat = _re.compile(
            r"\s*\b(?:at|on|by|in)?\s*" + _re.escape(matched) + r"\b",
            _re.IGNORECASE,
        )
        cleaned = pat.sub("", cleaned, count=1)
    else:
        patterns = [
            r"\b(today|tomorrow|tonight|tmrw)\b",
            r"\bin \d+ (minutes?|hours?|days?|weeks?)\b",
            r"\bnext (mon|tue|wed|thu|fri|sat|sun)[a-z]*\b",
            r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            r"\b(morning|afternoon|evening|night|noon|midnight)\b",
            r"\bat \d{1,2}(:\d{2})?\s?(am|pm)?\b",
            r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b",
            r"\bon \d{1,2}(st|nd|rd|th)?( of)?\s?[a-z]*\b",
        ]
        for p in patterns:
            cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-:")
    return cleaned or text


# --------------------------------------------------------------------------- #
# Scheduler                                                                   #
# --------------------------------------------------------------------------- #
scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=DB_URL)},
    timezone=TZ,
)


async def fire_reminder(task_id: int, reminder_id: int):
    """Send the reminder message to the user."""
    s = Session()
    try:
        task = s.get(Task, task_id)
        reminder = s.get(Reminder, reminder_id)
        if not task or not reminder or task.done:
            return

        now = datetime.now(TZ)
        delta = _aware(task.due_at) - now
        if delta.total_seconds() > 0:
            mins_left = int(delta.total_seconds() // 60)
            if mins_left >= 1440:
                when = f"in {mins_left // 1440}d {(mins_left % 1440) // 60}h"
            elif mins_left >= 60:
                when = f"in {mins_left // 60}h {mins_left % 60}m"
            else:
                when = f"in {mins_left}m"
            header = f"⏰ Reminder ({when})"
        else:
            overdue_min = int(-delta.total_seconds() // 60)
            if overdue_min >= 60:
                header = f"⚠️ Overdue by {overdue_min // 60}h {overdue_min % 60}m"
            else:
                header = f"⚠️ Overdue by {overdue_min}m"

        text = (
            f"{header}\n\n"
            f"\U0001F4CC *{task.title}*\n"
            f"Due: {fmt_dt(_aware(task.due_at))}"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Done", callback_data=f"done:{task.id}"),
                InlineKeyboardButton("⏱ Snooze 30m", callback_data=f"snooze:{task.id}:30"),
                InlineKeyboardButton("\U0001F5D1 Delete", callback_data=f"del:{task.id}"),
            ]
        ])

        await application.bot.send_message(
            chat_id=task.chat_id, text=text, reply_markup=kb, parse_mode="Markdown"
        )

        reminder.sent = True
        s.commit()

        # If this was the last scheduled reminder and the task is past due
        # without confirmation, queue another nag in 30 min.
        all_sent = all(r.sent for r in task.reminders)
        if all_sent and not task.done and now >= _aware(task.due_at):
            schedule_followup(task.id, minutes=30)
    finally:
        s.close()


def schedule_followup(task_id: int, minutes: int = 30):
    """Add a one-off follow-up reminder N minutes from now."""
    s = Session()
    try:
        task = s.get(Task, task_id)
        if not task or task.done:
            return
        fire_at = datetime.now(TZ) + timedelta(minutes=minutes)
        reminder = Reminder(task_id=task.id, fire_at=fire_at)
        s.add(reminder)
        s.commit()
        scheduler.add_job(
            fire_reminder,
            "date",
            run_date=fire_at,
            args=[task.id, reminder.id],
            id=f"r{reminder.id}",
            replace_existing=True,
        )
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# Handlers                                                                    #
# --------------------------------------------------------------------------- #
WELCOME = (
    "\U0001F44B Hi! I'm your reminder bot.\n\n"
    "Just send me a task in natural language, e.g.:\n"
    "  • *meeting with John tomorrow 5pm*\n"
    "  • *submit taxes friday morning*\n"
    "  • *call mom in 2 days evening*\n\n"
    "Then I'll ask when to remind you (e.g. `1d 1h 0` = a day before, an "
    "hour before, and at the due time).\n\n"
    "Commands:\n"
    "/list — show pending tasks\n"
    "/cancel — abandon a task you're in the middle of creating\n"
    "/help — this message\n"
    f"\nTimezone: *{TIMEZONE}*"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = Session()
    try:
        draft = s.get(Draft, chat_id)
        if draft:
            s.delete(draft)
            s.commit()
            await update.message.reply_text("\U0001F5D1 Pending task cancelled.")
        else:
            await update.message.reply_text("Nothing pending to cancel.")
    finally:
        s.close()


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = Session()
    try:
        tasks = (
            s.query(Task)
            .filter_by(chat_id=update.effective_chat.id, done=False)
            .order_by(Task.due_at)
            .all()
        )
        if not tasks:
            await update.message.reply_text("\U0001F389 No pending tasks. Inbox zero!")
            return
        lines = ["*Your pending tasks:*\n"]
        for t in tasks:
            offsets = [int(x) for x in t.reminder_offsets.split(",") if x]
            lines.append(
                f"• *{t.title}*\n  Due: {fmt_dt(_aware(t.due_at))}\n  "
                f"Reminders: {fmt_offsets(offsets)}\n  /done\\_{t.id}  /del\\_{t.id}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    finally:
        s.close()


async def quick_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"/done_(\d+)", update.message.text)
    if not m:
        return
    await mark_done(update.effective_chat.id, int(m.group(1)),
                    lambda t, **kw: update.message.reply_text(t, parse_mode="Markdown"))


async def quick_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = re.match(r"/del_(\d+)", update.message.text)
    if not m:
        return
    await delete_task(update.effective_chat.id, int(m.group(1)),
                      lambda t, **kw: update.message.reply_text(t, parse_mode="Markdown"))


async def mark_done(chat_id: int, task_id: int, reply):
    s = Session()
    try:
        task = s.get(Task, task_id)
        if not task or task.chat_id != chat_id:
            await reply("Task not found.")
            return
        task.done = True
        for r in task.reminders:
            if not r.sent:
                try:
                    scheduler.remove_job(f"r{r.id}")
                except Exception:
                    pass
        s.commit()
        await reply(f"✅ Done: *{task.title}*")
    finally:
        s.close()


async def delete_task(chat_id: int, task_id: int, reply):
    s = Session()
    try:
        task = s.get(Task, task_id)
        if not task or task.chat_id != chat_id:
            await reply("Task not found.")
            return
        for r in task.reminders:
            if not r.sent:
                try:
                    scheduler.remove_job(f"r{r.id}")
                except Exception:
                    pass
        title = task.title
        s.delete(task)
        s.commit()
        await reply(f"\U0001F5D1 Deleted: *{title}*")
    finally:
        s.close()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler: either start a new task or capture reminder schedule."""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    lower = text.lower()

    s = Session()
    abandon_draft = False
    try:
        draft = s.get(Draft, chat_id)
        if draft:
            # First try: does this look like a schedule reply?
            offsets = parse_offsets(text)
            if offsets is not None:
                title, due_at = draft.title, _aware(draft.due_at)
                s.delete(draft)
                s.commit()
                await create_task_with_schedule(
                    chat_id, title, due_at, offsets, update.message.reply_text,
                )
                return

            # Second try: is it clearly a brand-new task with a future date?
            new_dt, _ = parse_natural_date(text)
            if new_dt and new_dt > datetime.now(TZ):
                abandon_draft = True  # fall through and let new-task code run
            else:
                # Ambiguous — keep the draft and re-prompt the user.
                await update.message.reply_text(
                    "I didn't recognize that as a reminder schedule.\n\n"
                    "Try things like:\n"
                    "  • `1d 1h 0`\n"
                    "  • `30m`\n"
                    "  • `an hour before`\n"
                    "  • `default`\n\n"
                    f"Pending task: *{draft.title}* due {fmt_dt(_aware(draft.due_at))}\n"
                    "Send /cancel to abandon it.",
                    parse_mode="Markdown",
                )
                return
        if abandon_draft and draft:
            s.delete(draft)
            s.commit()
    finally:
        s.close()

    # Treat as a new task.
    dt, matched = parse_natural_date(text)
    if not dt:
        await update.message.reply_text(
            "I couldn't find a date in that. Try something like "
            "*meeting with John tomorrow 5pm*.",
            parse_mode="Markdown",
        )
        return

    if dt <= datetime.now(TZ):
        await update.message.reply_text(
            f"That parses as {fmt_dt(dt)} — which is in the past. "
            "Try again with a future date."
        )
        return

    title = clean_title(text, matched)

    # Persist the draft so a redeploy doesn't lose state.
    s = Session()
    try:
        existing = s.get(Draft, chat_id)
        if existing:
            s.delete(existing)
        s.add(Draft(chat_id=chat_id, title=title, due_at=dt))
        s.commit()
    finally:
        s.close()

    await update.message.reply_text(
        f"\U0001F4CC *{title}*\n"
        f"Due: {fmt_dt(dt)}\n\n"
        "When should I remind you? Examples:\n"
        "  • `1d 1h 0` (a day before, an hour before, and at due time)\n"
        "  • `2h, 30m, 0`\n"
        "  • `30m`\n"
        "Or send `default` for 1d/1h/at-time.\n"
        "Send /cancel to abandon this task.",
        parse_mode="Markdown",
    )


async def create_task_with_schedule(
    chat_id: int, title: str, due_at: datetime, offsets: list, reply
):
    s = Session()
    try:
        task = Task(
            chat_id=chat_id,
            title=title,
            due_at=due_at,
            reminder_offsets=",".join(str(o) for o in offsets),
        )
        s.add(task)
        s.flush()

        now = datetime.now(TZ)
        due_at = _aware(due_at)
        scheduled = []
        for off in offsets:
            fire_at = due_at - timedelta(minutes=off)
            if fire_at <= now:
                continue
            r = Reminder(task_id=task.id, fire_at=fire_at)
            s.add(r)
            s.flush()
            scheduler.add_job(
                fire_reminder,
                "date",
                run_date=fire_at,
                args=[task.id, r.id],
                id=f"r{r.id}",
                replace_existing=True,
            )
            scheduled.append(fire_at)
        s.commit()

        if scheduled:
            sched_str = ", ".join(fmt_dt(d) for d in scheduled)
            await reply(
                f"✅ Saved *{title}* due {fmt_dt(due_at)}.\n"
                f"Reminders set for: {sched_str}",
                parse_mode="Markdown",
            )
        else:
            await reply(
                "⚠️ Saved but all reminder times are already in the past. "
                "Send `/list` and tap Snooze to reschedule.",
                parse_mode="Markdown",
            )
    finally:
        s.close()


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[0]
    task_id = int(parts[1])

    if action == "done":
        await mark_done(q.message.chat_id, task_id,
                        lambda t, **kw: q.edit_message_text(t, parse_mode="Markdown"))
    elif action == "del":
        await delete_task(q.message.chat_id, task_id,
                          lambda t, **kw: q.edit_message_text(t, parse_mode="Markdown"))
    elif action == "snooze":
        minutes = int(parts[2]) if len(parts) > 2 else 30
        schedule_followup(task_id, minutes)
        await q.edit_message_text(f"⏱ Snoozed for {minutes}m. I'll ping you again then.")


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
application = Application.builder().token(BOT_TOKEN).build()


async def post_init(app: Application):
    scheduler.start()
    logger.info("Scheduler started. Timezone=%s", TIMEZONE)


async def on_error(update, context):
    logger.exception("Handler error", exc_info=context.error)
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Something went wrong handling that. "
                     "I've logged it. You can try again, or /cancel and start over.",
            )
    except Exception:
        pass


def main():
    application.post_init = post_init
    application.add_error_handler(on_error)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("list", list_tasks))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(MessageHandler(filters.Regex(r"^/done_\d+"), quick_done))
    application.add_handler(MessageHandler(filters.Regex(r"^/del_\d+"), quick_del))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
