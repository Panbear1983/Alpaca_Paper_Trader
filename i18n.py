"""
i18n.py — tiny translation layer for the TUI (English / Traditional Chinese 繁體中文).

Design: a dict-based catalog (no gettext, no deps). Every user-facing string in
tui.py resolves through t(key, **fmt) at RENDER time, so modals pick up the
current language whenever they open; only the main screen needs an explicit
re-render on toggle (AlpacaTUI._apply_language).

_TABLE holds (english, traditional_chinese) pairs keyed by stable ids — building
both language dicts from one table guarantees the catalogs can never drift out
of sync. Unknown keys fall back en → key itself, so a missed translation shows
readable English rather than crashing.

Numbers/symbols/Rich markup are formatted at the CALL SITE and passed as
pre-formatted strings via t(key, name=value) placeholders.

Pure module: no Textual imports, unit-testable.
"""
from __future__ import annotations

LANGS = ["en", "zh_TW"]

# key: (english, traditional_chinese)
_TABLE: dict[str, tuple[str, str]] = {
    # ── app chrome ────────────────────────────────────────────────────────────
    "app.title": ("Alpaca Paper Trader — Cockpit",
                  "Alpaca 模擬交易 — 操盤艙"),
    "ui.loading": ("loading…", "載入中…"),
    "ui.search_ph": ("/ search ticker for company bio — e.g. NVDA",
                     "/ 搜尋股票代號查看公司簡介 — 例如 NVDA"),
    "keys.hints": (
        "[b]r[/b] refresh   [b]d[/b] dry-run   [b]p[/b] report   [b]o[/b] pf-chart   "
        "[b]w[/b] timeframe   [b]v[/b] positions/orders   "
        "[b]g[/b] sched   [b]m[/b] channels   [b]n[/b] strategy   [b]k[/b] wallet   "
        "[b]Shift+W[/b] compare   [b]h[/b] manual   [b]a[/b] arm/disarm   [b]l[/b] 中文   [b]q[/b] quit"
        "   •   "
        "[b]f[/b] flatten   [b]t[/b] tick   [b]c[/b] capitol   "
        "[b]b[/b] buy   [b]s[/b] sell   [b]e[/b] rebalance   "
        "[b]x[/b] cancel order   [b]X[/b] cancel all orders"
        "   •   [b]/[/b] bio search",
        "[b]r[/b] 更新   [b]d[/b] 模擬排名   [b]p[/b] 推送報告   [b]o[/b] 投組圖   "
        "[b]w[/b] 時間範圍   [b]v[/b] 持倉/掛單   "
        "[b]g[/b] 排程   [b]m[/b] 頻道   [b]n[/b] 策略設定   [b]k[/b] 錢包   "
        "[b]Shift+W[/b] 錢包比較   [b]h[/b] 手冊   [b]a[/b] 上鎖/解鎖   [b]l[/b] English   [b]q[/b] 離開"
        "   •   "
        "[b]f[/b] 全部平倉   [b]t[/b] 盤中執行   [b]c[/b] 國會跟單   "
        "[b]b[/b] 買入   [b]s[/b] 賣出   [b]e[/b] 再平衡   "
        "[b]x[/b] 取消掛單   [b]X[/b] 取消全部掛單"
        "   •   [b]/[/b] 公司簡介搜尋",
    ),

    # ── table columns ─────────────────────────────────────────────────────────
    "col.sym": ("SYM", "代號"),
    "col.qty": ("QTY", "數量"),
    "col.avg": ("AVG", "均價"),
    "col.price": ("PRICE", "現價"),
    "col.pl_usd": ("P&L $", "損益 $"),
    "col.pl_pct": ("P&L %", "損益 %"),
    "col.mkt_val": ("MKT VALUE", "總估值"),
    "col.cost": ("TOTAL COST", "總成本"),
    "col.side": ("SIDE", "方向"),
    "col.qty_usd": ("QTY / $", "數量 / $"),
    "col.status": ("STATUS", "狀態"),
    "col.submitted": ("SUBMITTED", "送出時間"),
    "col.id": ("ID", "編號"),

    # ── summary line + badges ─────────────────────────────────────────────────
    "sum.line": (
        "Equity [b]${equity}[/]   Stocks ${stocks}   Cash ${cash}   "
        "RegT(2x) ${regt}   DT(4x) ${dt}   "
        "DayP&L [{pc}]{daypl} ({daypct}%)[/]   "
        "TotalP&L [{tpc}]{totalpl} ({totalpct}%)[/]   "
        "Exposure [b]{lev}x[/]",
        "權益 [b]${equity}[/]   持股 ${stocks}   現金 ${cash}   "
        "RegT(2x) ${regt}   DT(4x) ${dt}   "
        "日損益 [{pc}]{daypl} ({daypct}%)[/]   "
        "總損益 [{tpc}]{totalpl} ({totalpct}%)[/]   "
        "曝險 [b]{lev}x[/]",
    ),
    "sum.split": ("[dim]Realized[/] [{rc}]{rpl}[/] [dim]· Unrealized[/] [{uc}]{upl}[/]",
                  "[dim]已實現[/] [{rc}]{rpl}[/] [dim]· 未實現[/] [{uc}]{upl}[/]"),
    "badge.market_open": ("[b black on green] MARKET OPEN [/]",
                          "[b black on green] 市場開盤中 [/]"),
    "badge.market_closed": (
        "[b black on yellow] MARKET CLOSED [/][yellow] orders queue → {next}[/]",
        "[b black on yellow] 市場已收盤 [/][yellow] 掛單排隊至 {next}[/]"),
    "badge.market_unknown": ("[dim] market ? [/]", "[dim] 市場 ? [/]"),
    "armbar.armed": ("[b white on red]  ARMED — live orders ENABLED  [/]",
                     "[b white on red]  已解鎖 — 可送出實際下單  [/]"),
    "armbar.disarmed": ("[b black on green]  DISARMED — safe (press 'a' to arm)  [/]",
                        "[b black on green]  已上鎖 — 安全（按 'a' 解鎖）  [/]"),

    # ── shared buttons ────────────────────────────────────────────────────────
    "btn.yes": ("Yes", "是"),
    "btn.no": ("No", "否"),
    "btn.buy": ("Buy", "買入"),
    "btn.sell": ("Sell", "賣出"),
    "btn.cancel": ("Cancel", "取消"),
    "btn.save": ("Save", "儲存"),
    "btn.build": ("Build plan", "建立計畫"),
    "btn.rename": ("Rename", "重新命名"),

    # ── buy modal ─────────────────────────────────────────────────────────────
    "buy.title": ("Manual BUY — symbol + notional USD",
                  "手動買入 — 代號＋金額（美元）"),
    "buy.sym_ph": ("Symbol e.g. NVDA", "代號，例如 NVDA"),
    "buy.avail": ("Cash available: [b]${cash}[/]", "可用現金：[b]${cash}[/]"),
    "buy.amt_ph": ("Notional USD e.g. 1000", "金額（美元），例如 1000"),
    "buy.after_margin": (
        "buy [b]${amt}[/] → cash [b]$0.00[/] [yellow](+${over} on margin/buying power)[/]",
        "買入 [b]${amt}[/] → 現金 [b]$0.00[/] [yellow]（+${over} 動用融資/購買力）[/]"),
    "buy.after": ("buy [b]${amt}[/] → cash after [b]${after}[/]",
                  "買入 [b]${amt}[/] → 剩餘現金 [b]${after}[/]"),
    "buy.err_sym": ("[red]enter a symbol[/]", "[red]請輸入代號[/]"),
    "buy.err_amt": ("[red]enter a positive USD amount (e.g. 1000, or 'all')[/]",
                    "[red]請輸入正的美元金額（例如 1000，或 'all'）[/]"),
    "log.buy_aborted": ("[dim]buy cancelled — NO order was sent[/]",
                        "[dim]已取消買入 — 未送出任何委託[/]"),

    # ── sell modal ────────────────────────────────────────────────────────────
    "sell.title": ("SELL {sym} — amount", "賣出 {sym} — 金額"),
    "sell.info": ("Position value: [b]${pv}[/]   Cash now: [b]${cash}[/]",
                  "持倉市值：[b]${pv}[/]   目前現金：[b]${cash}[/]"),
    "sell.amt_ph": ("all / $ amount", "all（全部）/ 金額"),
    "sell.after": (
        "sell [b]${amt}[/] → cash after [b]${after}[/], position left [b]${left}[/]",
        "賣出 [b]${amt}[/] → 現金變為 [b]${after}[/]，剩餘持倉 [b]${left}[/]"),

    # ── rebalance modal ───────────────────────────────────────────────────────
    "reb.title": (
        "Rebalance — keep the top how many (by P&L %)?\n"
        "Sells the rest, redeploys proceeds into the kept names.\n"
        "Then pick ONE: deploy idle cash OR withdraw (raise cash).\n"
        "Each field: 0 = none, a $ amount, or 'all'.",
        "再平衡 — 依損益 % 保留前幾名？\n"
        "其餘全部賣出，所得資金重新投入保留的名單。\n"
        "接著擇一：投入閒置現金，或提出資金（增加現金）。\n"
        "各欄位：0 = 不動作，或輸入金額，或 'all'（全部）。"),
    "reb.topn_ph": ("keep top N", "保留前 N 名"),
    "reb.avail": ("Available idle cash: [b]${cash}[/]", "可用閒置現金：[b]${cash}[/]"),
    "reb.deploy_ph": ("DEPLOY idle cash: 0 / amount / all",
                      "投入閒置現金：0 / 金額 / all"),
    "reb.withdraw_ph": ("WITHDRAW / raise cash: 0 / amount / all",
                        "提出資金／增加現金：0 / 金額 / all"),
    "reb.deploy_over": (
        "[yellow]deploy ${amt} → exceeds cash by ${over} (will cap at ${cap}; no leverage)[/]",
        "[yellow]投入 ${amt} → 超出現金 ${over}（將封頂為 ${cap}；不使用槓桿）[/]"),
    "reb.deploy_after": ("deploy [b]${amt}[/] → cash leftover [b]${left}[/]",
                         "投入 [b]${amt}[/] → 剩餘現金 [b]${left}[/]"),
    "reb.withdraw_over": (
        "[yellow]withdraw ${amt} → exceeds invested ${inv} (will cap = flatten)[/]",
        "[yellow]提出 ${amt} → 超出投入總額 ${inv}（將封頂 = 全部平倉）[/]"),
    "reb.withdraw_after": (
        "withdraw [b]${amt}[/] → remaining invested [b]${rem}[/]",
        "提出 [b]${amt}[/] → 剩餘投入 [b]${rem}[/]"),
    "reb.err_both": ("[red]pick ONE: deploy OR withdraw, not both[/]",
                     "[red]只能擇一：投入或提出，不可同時[/]"),

    # ── schedule modal ────────────────────────────────────────────────────────
    "sched.title": ("Auto-report schedule", "自動報告排程"),
    "sched.enabled_ph": ("enabled? yes/no", "啟用？yes/no"),
    "sched.time_ph": ("time ET (HH:MM, 24h)", "美東時間（HH:MM，24 小時制）"),
    "sched.weekdays_ph": ("weekdays only? yes/no", "僅平日？yes/no"),
    "sched.channel_ph": ("channel: {names}", "頻道：{names}"),
    "sched.err_time": ("[red]time must be HH:MM (24h)[/]",
                       "[red]時間格式須為 HH:MM（24 小時制）[/]"),
    "sched.err_channel": ("[red]channel must be one of: {names}[/]",
                          "[red]頻道必須是下列之一：{names}[/]"),

    # ── channels modal ────────────────────────────────────────────────────────
    "chan.title": (
        "Telegram channels: {names}\ndefault = {default}\n"
        "Values live in .env — this edits config only.",
        "Telegram 頻道：{names}\n預設 = {default}\n"
        "實際值存於 .env — 此處僅編輯設定。"),
    "chan.default_ph": ("set default channel (existing name)",
                        "設定預設頻道（既有名稱）"),
    "chan.newname_ph": ("add channel — name (optional)", "新增頻道 — 名稱（選填）"),
    "chan.token_ph": ("new channel TOKEN env-var name", "新頻道 TOKEN 環境變數名稱"),
    "chan.chat_ph": ("new channel CHAT env-var name", "新頻道 CHAT 環境變數名稱"),
    "chan.err_env": ("[red]new channel needs both env-var names[/]",
                     "[red]新頻道需要兩個環境變數名稱[/]"),
    "chan.err_missing": ("[red]'{name}' is not an existing channel[/]",
                         "[red]'{name}' 不是既有頻道[/]"),

    # ── strategy config modal ─────────────────────────────────────────────────
    "cfg.col.section": ("SECTION", "區段"),
    "cfg.col.setting": ("SETTING", "設定"),
    "cfg.col.value": ("VALUE", "數值"),
    "cfg.col.range": ("RANGE", "範圍"),
    "cfg.head": (
        "[b]Strategy tunables — {wallet}[/] — live-editing {file}\n"
        "[dim]last: manage {man} · swing {swing} · copy {copy} · "
        "intraday: LOCKED (edit JSON only — 't' key trades 4x if enabled)[/]",
        "[b]策略參數 — {wallet}[/] — 即時編輯 {file}\n"
        "[dim]最近：倉位管理 {man} · 波段 {swing} · 跟單 {copy} · "
        "盤中：鎖定（僅可手動改 JSON — 啟用後 't' 鍵將以 4 倍槓桿交易）[/]"),
    "cfg.hint": (
        "[dim]Enter=edit · r=reload · Esc=close — changes go live on the next "
        "scheduler tick (≤20 min)[/]",
        "[dim]Enter=編輯 · r=重新載入 · Esc=關閉 — 變更於下一個排程週期生效"
        "（≤20 分鐘）[/]"),
    "edit.current": ("current: [b]{cur}[/]", "目前：[b]{cur}[/]"),
    "edit.range": ("   range: {rng}", "   範圍：{rng}"),
    "danger.eng_swing": ("Swing buyer", "波段買入引擎"),
    "danger.eng_capitol": ("Capitol autorun (exit engine + copies)",
                           "國會自動跟單（出場引擎＋跟單）"),
    "danger.live": ("LIVE", "啟用"),
    "danger.off": ("OFF", "停用"),
    "danger.master": ("{eng} → {state} at the next scheduler tick. Proceed?",
                      "{eng} → 於下一個排程週期{state}。確定繼續？"),
    "danger.prune": (
        "Enable PRUNE? Next tick SELLS {n} position(s): {names}. Proceed?",
        "啟用 PRUNE（賣出非目標產業）？下一週期將賣出 {n} 檔：{names}。確定繼續？"),
    "danger.prune_none": ("(none currently off-sector)", "（目前無非目標產業持倉）"),
    "danger.cap": (
        "Cap {new} < {held} held → cap-tail SELLS the {k} smallest position(s) "
        "next tick. Proceed?",
        "上限 {new} < 持有 {held} 檔 → 下一週期將賣出最小的 {k} 檔。確定繼續？"),
    "danger.exposure": (
        "Exposure cap 1.00 = fully invested — margin territory. Proceed?",
        "曝險上限 1.00 = 全額投入 — 進入融資區間。確定繼續？"),
    "log.cfg_save_err": ("[red]cfg save error {path}: {e}[/]",
                         "[red]設定儲存錯誤 {path}：{e}[/]"),
    "log.cfg_changed": ("[cyan]cfg {path}: {old} → {new} (live ≤1 tick)[/]",
                        "[cyan]設定 {path}：{old} → {new}（≤1 週期生效）[/]"),

    # ── manual modal ──────────────────────────────────────────────────────────
    "man.hint": (
        "[dim]↑/↓ + Enter: jump to topic in the index (left) · "
        "PgUp/PgDn: scroll · Esc: close[/]",
        "[dim]↑/↓ + Enter：跳至左側索引主題 · PgUp/PgDn：捲動 · Esc：關閉[/]"),
    "man.unavailable": ("# Manual unavailable\n\nCould not read `{file}`: {e}",
                        "# 手冊無法使用\n\n無法讀取 `{file}`：{e}"),

    # ── wallet modals ─────────────────────────────────────────────────────────
    "wal.title": (
        "[b]Switch wallet[/] — the account the TUI views + acts on\n"
        "[dim]Enter=switch · e=rename · a=add new · Esc=close · "
        "Each wallet has its own strategy file; engines trade a wallet only "
        "while its own master switches are ON.[/]",
        "[b]切換錢包[/] — TUI 檢視與操作的帳戶\n"
        "[dim]Enter=切換 · e=重新命名 · a=新增 · Esc=關閉 · "
        "每個錢包有獨立策略檔；引擎僅在該錢包的主開關開啟時才會交易。[/]"),
    "wal.col.wallet": ("WALLET", "錢包"),
    "wal.col.account": ("ACCOUNT", "帳號"),
    "wal.col.status": ("STATUS", "狀態"),
    "wal.active": ("active", "使用中"),
    "wal.ready": ("ready", "可用"),
    "wal.missing": ("⚠ set {vars} in .env", "⚠ 請在 .env 設定 {vars}"),
    "wal.err_not_configured": (
        "[red]'{name}' not configured — add {miss} to .env, then reopen[/]",
        "[red]'{name}' 尚未設定 — 請在 .env 加入 {miss} 後重新開啟[/]"),
    "wren.title": ("Rename wallet [b]{name}[/]\n"
                   "[dim]Display label only — credentials/.env are unchanged.[/]",
                   "重新命名錢包 [b]{name}[/]\n"
                   "[dim]僅變更顯示名稱 — 憑證/.env 不受影響。[/]"),
    "wren.ph": ("new wallet name", "新錢包名稱"),
    "wren.err_empty": ("[red]name cannot be empty[/]", "[red]名稱不可為空[/]"),
    "btn.add": ("Add", "新增"),
    "wadd.title": (
        "Add a NEW wallet — paste its Alpaca PAPER API keys\n"
        "[dim]Generate keys for that paper account on app.alpaca.markets. "
        "They are validated live against /v2/account BEFORE anything is "
        "saved; on success the account number + equity are shown so a "
        "wrong-account keypair is caught immediately. Keys go into .env "
        "only; the new wallet starts with every engine switch OFF.[/]",
        "新增錢包 — 貼上該 Alpaca 模擬帳戶的 API 金鑰\n"
        "[dim]請在 app.alpaca.markets 為該模擬帳戶產生金鑰。儲存前會先向 "
        "/v2/account 即時驗證；成功後會顯示帳號與權益，貼錯帳戶的金鑰可立即"
        "發現。金鑰僅存於 .env；新錢包的所有引擎開關預設為關閉。[/]"),
    "wadd.name_ph": ("wallet name e.g. Mid Risk", "錢包名稱，例如 Mid Risk"),
    "wadd.key_ph": ("API key (PK…)", "API 金鑰（PK…）"),
    "wadd.secret_ph": ("API secret", "API 密鑰"),
    "wadd.err_all": ("[red]name, key and secret are all required[/]",
                     "[red]名稱、金鑰與密鑰皆為必填[/]"),
    "wal.validating": ("[yellow]validating keys against Alpaca…[/]",
                       "[yellow]正在向 Alpaca 驗證金鑰…[/]"),
    "wal.added": ("[green]added '{name}' — {info}[/]",
                  "[green]已新增 '{name}' — {info}[/]"),

    # ── boot / status logs ────────────────────────────────────────────────────
    "log.booted": (
        "[dim]booted DISARMED — press 'a' to enable live actions "
        "(Telegram alerts: {state})[/]",
        "[dim]已啟動並上鎖 — 按 'a' 啟用實際操作（Telegram 通知：{state}）[/]"),
    "log.boot2": ("[dim]{status} — 'p' push report, 'g' edit schedule, 'm' channels[/]",
                  "[dim]{status} — 'p' 推送報告，'g' 編輯排程，'m' 頻道[/]"),
    "state.on": ("on", "開"),
    "state.off": ("off", "關"),
    "rs.on": ("auto-report: ON @ {time} ET ({scope}) — TUI-scheduled",
              "自動報告：開啟 @ 美東 {time}（{scope}）— TUI 排程"),
    "rs.off": ("auto-report: OFF", "自動報告：關閉"),
    "rs.unknown": ("auto-report: ?", "自動報告：？"),
    "rs.weekdays": ("weekdays", "平日"),
    "rs.daily": ("daily", "每日"),
    "log.tg_failed": ("[yellow]telegram notify failed: {e}[/]",
                      "[yellow]Telegram 通知失敗：{e}[/]"),
    "log.lang": ("[cyan]language → {lang}[/]", "[cyan]語言 → {lang}[/]"),
    "log.code_stale": (
        "[b yellow]⚠ code updated on disk — THIS WINDOW RUNS OLD CODE. "
        "Press q and relaunch (./tui.sh) before trading.[/]",
        "[b yellow]⚠ 程式碼已更新 — 此視窗執行的是舊版程式。"
        "交易前請按 q 離開並重新啟動（./tui.sh）。[/]"),

    # ── config editors' logs ──────────────────────────────────────────────────
    "log.cfg_read_err": ("[red]config read error: {e}[/]",
                         "[red]設定讀取錯誤：{e}[/]"),
    "log.sched_save_err": ("[red]schedule save error: {e}[/]",
                           "[red]排程儲存錯誤：{e}[/]"),
    "log.sched_warn_window": (
        "  [yellow]⚠ window_minutes < 10m heartbeat — report may be missed[/]",
        "  [yellow]⚠ window_minutes 小於 10 分鐘心跳 — 報告可能被錯過[/]"),
    "log.chan_added": (
        "[cyan]channel '{name}' added — set {token} / {chat} in .env "
        "(restart TUI to use)[/]",
        "[cyan]頻道 '{name}' 已新增 — 請在 .env 設定 {token} / {chat}"
        "（重啟 TUI 後生效）[/]"),
    "log.chan_default": ("[cyan]default channel → {name}[/]",
                         "[cyan]預設頻道 → {name}[/]"),
    "log.chan_save_err": ("[red]channel save error: {e}[/]",
                          "[red]頻道儲存錯誤：{e}[/]"),

    # ── wallet logs ───────────────────────────────────────────────────────────
    "log.wallet_same": ("[dim]already on wallet '{name}'[/]",
                        "[dim]已在錢包 '{name}'[/]"),
    "log.wallet_switched": ("[b cyan]switched → wallet '{name}'[/] (disarmed; refreshing…)",
                            "[b cyan]已切換 → 錢包 '{name}'[/]（已上鎖；更新中…）"),
    "log.wallet_engines": (
        "[yellow]⚠ engines follow each wallet's own strategy file — '{name}' "
        "trades autonomously only while its master switches are ON ('n')[/]",
        "[yellow]⚠ 自動引擎依各錢包自己的策略檔運作 — '{name}' 僅在其主開關"
        "開啟時才會自動交易（按 'n' 檢視）[/]"),

    # ── arm / refresh logs ────────────────────────────────────────────────────
    "log.arm_closed": (
        "[yellow]⚠ Market CLOSED — any orders you place now will not fill "
        "immediately. They QUEUE and execute at the next open ({next}).[/]",
        "[yellow]⚠ 市場已收盤 — 現在下的單不會立即成交，"
        "將排隊至下次開盤（{next}）執行。[/]"),
    "log.need_arm": ("[yellow]DISARMED — press 'a' first[/]",
                     "[yellow]已上鎖 — 請先按 'a' 解鎖[/]"),
    "log.refreshing": ("[dim]refreshing…[/]", "[dim]更新中…[/]"),
    "log.refresh_err": ("[red]refresh error: {e}[/]", "[red]更新錯誤：{e}[/]"),
    "log.refreshed": ("[dim]refreshed {n} positions{orders} @ {time}[/]",
                      "[dim]已更新 {n} 檔持倉{orders} @ {time}[/]"),
    "log.orders_tag": (" · {n} open order(s)", " · {n} 筆掛單"),

    # ── bio search ────────────────────────────────────────────────────────────
    "bio.no_key": ("[yellow]FMP_API_KEY not set in .env — cannot fetch company bio.[/]",
                   "[yellow].env 未設定 FMP_API_KEY — 無法取得公司簡介。[/]"),
    "bio.looking": ("[dim]looking up {sym}…[/]", "[dim]查詢 {sym} 中…[/]"),
    "bio.none": ("[dim]No profile found for {sym}.[/]",
                 "[dim]找不到 {sym} 的公司資料。[/]"),
    "bio.nodesc": ("[dim](no description available)[/]", "[dim]（無公司描述）[/]"),
    "bio.translating": ("(translating…)", "（翻譯中…）"),
    "bio.perf": ("[b]PRICE[/] {px}   [b]1M[/] {m}   [b]1Y[/] {y}   [b]ALL[/] {a}",
                 "[b]現價[/] {px}   [b]近一月[/] {m}   [b]近一年[/] {y}   [b]全期間[/] {a}"),

    # ── chart ─────────────────────────────────────────────────────────────────
    "chart.pf": ("PORTFOLIO (equity)", "投資組合（權益）"),
    "chart.holding": ("holding {sym}", "持倉 {sym}"),
    "log.chart_to": ("[cyan]chart → {where}[/]", "[cyan]圖表 → {where}[/]"),
    "log.chart_tf": ("[cyan]chart timeframe → {rng}[/]",
                     "[cyan]圖表時間範圍 → {rng}[/]"),
    "chart.select": ("Select a holding (↑/↓), or press 'o' for portfolio",
                     "請選擇持倉（↑/↓），或按 'o' 檢視投資組合"),
    "chart.loading": ("{label} [{rng}] — loading…", "{label} [{rng}] — 載入中…"),
    "chart.nodata": ("{label} [{rng}] — data unavailable",
                     "{label} [{rng}] — 資料無法取得"),
    "chart.render_err": ("{label} [{rng}] — render error",
                         "{label} [{rng}] — 繪製錯誤"),
    "log.chart_render_err": ("[yellow]chart render error ({rng}): {e}[/]",
                             "[yellow]圖表繪製錯誤（{rng}）：{e}[/]"),
    "log.candle": ("[cyan]candle @ {t}[/]  ${c} {arrow} (open ${o})",
                   "[cyan]K 線 @ {t}[/]  ${c} {arrow}（開盤 ${o}）"),

    # ── views / orders ────────────────────────────────────────────────────────
    "view.positions": ("POSITIONS", "持倉"),
    "view.orders": ("OPEN ORDERS", "掛單"),
    "view.orders_hint": ("  x=cancel row  X=cancel all", "  x=取消該筆  X=取消全部"),
    "log.view": ("[cyan]view → {label}{hint}[/]", "[cyan]檢視 → {label}{hint}[/]"),
    "log.orders_first": ("[yellow]switch to orders view first (press 'v')[/]",
                         "[yellow]請先切換至掛單檢視（按 'v'）[/]"),
    "log.no_order_sel": ("[yellow]no order selected[/]", "[yellow]未選擇掛單[/]"),
    "confirm.cancel_order": ("Cancel order {sym} ({id}…)?",
                             "取消掛單 {sym}（{id}…）？"),
    "log.cancelled": ("[green]cancelled order {sym} ({id}…)[/]",
                      "[green]已取消掛單 {sym}（{id}…）[/]"),
    "log.cancel_failed": ("[red]cancel failed for {id}…[/]",
                          "[red]取消失敗：{id}…[/]"),
    "confirm.cancel_all": ("Cancel ALL {n} open orders?", "取消全部 {n} 筆掛單？"),
    "log.no_orders": ("[yellow]no open orders to cancel[/]",
                      "[yellow]沒有可取消的掛單[/]"),
    "log.cancel_all": ("[green]cancel-all sent — {n} orders[/]",
                       "[green]已送出全部取消 — {n} 筆[/]"),

    # ── dry-run / report ──────────────────────────────────────────────────────
    "dry.head": ("[cyan]RS ranking — SPY {spy}%  (top {top} = LONG):[/]",
                 "[cyan]RS 排名 — SPY {spy}%（前 {top} 名 = 做多）：[/]"),
    "log.dry_err": ("[red]dry-run error: {e}[/]", "[red]模擬排名錯誤：{e}[/]"),
    "log.report_building": (
        "[cyan]building report → Telegram… (this can take ~20-30s)[/]",
        "[cyan]產生報告 → Telegram…（約需 20-30 秒）[/]"),
    "log.report_ok": ("[green]✓ report pushed to Telegram[/]",
                      "[green]✓ 報告已推送至 Telegram[/]"),
    "log.report_not_sent": ("[yellow]report built but not sent[/]",
                            "[yellow]報告已產生但未送出[/]"),
    "log.report_err": ("[red]report error: {e}[/]", "[red]報告錯誤：{e}[/]"),
    "log.report_auto": (
        "[cyan]auto-report window reached — pushing scheduled report[/]",
        "[cyan]已到自動報告時段 — 推送排程報告[/]"),

    # ── multi-wallet Telegram report body (wallet_report.py) ─────────────────
    "rpt.title": ("📊 *Alpaca Wallets Report*", "📊 *Alpaca 錢包總報告*"),
    "rpt.leaderboard": ("🏆 *LEADERBOARD* (vs $100k)",
                        "🏆 *排行榜*（對比 $100k）"),
    "rpt.combined": ("*ALL WALLETS ({n})*", "*全部錢包（{n}）*"),
    "rpt.equity": ("Equity", "總資產"),
    "rpt.day": ("Day", "本日"),
    "rpt.vs_base": ("vs base", "對比本金"),
    "rpt.up_down": ("{up}▲ {down}▼", "{up}▲ {down}▼"),
    "rpt.trades": ("Trades today", "今日交易"),
    "rpt.no_trades": ("no trades today", "今日無交易"),
    "rpt.all_cash": ("all cash — no positions", "全部現金 — 無持倉"),
    "rpt.err": ("⚠ {name}: {err}", "⚠ {name}：{err}"),
    "rpt.footer": ("_Alpaca Paper Trader · multi-wallet report_",
                   "_Alpaca 模擬交易 · 多錢包報告_"),

    # ── trading actions ───────────────────────────────────────────────────────
    "confirm.flatten": ("Flatten ALL positions to cash?", "將全部持倉平倉為現金？"),
    "log.flatten_sent": ("[red]FLATTEN sent — {n} positions[/]",
                         "[red]已送出全部平倉 — {n} 檔[/]"),
    "log.flatten_err": ("[red]flatten error: {e}[/]", "[red]平倉錯誤：{e}[/]"),
    "confirm.tick": ("Run a LIVE intraday tick now?", "立即執行一輪實際盤中交易？"),
    "log.tick_done": ("[cyan]intraday tick complete[/]", "[cyan]盤中交易一輪完成[/]"),
    "log.tick_err": ("[red]tick error: {e}[/]", "[red]盤中執行錯誤：{e}[/]"),
    "confirm.capitol": ("Run a LIVE Capitol Copier cycle now?",
                        "立即執行一輪實際國會跟單？"),
    "log.capitol_done": ("[cyan]Capitol run complete[/]", "[cyan]國會跟單完成[/]"),
    "log.capitol_err": ("[red]capitol error: {e}[/]", "[red]國會跟單錯誤：{e}[/]"),
    "confirm.buy": ("BUY {sym}  ${amt}?", "買入 {sym}  ${amt}？"),
    "confirm.buy_shares": ("BUY {sym}  {qty} shares ≈ ${est}? (whole-share asset)",
                           "買入 {sym}  {qty} 股 ≈ ${est}？（此標的僅限整股）"),
    "buy.err_unknown": ("unknown or untradable symbol", "代號不存在或不可交易"),
    "buy.rule_checking": ("[dim]checking trading rules…[/]", "[dim]檢查交易規則中…[/]"),
    "buy.rule_unknown": ("[b red]✗ {sym}: unknown or NOT tradable on Alpaca — order would be rejected[/]",
                         "[b red]✗ {sym}：代號不存在或 Alpaca 不可交易 — 委託將被拒絕[/]"),
    "buy.rule_frac": ("[green]✓ {sym}: tradable · fractional OK — $ amounts accepted[/]",
                      "[green]✓ {sym}：可交易 · 可碎股 — 可直接用金額下單[/]"),
    "buy.rule_whole": ("[yellow]✓ {sym}: tradable · ⚠ WHOLE SHARES ONLY @ ${price}/sh — your $ converts to whole shares[/]",
                       "[yellow]✓ {sym}：可交易 · ⚠ 僅限整股 @ ${price}/股 — 金額將自動換算為整股數[/]"),
    "buy.err_whole": (
        "[red]{sym} trades in whole shares only — one share costs ${price}, "
        "more than your ${amt}[/]",
        "[red]{sym} 僅限整股交易 — 每股 ${price}，超過您輸入的 ${amt}[/]"),
    "confirm.sell_all": ("SELL ALL of {sym}?", "全數賣出 {sym}？"),
    "confirm.sell_amt": ("SELL ${amt} of {sym}?", "賣出 {sym} ${amt}？"),
    "log.no_pos_sel": ("[yellow]no position selected[/]", "[yellow]未選擇持倉[/]"),
    "log.nothing_sell": ("[yellow]nothing to sell (amount 0)[/]",
                         "[yellow]無可賣出（金額為 0）[/]"),
    "log.no_pos": ("[yellow]no {sym} position to sell[/]",
                   "[yellow]無 {sym} 持倉可賣[/]"),
    "log.order_sent": ("[green]{side} {sym} sent → {id}[/]",
                       "[green]已送出 {side} {sym} → {id}[/]"),
    "log.order_err": ("[red]order error: {e}[/]", "[red]下單錯誤：{e}[/]"),
    "log.order_rejected": (
        "[b red]{side} {sym} REJECTED by Alpaca — {reason}[/]",
        "[b red]{side} {sym} 遭 Alpaca 拒絕 — {reason}[/]"),

    # ── rebalance ─────────────────────────────────────────────────────────────
    "log.reb_both": ("[red]pick ONE: deploy OR withdraw[/]",
                     "[red]只能擇一：投入或提出[/]"),
    "log.reb_cap": (
        "[yellow]deploy ${amt} > cash ${cash} — capping (no leverage)[/]",
        "[yellow]投入 ${amt} > 現金 ${cash} — 已封頂（不使用槓桿）[/]"),
    "log.reb_nothing": (
        "[yellow]only {n} long positions at top {top}, nothing to do[/]",
        "[yellow]僅有 {n} 檔多頭持倉，保留前 {top} 名無事可做[/]"),
    "reb.sum_trim": (
        "Rebalance: SELL {sells} + TRIM {trims} kept to raise ${raised} cash?",
        "再平衡：賣出 {sells} 檔＋修剪保留的 {trims} 檔，籌集 ${raised} 現金？"),
    "reb.sum_deploy": (
        "Rebalance: SELL {sells}, deploy ${total} (${freed} proceeds{cashbit}) "
        "into top {buys}?",
        "再平衡：賣出 {sells} 檔，將 ${total}（賣出所得 ${freed}{cashbit}）"
        "投入前 {buys} 名？"),
    "reb.cashbit": (" + ${amt} idle cash", "＋閒置現金 ${amt}"),
    "log.reb_plan_err": ("[red]rebalance plan error: {e}[/]",
                         "[red]再平衡規劃錯誤：{e}[/]"),
    "log.reb_err": ("[red]rebalance error: {e}[/]", "[red]再平衡錯誤：{e}[/]"),
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {k: v[0] for k, v in _TABLE.items()},
    "zh_TW": {k: v[1] for k, v in _TABLE.items()},
}

