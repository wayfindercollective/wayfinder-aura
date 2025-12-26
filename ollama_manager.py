"""
Ollama Manager for Wayfinder Voice.
Handles detection, installation, and service management of Ollama.
Works on SteamOS, Bazzite, and standard Linux distributions.
"""

import os
import subprocess
import threading
import shutil
from pathlib import Path
from typing import Optional, Callable, Tuple, List, Dict
import time


class OllamaManager:
    """Manages Ollama installation and service lifecycle."""
    
    # Ollama install script URL
    INSTALL_SCRIPT_URL = "https://ollama.com/install.sh"
    
    # Common Ollama binary locations
    OLLAMA_PATHS = [
        "/usr/local/bin/ollama",
        "/usr/bin/ollama",
        "/var/usrlocal/bin/ollama",  # Bazzite/immutable systems
        str(Path.home() / ".local" / "bin" / "ollama"),
    ]
    
    # Recommended models for post-processing (small and fast)
    RECOMMENDED_MODELS = [
        {
            "name": "smollm2:360m",
            "display_name": "SmolLM2 360M",
            "size": "230 MB",
            "description": "Tiny and instant. Best for simple cleanup.",
            "speed": "Instant",
            "quality": "Basic",
        },
        {
            "name": "llama3.2:1b",
            "display_name": "Llama 3.2 1B",
            "size": "1.3 GB",
            "description": "Meta's efficient model. Good all-rounder.",
            "speed": "Very Fast",
            "quality": "Good",
        },
        {
            "name": "phi3:mini",
            "display_name": "Phi-3 Mini",
            "size": "2.2 GB",
            "description": "Microsoft's powerhouse. Excellent quality.",
            "speed": "Fast",
            "quality": "High",
            "recommended": True,
        },
        {
            "name": "qwen2.5:1.5b",
            "display_name": "Qwen2.5 1.5B",
            "size": "986 MB",
            "description": "Great speed/quality balance.",
            "speed": "Very Fast",
            "quality": "Good",
        },
    ]
    
    def __init__(self):
        self._ollama_path: Optional[str] = None
        self._service_process: Optional[subprocess.Popen] = None
        self._install_cancel = False
    
    # =========================================================================
    # Detection
    # =========================================================================
    
    def find_ollama_binary(self) -> Optional[str]:
        """Find the Ollama binary on the system."""
        # First check if it's in PATH
        ollama_in_path = shutil.which("ollama")
        if ollama_in_path:
            self._ollama_path = ollama_in_path
            return ollama_in_path
        
        # Check known locations
        for path in self.OLLAMA_PATHS:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                self._ollama_path = path
                return path
        
        return None
    
    def is_installed(self) -> bool:
        """Check if Ollama is installed."""
        return self.find_ollama_binary() is not None
    
    def is_service_running(self) -> bool:
        """Check if Ollama service is running and responding."""
        try:
            import requests
            response = requests.get("http://localhost:11434/api/tags", timeout=2)
            return response.status_code == 200
        except:
            return False
    
    def get_version(self) -> Optional[str]:
        """Get the installed Ollama version."""
        binary = self.find_ollama_binary()
        if not binary:
            return None
        
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Parse "ollama version 0.1.xx"
                output = result.stdout.strip()
                if "version" in output:
                    return output.split("version")[-1].strip()
                return output
        except:
            pass
        return None
    
    def get_status(self) -> Dict:
        """Get comprehensive Ollama status."""
        installed = self.is_installed()
        running = self.is_service_running() if installed else False
        version = self.get_version() if installed else None
        models = self.list_models() if running else []
        
        return {
            "installed": installed,
            "running": running,
            "version": version,
            "binary_path": self._ollama_path,
            "models": models,
            "model_count": len(models),
        }
    
    # =========================================================================
    # Installation
    # =========================================================================
    
    def install(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
        complete_callback: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        """
        Install Ollama using the official install script.
        Runs in a background thread.
        
        Args:
            progress_callback: Called with (status_message, progress_0_to_1)
            complete_callback: Called with (success, message) when done
        """
        self._install_cancel = False
        
        def install_thread():
            try:
                if progress_callback:
                    progress_callback("Downloading Ollama installer...", 0.1)
                
                # Check if already installed
                if self.is_installed():
                    if complete_callback:
                        complete_callback(True, "Ollama is already installed!")
                    return
                
                if self._install_cancel:
                    if complete_callback:
                        complete_callback(False, "Installation cancelled")
                    return
                
                if progress_callback:
                    progress_callback("Running installer (may ask for password)...", 0.3)
                
                # Run the install script
                # We use curl to download and pipe to sh
                # This requires sudo, so it will prompt for password
                process = subprocess.Popen(
                    ["bash", "-c", f"curl -fsSL {self.INSTALL_SCRIPT_URL} | sh"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                
                output_lines = []
                while True:
                    if self._install_cancel:
                        process.terminate()
                        if complete_callback:
                            complete_callback(False, "Installation cancelled")
                        return
                    
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    
                    if line:
                        output_lines.append(line.strip())
                        # Update progress based on output
                        if "Downloading" in line:
                            if progress_callback:
                                progress_callback("Downloading Ollama...", 0.5)
                        elif "Installing" in line:
                            if progress_callback:
                                progress_callback("Installing Ollama...", 0.7)
                        elif "complete" in line.lower():
                            if progress_callback:
                                progress_callback("Installation complete!", 0.9)
                
                returncode = process.wait()
                
                if returncode == 0 and self.is_installed():
                    if progress_callback:
                        progress_callback("Ollama installed successfully!", 1.0)
                    if complete_callback:
                        complete_callback(True, "Ollama installed successfully!")
                else:
                    error_msg = "\n".join(output_lines[-5:]) if output_lines else "Unknown error"
                    if complete_callback:
                        complete_callback(False, f"Installation failed: {error_msg}")
                        
            except Exception as e:
                if complete_callback:
                    complete_callback(False, f"Installation error: {str(e)}")
        
        threading.Thread(target=install_thread, daemon=True).start()
    
    def cancel_install(self):
        """Cancel an ongoing installation."""
        self._install_cancel = True
    
    # =========================================================================
    # Service Management
    # =========================================================================
    
    def start_service(
        self,
        callback: Optional[Callable[[bool, str], None]] = None
    ) -> bool:
        """
        Start the Ollama service.
        
        Args:
            callback: Called with (success, message) when service starts or fails
            
        Returns:
            True if service started successfully
        """
        if self.is_service_running():
            if callback:
                callback(True, "Ollama is already running")
            return True
        
        binary = self.find_ollama_binary()
        if not binary:
            if callback:
                callback(False, "Ollama is not installed")
            return False
        
        def start_thread():
            try:
                # Start ollama serve in background with proper pipe handling
                # Use PIPE for stderr to detect errors, but don't block on it
                self._service_process = subprocess.Popen(
                    [binary, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,  # Detach from parent process group
                )
                
                # Wait for service to become available with early exit on process death
                for i in range(30):  # Wait up to 30 seconds
                    # Check if process died
                    poll_result = self._service_process.poll()
                    if poll_result is not None:
                        # Process exited - try to read stderr for error message
                        try:
                            stderr_output = self._service_process.stderr.read().decode('utf-8', errors='ignore')
                            if "address already in use" in stderr_output.lower():
                                # Another Ollama is already running - that's fine!
                                if self.is_service_running():
                                    if callback:
                                        callback(True, "Ollama service is running")
                                    return
                            error_msg = stderr_output.strip()[:200] if stderr_output.strip() else f"exit code {poll_result}"
                        except Exception:
                            error_msg = f"exit code {poll_result}"
                        if callback:
                            callback(False, f"Ollama failed to start: {error_msg}")
                        return
                    
                    time.sleep(1)
                    if self.is_service_running():
                        if callback:
                            callback(True, "Ollama service started")
                        return
                
                if callback:
                    callback(False, "Ollama service failed to start (timeout)")
                    
            except Exception as e:
                if callback:
                    callback(False, f"Failed to start Ollama: {str(e)}")
        
        threading.Thread(target=start_thread, daemon=True).start()
        return True
    
    def stop_service(self) -> bool:
        """Stop the Ollama service if we started it."""
        if self._service_process:
            self._service_process.terminate()
            self._service_process = None
            return True
        return False
    
    # =========================================================================
    # Model Management
    # =========================================================================
    
    def list_models(self) -> List[str]:
        """Get list of installed Ollama models."""
        if not self.is_service_running():
            return []
        
        try:
            import requests
            response = requests.get("http://localhost:11434/api/tags", timeout=5)
            if response.status_code == 200:
                data = response.json()
                return [m["name"] for m in data.get("models", [])]
        except:
            pass
        return []
    
    def pull_model(
        self,
        model_name: str,
        progress_callback: Optional[Callable[[str, float], None]] = None,
        complete_callback: Optional[Callable[[bool, str], None]] = None,
    ) -> None:
        """
        Pull (download) an Ollama model.
        
        Args:
            model_name: Name of the model (e.g., "smollm2:360m")
            progress_callback: Called with (status, progress_0_to_1)
            complete_callback: Called with (success, message) when done
        """
        if not self.is_service_running():
            if complete_callback:
                complete_callback(False, "Ollama service is not running")
            return
        
        def pull_thread():
            try:
                import requests
                import json
                
                if progress_callback:
                    progress_callback(f"Starting download of {model_name}...", 0.0)
                
                response = requests.post(
                    "http://localhost:11434/api/pull",
                    json={"name": model_name},
                    stream=True,
                    timeout=(30, 1800),  # 30s connect, 30min read
                )
                
                if response.status_code != 200:
                    if complete_callback:
                        complete_callback(False, f"Failed to pull model: HTTP {response.status_code}")
                    return
                
                for line in response.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            status = data.get("status", "")
                            
                            if "total" in data and "completed" in data:
                                total = data["total"]
                                completed = data["completed"]
                                if total > 0:
                                    progress = completed / total
                                    size_mb = completed / (1024 * 1024)
                                    total_mb = total / (1024 * 1024)
                                    if progress_callback:
                                        progress_callback(
                                            f"Downloading: {size_mb:.0f} / {total_mb:.0f} MB",
                                            progress
                                        )
                            elif status:
                                if progress_callback:
                                    progress_callback(status, -1)  # Indeterminate
                                    
                        except json.JSONDecodeError:
                            pass
                
                # Verify model was installed
                if model_name.split(":")[0] in " ".join(self.list_models()):
                    if complete_callback:
                        complete_callback(True, f"Successfully installed {model_name}")
                else:
                    if complete_callback:
                        complete_callback(True, f"Model {model_name} ready")
                        
            except Exception as e:
                if complete_callback:
                    complete_callback(False, f"Failed to pull model: {str(e)}")
        
        threading.Thread(target=pull_thread, daemon=True).start()
    
    def delete_model(self, model_name: str) -> Tuple[bool, str]:
        """Delete an Ollama model."""
        if not self.is_service_running():
            return False, "Ollama service is not running"
        
        try:
            import requests
            response = requests.delete(
                "http://localhost:11434/api/delete",
                json={"name": model_name},
                timeout=30,
            )
            if response.status_code == 200:
                return True, f"Deleted {model_name}"
            else:
                return False, f"Failed to delete: HTTP {response.status_code}"
        except Exception as e:
            return False, f"Failed to delete: {str(e)}"
    
    # =========================================================================
    # Utility
    # =========================================================================
    
    def get_recommended_models(self) -> List[Dict]:
        """Get list of recommended models with install status."""
        installed = self.list_models()
        models = []
        
        for model in self.RECOMMENDED_MODELS:
            model_copy = model.copy()
            # Check if this model (or any variant) is installed
            model_copy["installed"] = any(
                model["name"].split(":")[0] in m for m in installed
            )
            models.append(model_copy)
        
        return models


# Singleton instance
_manager: Optional[OllamaManager] = None

def get_ollama_manager() -> OllamaManager:
    """Get the singleton OllamaManager instance."""
    global _manager
    if _manager is None:
        _manager = OllamaManager()
    return _manager



