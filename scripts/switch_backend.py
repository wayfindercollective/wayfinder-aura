#!/usr/bin/env python3
"""
Quick helper to switch transcription backends.

Usage:
    python scripts/switch_backend.py local      # Use whisper.cpp (default)
    python scripts/switch_backend.py groq       # Use Groq API (ultra-fast)
    python scripts/switch_backend.py status     # Show current backend
"""

import json
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "wayfinder-aura" / "config.json"


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def switch_to_local():
    """Switch to local whisper.cpp backend."""
    config = load_config()
    config["transcription_backend"] = "whisper_cpp"
    save_config(config)
    print("✅ Switched to LOCAL (whisper.cpp)")
    print("   • Uses your GPU (Vulkan)")
    print("   • ~0.5s for 10s audio")
    print("   • 100% private, no internet needed")


def switch_to_groq():
    """Switch to Groq API backend."""
    import os
    
    api_key = os.environ.get("GROQ_API_KEY", "")
    
    if not api_key:
        print("❌ GROQ_API_KEY not set!")
        print("\nTo get a free API key:")
        print("  1. Go to: https://console.groq.com/keys")
        print("  2. Sign up/login (free)")
        print("  3. Create an API key")
        print("  4. Add to ~/.bashrc:")
        print("     export GROQ_API_KEY='gsk_your_key_here'")
        print("  5. Run: source ~/.bashrc")
        print("  6. Try again: python scripts/switch_backend.py groq")
        return
    
    config = load_config()
    config["transcription_backend"] = "groq_whisper"
    save_config(config)
    print("✅ Switched to GROQ (cloud API)")
    print("   • Ultra-fast: ~0.3s for 10s audio")
    print("   • Same Whisper Large-v3 quality")
    print("   • Requires internet connection")
    print(f"   • API key: {api_key[:10]}...")


def show_status():
    """Show current backend status."""
    import os
    
    config = load_config()
    backend = config.get("transcription_backend", "whisper_cpp")
    
    print("=== Transcription Backend Status ===\n")
    
    backends = {
        "whisper_cpp": ("🔒 Local (whisper.cpp)", "Vulkan GPU, ~0.5s/10s audio"),
        "groq_whisper": ("☁️  Groq API", "Ultra-fast, ~0.3s/10s audio"),
        "faster_whisper": ("🔒 Local (Faster-Whisper)", "ROCm/CUDA GPU"),
        "openai_whisper": ("☁️  OpenAI API", "Cloud, reliable"),
    }
    
    for key, (name, desc) in backends.items():
        marker = "→" if key == backend else " "
        print(f"  {marker} {name}")
        print(f"      {desc}")
        if key == backend:
            print(f"      ✅ ACTIVE")
        print()
    
    # Check Groq API key
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        print(f"Groq API key: ✅ Set ({groq_key[:10]}...)")
    else:
        print("Groq API key: ❌ Not set")
        print("  Get one at: https://console.groq.com/keys")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nCurrent status:")
        show_status()
        return
    
    cmd = sys.argv[1].lower()
    
    if cmd == "local":
        switch_to_local()
    elif cmd == "groq":
        switch_to_groq()
    elif cmd == "status":
        show_status()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