# ── strategy-config field text (config_fields.py stays the English source) ────
# path -> (label_zh, desc_zh); section name -> zh
_SECTION_ZH: dict[str, str] = {
    "MASTER": "主開關",
    "RISK": "風險",
    "EXITS": "出場",
    "SWING": "波段",
    "COPIER": "跟單",
    "SCHED": "排程",
}

_FIELD_ZH: dict[str, tuple[str, str]] = {
    "swing.enabled": (
        "波段買入 開/關",
        "每日 RS 動能買入＋逢低加碼。排程器於下一週期套用。"),
    "capitol_copier.autorun_enabled": (
        "國會自動跟單 開/關",
        "出場引擎（每 20 分鐘停損/移動停利/停利/加碼）＋每日申報跟單。"),
    "pool.max_total_exposure_pct": (
        "最大曝險（權益比例）",
        "投入市值的硬上限。1.00 = 全額投入（融資邊緣）。"),
    "swing.min_cash_reserve_usd": (
        "現金保留底線 $",
        "波段買入不會花到低於此現金緩衝。"),
    "pool.max_position_usd": (
        "單一持倉上限 $",
        "任一持倉經買入/加碼後市值不得超過此值。"),
    "pool.min_position_usd": (
        "最小下單金額 $",
        "小於此金額的訂單將被略過。"),
    "dynamic_exits.stop_loss_pct": (
        "停損（比例）",
        "未實現虧損達此值即全部賣出（0.08 = -8%）。"),
    "dynamic_exits.trail_trigger_pct": (
        "移動停利觸發（比例）",
        "峰值獲利達此值後啟動移動停利。"),
    "dynamic_exits.trail_giveback_pct": (
        "移動停利回吐（比例）",
        "觸發後，價格自峰值回落達此幅度即賣出。"),
    "dynamic_exits.pyramid_add_frac": (
        "金字塔加碼比例（原始部位）",
        "每層金字塔加碼原始部位的此比例。"),
    "dynamic_exits.max_holdings": (
        "最大持倉數（出場引擎）",
        "尾端封頂：超過此數量將於下一週期賣出最小的持倉。"),
    "dynamic_exits.prune_off_target": (
        "剔除非目標產業持倉",
        "危險：啟用後下一週期將賣出所有不在 target_sectors 的持倉。"),
    "swing.entry_size_usd": (
        "新進場金額 $",
        "每筆新 RS 動能進場的名目金額。"),
    "swing.max_new_positions_per_run": (
        "每日最多新進場數",
        "0 = 暫停新進場，但保留逢低加碼。"),
    "swing.rs_lookback_days": (
        "RS 回看天數",
        "相對強度排名（對比 SPY）的視窗。"),
    "swing.regime_sma_days": (
        "大盤均線天數",
        "SPY 收盤低於此均線時不進行新買入。"),
    "swing.dip_add_usd": (
        "逢低加碼金額 $",
        "回檔時對強勢股加碼的名目金額。0 = 停用逢低加碼。"),
    "swing.dip_min_peak_gain": (
        "逢低：最低峰值漲幅（比例）",
        "持倉峰值漲幅須達此值才符合加碼資格。"),
    "swing.dip_trigger_off_peak": (
        "逢低：自峰值回檔（比例）",
        "……且自峰值回落至少此幅度（仍高於進場價）。"),
    "swing.dip_cooldown_days": (
        "逢低加碼冷卻（天）",
        "每檔每隔此天數最多一次逢低加碼。"),
    "swing.stop_cooldown_days": (
        "停損後再買冷卻（天）",
        "在此期間內不回補被出場引擎停損的股票。"),
    "swing.max_holdings": (
        "最大持倉數（波段買入）",
        "超過此數量後波段買入不再開新倉。"),
    "pool.daily_budget_usd": (
        "每日跟單預算 $",
        "跟單申報時依權重分配的基礎預算。"),
    "pool.consensus_boost_multiplier": (
        "共識加碼倍數",
        "14 天內 2 位以上成員買入同一股票時的加碼倍數。"),
    "capitol_copier.max_disclosure_lag_days": (
        "申報最大延遲（天）",
        "略過超過此天數的申報 — 過期資訊沒有優勢。"),
    "capitol_copier.sentiment_veto_enabled": (
        "情緒否決",
        "開啟時，1/5 看空的 LLM 情緒會擋下買入（關閉 = 僅縮小金額）。"),
    "capitol_copier.target_sectors": (
        "目標產業（csv）",
        "新跟單買入的白名單。"),
    "trading_schedule.manage_every_minutes": (
        "出場引擎頻率（分鐘）",
        "盤中檢查停損/移動停利/停利的間隔。"),
    "trading_schedule.swing_time_et": (
        "波段買入時間（美東）",
        "每日波段買入視窗起點，HH:MM 24 小時制（美東）。"),
    "trading_schedule.copy_time_et": (
        "跟單時間（美東）",
        "每日申報跟單視窗起點，HH:MM 24 小時制（美東）。"),
    "trading_schedule.window_minutes": (
        "每日視窗寬度（分鐘）",
        "波段/跟單觸發視窗的寬度。"),
    "trading_schedule.weekdays_only": (
        "僅平日",
        "週六/週日完全跳過。"),
}

