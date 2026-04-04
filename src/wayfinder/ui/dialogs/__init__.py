"""
Dialog windows for Wayfinder Aura.

Future extraction targets (currently in wayfinder_main.py):
- settings: Main settings dialog
- hotkey: Hotkey configuration
- model_download: Whisper model downloader
- benchmark: Performance benchmarking
- license: License activation

To extract a dialog:
1. Create the module (e.g., settings.py)
2. Move the relevant class/functions from wayfinder_main.py
3. Update imports in wayfinder_main.py to use the new module
4. Add exports to this __init__.py
"""

from .setup_wizard import SetupWizard

# Placeholder for future dialog extractions
# from .settings import SettingsDialog
# from .hotkey import HotkeyDialog
# from .model_download import ModelDownloadDialog
# from .benchmark import BenchmarkDialog
# from .license import LicenseDialog

__all__ = [
    "SetupWizard",
]
