"""Dialog windows for Wayfinder Aura.

NOTE: the first-run SetupWizard (a modal CTkToplevel) was removed in July 2026 —
first-run dependency setup is now an in-window pane (``wayfinder.ui.setup_pane``),
in line with CLAUDE.md rule 2 (no popup dialogs). This package currently exports
nothing; it remains as a home for any future genuinely-windowed helpers.

Future extraction targets (currently in wayfinder_main.py) would each become an
inline CTkFrame panel (via ``_show_inline_panel``), not a Toplevel:
- settings, hotkey, model_download, benchmark, license.
"""

__all__: list[str] = []
