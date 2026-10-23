# OpenCode Usage for SwiftBar · OpenCode 额度菜单栏 📊

Your OpenCode quota in the macOS menu bar: the 5-hour / weekly / monthly windows at a glance, with a heads-up before you run out ⏰

![Menu preview](screenshot.png)

[中文说明](README.zh-CN.md)

## ✨ Features

- 📊 **Live quota for all three windows** — a 50-tick meter per window, colour-coded 🟢 green → 🟠 amber at 60% → 🔴 red at 85%, with the exact percentage in a pill on the right
- 🔔 **Heads-up before you run out** — a macOS notification when any window crosses 85%; once per window per cycle, never a barrage
- 🏃 **Pace warning** — burning a window faster than the clock turns it red and marks it "ahead of pace"
- ⚡ **Zen (pay as you go) detected automatically** — a key with no Go windows switches to the Zen card
- 🔑 **Multiple keys, with names** — call them "personal" and "work", switch from the menu
- 🌍 **Seven languages** — 简体中文 / 繁體中文 / English / 日本語 / 한국어 / Español / Français, applied instantly
- ☁️ **Offline-proof** — when a read fails, the last good reading stays on screen, labelled with its age

## 📦 Requirements

- 🍎 macOS + [SwiftBar](https://github.com/swiftbar/SwiftBar)
- 🐍 python3 (optional Pillow: without it the menu bar icon falls back to plain text, everything else works)

## 🚀 Install

```bash
git clone https://github.com/Nick0112a/opencode-usage-swiftbar.git
cd opencode-usage-swiftbar
./install.sh
```

## 🪄 First run

1. 🖱️ Click the new menu bar item
2. 📋 "API Key ▸ Add key ▸ From clipboard…" — copy your key from the OpenCode console first
3. ✅ Done! It refreshes itself every 5 minutes

## 🖥️ The menu

| Item | What it does |
|---|---|
| 📊 Three windows + meters | The percentage sits in the pill on the right; click any row to open the OpenCode dashboard |
| 🔑 API Key ▸ | Every saved key (ticked = live); rename, add or remove them |
| 🌍 Language ▸ | Seven languages, applied instantly |
| 🎛️ Open dashboard / Refresh | Jump to the OpenCode console / force a refresh |

## ❓ FAQ

- **Why can't I see my Zen balance?** OpenCode exposes no balance endpoint — it's web-console only 🌐
- **Where do the dollar caps come from?** The documented standard Go limits ($12 / $30 / $60); the API only returns percentages, and Go Plus differs — hence the ≈
- **No notification?** macOS asks for permission the first time one fires 🔔
- **Why doesn't the meter reach the menu edge?** That last bit is the menu's own margin

## ⚠️ Known limits

- Built on two undocumented endpoints the web console uses; if their shape changes you'll see the cached reading and an explicit error, never a crash
- The dollar caps are documentation values; the API only returns percentages
- macOS only

Before hacking on the plugin, read [DEVELOPMENT.md](DEVELOPMENT.md) — it documents the SwiftBar behaviours that are easy to walk back into.

## 📄 License

MIT
