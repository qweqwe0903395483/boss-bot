# boss_bot.py
# -*- coding: utf-8 -*-
import os
import re
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ===== env =====
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
# 可選：指定伺服器 ID（限定該伺服器同步 slash 指令）；不填就全域註冊
GUILD_ID = os.getenv("GUILD_ID")
MY_GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

EARLY_MINUTES = int(os.getenv("EARLY_MINUTES", "3"))          # 提前提醒分鐘
ANTI_DUP_GRACE_SEC = int(os.getenv("ANTI_DUP_GRACE_SEC", "180"))

# 預設 BOSS 與周期（分鐘）
DEFAULT_BOSSES: dict[int, list[str]] = {
    120: ["02", "03"],
    180: ["05", "06", "08", "10"],
    240: ["12", "14", "70-2F"],
    300: ["17", "18"],
    360: ["19", "21", "80-3F"],
    480: ["22", "26", "29", "j70-2F"],
    600: ["30", "31", "32", "40"],
    720: ["B3", "33", "34", "37", "j80-3F"],
    840: ["41"],
}

# ===== log =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("boss-bot")

# ===== data =====
DATA_FILE = Path("records.json")

# records[boss] = {
#   "period": int,
#   "last_kill": datetime|None,
#   "user": str|None,
#   "killed_by": str|None,
#   "channel": int|None,
#   "reminded": bool,
#   "carded": bool,
#   "card_channel_id": int|None,
#   "card_msg_id": int|None,
#   "manual_set_at": str(iso)|None,
# }
records: dict[str, dict] = {}


def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def save_records() -> None:
    out = {}
    for k, v in records.items():
        d = dict(v)
        if isinstance(d.get("last_kill"), datetime):
            d["last_kill"] = d["last_kill"].isoformat()
        out[k] = d
    DATA_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已儲存 records.json（%d 筆）", len(records))


def load_records() -> None:
    records.clear()
    if not DATA_FILE.exists():
        log.info("第一次執行，尚未有 records.json，將在運行過程中建立。")
        return
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        for k, v in raw.items():
            d = dict(v)
            lk = d.get("last_kill")
            if isinstance(lk, str):
                try:
                    d["last_kill"] = datetime.fromisoformat(lk)
                except Exception:
                    d["last_kill"] = None
            records[k] = d
        log.info("已載入 records.json（%d 筆）", len(records))
    except Exception:
        log.exception("讀取 records.json 失敗")


# ===== helpers =====
def boss_label(name: str) -> str:
    return f"{name} BOSS"


def safe_period(p: Optional[int], fallback: int = 120) -> int:
    try:
        n = int(p or fallback)
        return n if n > 0 else fallback
    except Exception:
        return fallback


def ensure_boss(boss: str, period_hint: Optional[int] = None) -> str:
    b = boss.strip()
    if b not in records:
        per = period_hint
        if per is None:
            for pp, names in DEFAULT_BOSSES.items():
                if b in names:
                    per = pp
                    break
        per = per or 120
        records[b] = {"period": int(per), "last_kill": None, "channel": None}
    return b


def progress_bar(elapsed: timedelta, total_minutes: int, width: int = 16) -> str:
    total = max(1, total_minutes * 60)
    e = max(0, min(total, int(elapsed.total_seconds())))
    filled = int(round(e / total * width))
    filled = max(0, min(width, filled))
    return "#" * filled + "-" * (width - filled)


def pretty_compact(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total <= 0:
        return "0分"
    m, _ = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}時{m:02d}分" if h else f"{m}分"