# ── state + API ───────────────────────────────────────────────────────────────
_lang = "en"


def set_lang(code: str) -> None:
    global _lang
    _lang = code if code in STRINGS else "en"


def get_lang() -> str:
    return _lang


def next_lang(code: str | None = None) -> str:
    cur = code if code in LANGS else _lang
    return LANGS[(LANGS.index(cur) + 1) % len(LANGS)]


def lang_label(code: str | None = None) -> str:
    return {"en": "English", "zh_TW": "繁體中文"}.get(code or _lang, code or _lang)


def t(key: str, **fmt) -> str:
    """Translate a catalog key in the current language (en fallback, then the
    key itself). kwargs are .format()-substituted into {placeholders}."""
    s = STRINGS.get(_lang, {}).get(key) or STRINGS["en"].get(key) or key
    return s.format(**fmt) if fmt else s


def section_text(section: str) -> str:
    """Localized section name for the strategy-config table."""
    if _lang == "zh_TW":
        return _SECTION_ZH.get(section, section)
    return section


def field_text(path: str, attr: str) -> str | None:
    """Localized label/desc for a config_fields Field, or None to use the
    English text already on the Field (the fallback source of truth)."""
    if _lang != "zh_TW":
        return None
    pair = _FIELD_ZH.get(path)
    if not pair:
        return None
    return pair[0] if attr == "label" else pair[1]
