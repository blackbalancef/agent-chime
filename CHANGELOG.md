# Changelog

## 0.1.0 - Unreleased

### Added
- Global stop-speaking hotkey. While the menu bar app runs, a system-wide keyboard shortcut (default `⌥⌘S`) instantly silences the current announcement from any app — handy on a meeting or when you've already read the message. It uses Carbon's `RegisterEventHotKey`, so no Accessibility/Input-Monitoring permission is needed and only the chosen combo is captured. Configure it three ways: the new `Stop hotkey` submenu in the menu bar, a picker during `voiccce setup` (or `--hotkey`), or `voiccce config --hotkey "ctrl+alt+cmd+."` (`--hotkey off` disables it). Lives under `[hotkey]` in `config.toml`.
- Free-form notification language setting for AI summaries. `voiccce setup` now asks for a language name, `voiccce setup --language Spanish` and `voiccce config --language Spanish` persist it, and the menu bar exposes a `Notification language` entry.

### Changed
- `voiccce setup` is now a guided interactive wizard. It gathers every choice up front before doing any work: (1) a checkbox picker for which agents to wire (Claude Code, Codex, pi), (2) a text prompt for the AI-summary language, (3) a new voice picker to choose OpenAI TTS or the offline macOS voice, and (4) a yes/no prompt for the menu bar app. `↑`/`↓` navigate, `space` toggles, `a` selects all, `enter` confirms, `esc` cancels. Each menu is skipped when the matching flag is passed: a target (`claude-code`/`codex`/`pi`/`both`), `--language`, `--openai`/`--local`, or `--menubar`/`--no-menubar`. The new `--openai` flag forces OpenAI TTS without showing the voice picker.

### Fixed
- pi integration now honors `PI_CODING_AGENT_DIR`, so alternate profiles such as `pi-personal` (which runs `PI_CODING_AGENT_DIR=$HOME/.pi-personal/agent pi`) get the extension installed into their own extensions directory instead of always `~/.pi/agent/extensions/`. Without this, voiccce stayed silent for pi-personal because pi never discovered the extension.



- Local SQLite event queue and daemon.
- Claude Code hook collector and personal settings installer.
- Session-aware notification state with deduplication, grouping, and stale/conflicting event suppression.
- English notification text with configurable templates.
- macOS `say`, desktop notification, terminal, and OpenAI TTS delivery.
- Local secret loading from environment variables, `~/.voiccce/.env`, or macOS Keychain.
- Optional macOS menu bar companion with stop-speaking, mute, daemon controls, and quick access to config/logs.
- Runtime voice controls: `stop-speaking`, `mute`, and `unmute`.
- `stop-speaking` now cancels pending OpenAI TTS playback and terminates voice playback process groups.