# 狀態：SPAWNED（已出）/ SOON（即將）/ WAIT（等待）/ MISSED（錯過≥1輪）
def status_of(
    period: int, last: Optional[datetime], now: datetime
) -> Tuple[str, str, Optional[datetime], Optional[int], Optional[int]]:
    if not last:
        return "WAIT", "未設定", None, None, None

    respawn = last + timedelta(minutes=period)
    remain = respawn - now
    if remain.total_seconds() <= 0:
        over_secs = int(-remain.total_seconds())
        miss_times = over_secs // (period * 60)
        if miss_times >= 1:
            return "MISSED", f"已超過 {miss_times} 輪", respawn, miss_times, over_secs // 60
        else:
            return "SPAWNED", "0分", respawn, 0, 0
    elif remain.total_seconds() <= EARLY_MINUTES * 60:
        return "SOON", pretty_compact(remain), respawn, 0, None
    else:
        return "WAIT", pretty_compact(remain), respawn, 0, None


def chunk_text_blocks(lines: List[str], max_len: int = 950) -> List[str]:
    blocks, cur = [], ""
    for ln in lines:
        add = (ln + "\n")
        if len(cur) + len(add) > max_len:
            blocks.append(cur.rstrip("\n"))
            cur = add
        else:
            cur += add
    if cur:
        blocks.append(cur.rstrip("\n"))
    return blocks


def fmt_m_d(dt: Optional[datetime]) -> str:
    return "--/--" if not dt else dt.strftime("%m-%d")


def fmt_h_m(dt: Optional[datetime]) -> str:
    return "--:--" if not dt else dt.strftime("%H:%M")


# ===== embed =====
def build_boss_card(
    boss: str,
    rec: dict,
    now: Optional[datetime] = None,
    *,
    state_override: Optional[str] = None,
    color_override: Optional[discord.Color] = None,
    footer_text: Optional[str] = None,
) -> discord.Embed:
    now = now or datetime.now()
    period = safe_period(rec.get("period", 120))
    last: Optional[datetime] = rec.get("last_kill")

    state_line = "等待中"
    color = discord.Color.greyple()
    bar_text = None
    respawn = None
    remain_text = "未設定"

    if last:
        respawn = last + timedelta(minutes=period)
        remain = respawn - now
        elapsed = now - last
        bar_text = progress_bar(elapsed, period, 18)

        sym, remain_text_calc, _, miss_times, _minutes_over = status_of(period, last, now)
        if sym == "SPAWNED":
            state_line = "已刷新"
            color = discord.Color.red()
            remain_text = "0分"
        elif sym == "SOON":
            state_line = f"即將刷新（{EARLY_MINUTES} 分內）"
            color = discord.Color.gold()
            remain_text = remain_text_calc
        elif sym == "WAIT":
            state_line = "冷卻中"
            color = discord.Color.blurple()
            remain_text = remain_text_calc
        elif sym == "MISSED":
            state_line = "已超過刷新時間"
            color = discord.Color.orange()
            remain_text = remain_text_calc

    if state_override:
        state_line = state_override
    if color_override:
        color = color_override

    desc = f"**狀態**：{state_line}\n\n**冷卻**：{period} 分"
    if bar_text:
        desc += f"\n**進度**：`{bar_text}`"

    e = discord.Embed(title=f"📜 {boss_label(boss)}", description=desc, color=color)

    if last and respawn:
        e.add_field(name="上次擊殺", value=f"{fmt_m_d(last)}\n{fmt_h_m(last)}", inline=True)
        e.add_field(name="預計刷新", value=f"{fmt_m_d(respawn)}\n{fmt_h_m(respawn)}", inline=True)
        e.add_field(name="剩餘", value=remain_text, inline=True)

    if footer_text:
        e.set_footer(text=footer_text)
    return e


