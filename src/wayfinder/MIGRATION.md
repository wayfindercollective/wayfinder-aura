# Migration Status

This document tracks the migration from the monolithic `wayfinder_main.py` to the modular package structure.

## Current Status

The package structure is in place and all modules are functional. However, `wayfinder_main.py` still imports from the root-level files for backwards compatibility.

## Root Files (Legacy - Keep for Now)

These files are still imported by `wayfinder_main.py` and must remain in the root:

| File | Status | Package Location |
|------|--------|------------------|
| `recorder.py` | Duplicated | `src/wayfinder/core/recorder.py` |
| `transcriber.py` | Duplicated | `src/wayfinder/core/transcriber.py` |
| `injector.py` | Duplicated | `src/wayfinder/core/injector.py` |
| `postprocessor.py` | Duplicated | `src/wayfinder/core/postprocessor.py` |
| `ollama_manager.py` | Duplicated | `src/wayfinder/core/ollama_manager.py` |
| `license.py` | Duplicated | `src/wayfinder/license.py` |
| `status_overlay.py` | Duplicated | `src/wayfinder/ui/overlay.py` |
| `wayfinder_main.py` | Legacy main | To be broken into modules |

## To Complete the Migration

### Phase 1: Update wayfinder_main.py Imports (Safe)

Change imports in `wayfinder_main.py` from:
```python
from recorder import AudioRecorder
from transcriber import transcribe_with_config
```

To:
```python
from wayfinder.core import AudioRecorder
from wayfinder.core import transcribe_with_config
```

After this, the root-level core files can be deleted.

### Phase 2: Extract Dialogs from wayfinder_main.py

The following can be extracted to `src/wayfinder/ui/dialogs/`:
- Settings dialog → `settings.py`
- Hotkey dialog → `hotkey.py`
- Model download dialog → `model_download.py`
- Ollama setup wizard → `ollama_setup.py`
- Benchmark dialog → `benchmark.py`
- License dialog → `license.py`

### Phase 3: Extract Main Window and Tray

- Main window layout → `src/wayfinder/ui/main_window.py`
- System tray → `src/wayfinder/ui/tray.py`

### Phase 4: Slim Down wayfinder_main.py

After all extractions, `wayfinder_main.py` should contain only:
- WayfinderApp class (coordinating all components)
- Event loop and state management

## For AI Agents

When making changes:
1. **Prefer the package modules** in `src/wayfinder/` for new code
2. **Keep root files in sync** if you modify package modules (for now)
3. **Test both import paths** until migration is complete

```bash
# Test package imports
PYTHONPATH=src python -c "from wayfinder.core import AudioRecorder; print('OK')"

# Test root imports (used by wayfinder_main.py)
python -c "from recorder import AudioRecorder; print('OK')"
```



