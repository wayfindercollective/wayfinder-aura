#!/usr/bin/env python3
"""
Test Groq Whisper API transcription.

Usage:
    1. Set your API key: export GROQ_API_KEY='gsk_your_key'
    2. Run: python scripts/test_groq.py [audio_file.wav]
    
If no audio file is provided, records a 5-second sample.
"""

import os
import sys
import time
import tempfile

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from wayfinder.core.transcriber import GroqWhisperBackend
from wayfinder.core.recorder import AudioRecorder


def test_groq_transcription(audio_path: str = None):
    """Test Groq Whisper transcription."""
    
    # Check API key
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        print("❌ GROQ_API_KEY not set!")
        print("\nTo get a free API key:")
        print("  1. Go to: https://console.groq.com/keys")
        print("  2. Sign up/login")
        print("  3. Create an API key")
        print("  4. Run: export GROQ_API_KEY='gsk_your_key'")
        return False
    
    print(f"✅ API key found: {api_key[:10]}...")
    
    # Create backend
    backend = GroqWhisperBackend(
        api_key=api_key,
        model="whisper-large-v3",
        language="en",
    )
    
    if not backend.is_available():
        print("❌ Groq backend not available")
        print("   Install: pip install groq")
        return False
    
    print(f"✅ Backend: {backend.get_name()}")
    
    # Record or use provided audio
    if audio_path and os.path.exists(audio_path):
        print(f"\n🎤 Using audio file: {audio_path}")
    else:
        print("\n🎤 Recording 5 seconds of audio...")
        print("   Speak now!")
        
        recorder = AudioRecorder(sample_rate=16000)
        recorder.start()
        
        for i in range(5, 0, -1):
            time.sleep(1)
            level = recorder.get_audio_level()
            bar = "█" * int(level * 20)
            print(f"   {i}s remaining... {bar}")
        
        audio_path = recorder.stop()
        print(f"   ✅ Recorded to: {audio_path}")
    
    # Transcribe
    print("\n⚡ Transcribing with Groq...")
    
    start_time = time.time()
    try:
        result = backend.transcribe(audio_path)
        elapsed = time.time() - start_time
        
        print(f"\n✅ Transcription complete!")
        print(f"   Time: {elapsed:.2f}s")
        print(f"   Text: {result}")
        
        # Compare with local estimate
        file_size = os.path.getsize(audio_path)
        duration_estimate = file_size / (16000 * 2)  # 16kHz, 16-bit
        print(f"\n📊 Speed comparison:")
        print(f"   Audio duration: ~{duration_estimate:.1f}s")
        print(f"   Groq latency: {elapsed:.2f}s")
        print(f"   Real-time factor: {elapsed/duration_estimate:.2f}x")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Transcription failed: {e}")
        return False


if __name__ == "__main__":
    audio_file = sys.argv[1] if len(sys.argv) > 1 else None
    success = test_groq_transcription(audio_file)
    sys.exit(0 if success else 1)