# ===== View（按鈕）=====
class BossKillView(discord.ui.View):
    def __init__(self, boss: str, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.boss = boss
        for child in self.children:
            child.disabled = disabled

    @discord.ui.button(label="✅ 已擊殺", style=discord.ButtonStyle.danger)
    async def btn_kill(self, interaction: discord.Interaction, _: discord.ui.Button):
        b = ensure_boss(self.boss)
        now = datetime.now()
        rec = records[b]
        rec["last_kill"] = now
        rec["killed_by"] = interaction.user.display_name
        rec.pop("reminded", None)
        rec.pop("carded", None)
        rec.pop("card_channel_id", None)
        rec.pop("card_msg_id", None)
        save_records()

        e = build_boss_card(
            b, rec, now,
            state_override="已手動標記擊殺",
            color_override=discord.Color.green(),
            footer_text=f"操作人：{interaction.user.display_name}",
        )
        self.clear_items()
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def btn_no(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("已取消。", ephemeral=True)


# ===== Cog =====
class BossCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_task.start()

    def cog_unload(self):
        self.check_task.cancel()

    async def _disable_existing_card(self, boss: str):
        rec = records.get(boss, {})
        chan_id = rec.get("card_channel_id")
        msg_id = rec.get("card_msg_id")
        if not chan_id or not msg_id:
            return
        try:
            channel = self.bot.get_channel(chan_id) or await self.bot.fetch_channel(chan_id)
            msg = await channel.fetch_message(msg_id)
            await msg.edit(view=BossKillView(boss, disabled=True))
            rec.pop("card_channel_id", None)
            rec.pop("card_msg_id", None)
            save_records()
        except Exception as e:
            log.warning("無法停用舊卡片：%s", e)

    @tasks.loop(seconds=60)
    async def check_task(self):
        now = datetime.now()
        for name, rec in list(records.items()):
            if not rec.get("last_kill") or not rec.get("period"):
                continue

            kill = rec["last_kill"]
            period = safe_period(rec["period"])
            respawn = kill + timedelta(minutes=period)
            remind_time = respawn - timedelta(minutes=EARLY_MINUTES)

            chan_id = rec.get("channel")
            if not chan_id:
                continue
            channel = self.bot.get_channel(chan_id) or await self.bot.fetch_channel(chan_id)
            if not channel:
                continue

            # 提前提醒
            if not rec.get("reminded") and now >= remind_time and now < respawn:
                rec["reminded"] = True
                e = build_boss_card(name, rec, now, state_override=f"即將刷新（{EARLY_MINUTES} 分內）")
                try:
                    await channel.send(embed=e)
                    save_records()
                except Exception:
                    log.exception("提前提醒送出失敗")

            # 到點發卡
            if not rec.get("carded") and now >= respawn:
                rec["carded"] = True
                disable_view = False
                if rec.get("manual_set_at"):
                    try:
                        set_at = datetime.fromisoformat(rec["manual_set_at"])
                        if (now - set_at).total_seconds() <= ANTI_DUP_GRACE_SEC:
                            disable_view = True
                    except Exception:
                        pass

                e = build_boss_card(name, rec, now, state_override="已刷新")
                try:
                    msg = await channel.send(embed=e, view=BossKillView(name, disabled=disable_view))
                    rec["card_channel_id"] = msg.channel.id
                    rec["card_msg_id"] = msg.id
                    save_records()
                except Exception:
                    log.exception("刷新卡片送出失敗")

    @check_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def _send_embeds(self, interaction: discord.Interaction, embeds: list[discord.Embed]):
        if not embeds:
            if interaction.response.is_done():
                await interaction.followup.send("沒有可顯示的內容。", ephemeral=True)
            else:
                await interaction.response.send_message("沒有可顯示的內容。", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embeds[0])
            for e in embeds[1:]:
                await interaction.followup.send(embed=e)
        else:
            for e in embeds:
                await interaction.followup.send(embed=e)

    # ===== 管理 =====
    @app_commands.command(name="add", description="新增 BOSS 並設定週期（分鐘）")
    @app_commands.describe(boss="BOSS 名稱", period="刷新週期（分鐘）")
    @app_commands.checks.has_permissions(administrator=True)
    async def add_(self, interaction: discord.Interaction, boss: str, period: int):
        b = ensure_boss(boss, period)
        records[b]["period"] = int(period)
        save_records()
        await interaction.response.send_message(f"已新增 {boss_label(b)}，週期 {period} 分。", ephemeral=True)

    @app_commands.command(name="set", description="設定 BOSS 週期（分鐘）")
    @app_commands.describe(boss="BOSS 名稱", period="刷新週期（分鐘）")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_(self, interaction: discord.Interaction, boss: str, period: int):
        b = ensure_boss(boss)
        records[b]["period"] = int(period)
        save_records()
        await interaction.response.send_message(f"已更新 {boss_label(b)} 週期為 {period} 分。", ephemeral=True)

    @app_commands.command(name="del", description="刪除 BOSS")
    @app_commands.describe(boss="BOSS 名稱")
    @app_commands.checks.has_permissions(administrator=True)
    async def del_(self, interaction: discord.Interaction, boss: str):
        b = boss.strip()
        if b in records:
            records.pop(b)
            save_records()
            await interaction.response.send_message(f"已刪除 {boss_label(b)}。", ephemeral=True)
        else:
            await interaction.response.send_message("找不到該 BOSS。", ephemeral=True)

    @app_commands.command(name="clear", description="清空所有擊殺狀態（保留週期）")
    @app_commands.checks.has_permissions(administrator=True)
    async def clear_(self, interaction: discord.Interaction):
        for _, rec in records.items():
            rec["last_kill"] = None
            rec.pop("reminded", None)
            rec.pop("carded", None)
            rec.pop("card_channel_id", None)
            rec.pop("card_msg_id", None)
        save_records()
        await interaction.response.send_message("已清空擊殺狀態。", ephemeral=True)

    # ===== 使用 =====
    @app_commands.command(name="k", description="立即標記某 BOSS 已擊殺（記錄當前時間）")
    @app_commands.describe(boss="BOSS 名稱")
    async def k_(self, interaction: discord.Interaction, boss: str):
        b = ensure_boss(boss)
        now = datetime.now()
        rec = records[b]
        rec["last_kill"] = now
        rec["channel"] = interaction.channel.id
        rec["user"] = interaction.user.display_name
        rec["manual_set_at"] = now.isoformat()
        rec.pop("reminded", None)
        rec.pop("carded", None)
        await self._disable_existing_card(b)
        save_records()

        e = build_boss_card(b, rec, now, footer_text=f"操作人：{interaction.user.display_name}")
        log.info("/k by %s in #%s -> %s", interaction.user, interaction.channel, b)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="killat", description="把某 BOSS 的擊殺時間設為 HHMM（例如 2340）")
    @app_commands.describe(boss="BOSS 名稱", time_hhmm="時間（HHMM）")
    async def killat_(self, interaction: discord.Interaction, boss: str, time_hhmm: str):
        b = ensure_boss(boss)
        now = datetime.now()
        try:
            t = datetime.strptime(time_hhmm, "%H%M")
            kill_time = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            await interaction.response.send_message("時間格式錯誤，請輸入 HHMM（例如 2340）。", ephemeral=True)
            return

        rec = records[b]
        rec["last_kill"] = kill_time
        rec["channel"] = interaction.channel.id
        rec["user"] = interaction.user.display_name
        rec["manual_set_at"] = now.isoformat()
        rec.pop("reminded", None)
        rec.pop("carded", None)
        await self._disable_existing_card(b)
        save_records()

        e = build_boss_card(b, rec, now, footer_text=f"設定人：{interaction.user.display_name}")
        log.info("/killat by %s -> %s %s", interaction.user, b, time_hhmm)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="when", description="查詢某 BOSS 的刷新剩餘時間")
    @app_commands.describe(boss="BOSS 名稱")
    async def when_(self, interaction: discord.Interaction, boss: str):
        b = ensure_boss(boss)
        rec = records[b]
        if not rec.get("last_kill"):
            await interaction.response.send_message(f"{boss_label(b)} 尚未設定擊殺時間。", ephemeral=True)
            return
        now = datetime.now()
        e = build_boss_card(b, rec, now, footer_text=f"查詢者：{interaction.user.display_name} ＠ {now.strftime('%Y-%m-%d %H:%M:%S')}")
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="all", description="列出近期所有 BOSS 狀態（預設 10 筆）")
    @app_commands.describe(limit="顯示上限（1~40，預設 10）")
    async def all_list_(self, interaction: discord.Interaction, limit: Optional[int] = 10):
        now = datetime.now()
        limit = max(1, min(int(limit or 10), 40))
        items = []
        for b, rec in records.items():
            last = rec.get("last_kill")
            if not last:
                continue
            period = safe_period(rec.get("period"))
            sym, _, respawn, miss_times, _ = status_of(period, last, now)
            if not respawn:
                continue
            left_sec = max(0, int((respawn - now).total_seconds()))
            items.append((b, period, last, respawn, sym, miss_times, left_sec))
        items.sort(key=lambda x: x[6])

        lines: list[str] = []
        shown = 0
        for b, period, last, respawn, sym, miss_times, _ in items:
            if sym == "MISSED" and miss_times and miss_times >= 1:
                tail = f"已超過 {miss_times} 輪"
            elif sym == "SPAWNED":
                tail = "0分"
            else:
                tail = pretty_compact(respawn - now)

            lines.append(f"• {boss_label(b)}：{tail}")
            lines.append(f"  上次：{fmt_m_d(last)} {fmt_h_m(last)}")
            lines.append(f"  預計：{fmt_m_d(respawn)} {fmt_h_m(respawn)}")
            lines.append("")
            shown += 1
            if shown >= limit:
                break

        if not lines:
            await interaction.response.send_message("尚無可列出的 BOSS。", ephemeral=True)
            return

        text = "```" + "\n".join(lines).rstrip() + "```"
        e = discord.Embed(title="📋 BOSS 狀態列表", description=text)
        e.set_footer(text=f"總計 {len(items)}，顯示 {min(limit, len(items))}。/all limit:20 可顯示更多")
        await interaction.response.send_message(embed=e)

    # ===== 權限與同步 =====
    @app_commands.command(name="sync", description="同步 Slash 指令（管理員/擁有者）")
    async def sync_cmd(self, interaction: discord.Interaction):
        app_owner = (await interaction.client.application_info()).owner
        is_owner = interaction.user.id == app_owner.id
        is_admin = getattr(interaction.user.guild_permissions, "administrator", False)
        if not (is_owner or is_admin):
            await interaction.response.send_message("沒有權限。", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if MY_GUILD:
                synced = await interaction.client.tree.sync(guild=MY_GUILD)
            else:
                synced = await interaction.client.tree.sync()
            await interaction.followup.send(
                f"已同步 {len(synced)} 個指令：{', '.join(sorted(c.name for c in synced))}",
                ephemeral=True,
            )
            log.info("同步完成。")
        except Exception:
            log.exception("同步失敗")
            try:
                await interaction.followup.send("同步失敗。", ephemeral=True)
            except Exception:
                pass


# ===== Bot 啟動 =====
class BossBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(BossCog(self))

        # 預先補上預設清單
        for p, names in DEFAULT_BOSSES.items():
            for n in names:
                ensure_boss(n, p)
        load_records()
        save_records()

        try:
            if MY_GUILD:
                self.tree.copy_global_to(guild=MY_GUILD)
                synced = await self.tree.sync(guild=MY_GUILD)
                log.info("已在指定伺服器同步 %s 個 Slash 指令：%s", len(synced), ", ".join(sorted(c.name for c in synced)))
            else:
                synced = await self.tree.sync()
                log.info("已全域同步 %s 個 Slash 指令", len(synced))
        except Exception:
            log.exception("同步指令失敗")


intents = discord.Intents.default()
intents.message_content = True
bot = BossBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info("已登入：%s (id=%s)", bot.user, bot.user.id)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("找不到 DISCORD_TOKEN，請在 .env 或雲端環境變數設定。")
    bot.run(TOKEN)
