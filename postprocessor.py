"""
Post-processing module for Wayfinder Voice.
Cleans up transcription output using LLM backends (local or cloud).
Supports llama-cpp-python for local inference and Anthropic Claude for cloud.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Dict, Any


class PostProcessingError(Exception):
    """Raised when post-processing fails."""
    pass


# =============================================================================
# Tone-Based Prompts (New Simplified System)
# =============================================================================

# Tone guidance for different output styles
TONE_GUIDANCE: Dict[str, str] = {
    "professional": "Use formal, polished, business-appropriate language. Be clear and concise.",
    "casual": "Use relaxed, conversational, friendly language. Keep it natural and approachable.",
    "technical": "Use precise, detailed, developer-focused language. Be accurate and specific.",
}

# Smart formatting prompt - auto-detects content type and formats appropriately
SMART_FORMAT_PROMPT = """You are a transcription cleanup assistant. Process this speech transcription with these rules:

1. Remove ALL filler words: um, uh, ah, like, you know, basically, actually, I mean, so, well, right
2. Fix punctuation and capitalization
3. AUTO-DETECT the content type and format appropriately:
   - If it reads like an email (mentions sending, recipients, subjects): format as email with greeting/sign-off
   - If it contains lists or multiple items: use bullet points
   - If it describes code or technical concepts: use documentation style
   - Otherwise: clean prose paragraphs

4. Tone: {tone_guidance}

IMPORTANT: Output ONLY the cleaned text. No explanations, no labels, no "Here is...".

Transcription:
{text}

Cleaned:"""

# Clean-only prompt - minimal processing, just removes fillers
CLEAN_ONLY_PROMPT = """Clean up this transcribed speech. Remove filler words (um, uh, ah, like, you know, basically, actually, I mean), fix punctuation and capitalization. Keep the exact structure and meaning - only clean up the delivery.

Tone: {tone_guidance}

IMPORTANT: Output ONLY the cleaned text. No explanations, no labels.

Transcription:
{text}

Cleaned:"""


def build_prompt(text: str, config: dict) -> str:
    """
    Build the appropriate prompt based on config settings.
    
    Args:
        text: The transcription text to process
        config: Configuration dictionary with style settings
        
    Returns:
        Formatted prompt ready for LLM
    """
    # Get tone guidance
    tone = config.get("output_tone", "professional")
    tone_guidance = TONE_GUIDANCE.get(tone, TONE_GUIDANCE["professional"])
    
    # Choose prompt based on smart formatting setting
    smart_formatting = config.get("smart_formatting", True)
    
    if smart_formatting:
        template = SMART_FORMAT_PROMPT
    else:
        template = CLEAN_ONLY_PROMPT
    
    # Fill in the template
    return template.format(tone_guidance=tone_guidance, text=text)


# Legacy compatibility - keeping for any external code that might use these
PROMPT_TEMPLATES: Dict[str, str] = {
    "clean": CLEAN_ONLY_PROMPT,
}


def get_prompt_template(template_name: str, custom_prompt: str = "") -> str:
    """Legacy function - now uses build_prompt internally."""
    return CLEAN_ONLY_PROMPT


def format_prompt(template: str, text: str) -> str:
    """Format a prompt template with the transcription text."""
    return template.replace("{text}", text)


# =============================================================================
# Abstract Backend
# =============================================================================

class PostProcessorBackend(ABC):
    """Abstract base class for post-processing backends."""
    
    @abstractmethod
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process transcribed text to clean it up.
        
        Args:
            text: The raw transcription text
            prompt_template: The formatted prompt to use
            
        Returns:
            Cleaned/formatted text
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is available/installed."""
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Return the display name of this backend."""
        pass


# =============================================================================
# llama-cpp-python Backend (Local)
# =============================================================================

class LlamaCppBackend(PostProcessorBackend):
    """
    Local LLM backend using llama-cpp-python.
    Recommended models: Phi-3-mini, Qwen2.5-1.5B-Instruct, SmolLM2-1.7B
    """
    
    # Class-level model cache to avoid reloading
    _model_cache: Dict[str, Any] = {}
    
    def __init__(
        self,
        model_path: str = "",
        n_ctx: int = 2048,
        n_threads: int = 4,
        n_gpu_layers: int = -1,  # -1 = auto (use all available)
        max_tokens: int = 1024,
        temperature: float = 0.1,
        use_gpu: bool = True,
    ):
        self.model_path = os.path.expanduser(model_path) if model_path else ""
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.n_gpu_layers = n_gpu_layers if use_gpu else 0
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._model = None
    
    def get_name(self) -> str:
        return "llama.cpp (Local)"
    
    def is_available(self) -> bool:
        """Check if llama-cpp-python is installed and model exists."""
        try:
            import llama_cpp
            if self.model_path:
                return Path(self.model_path).exists()
            return True  # Library is available, model path may be set later
        except ImportError:
            return False
    
    def _get_model(self):
        """Get or create the LLM model (cached)."""
        if self._model is not None:
            return self._model
        
        if not self.model_path or not Path(self.model_path).exists():
            raise PostProcessingError(
                f"Model file not found: {self.model_path}. "
                "Please download a GGUF model (e.g., Phi-3-mini, Qwen2.5-1.5B-Instruct)."
            )
        
        cache_key = (self.model_path, self.n_ctx, self.n_gpu_layers)
        if cache_key in LlamaCppBackend._model_cache:
            self._model = LlamaCppBackend._model_cache[cache_key]
            return self._model
        
        try:
            from llama_cpp import Llama
            
            self._model = Llama(
                model_path=self.model_path,
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
                n_gpu_layers=self.n_gpu_layers,
                verbose=False,
            )
            
            LlamaCppBackend._model_cache[cache_key] = self._model
            return self._model
            
        except Exception as e:
            raise PostProcessingError(f"Failed to load llama.cpp model: {e}")
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using local LLM.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            raise PostProcessingError(
                "llama-cpp-python is not installed. "
                "Install with: pip install llama-cpp-python"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            model = self._get_model()
            
            # Format the full prompt
            full_prompt = format_prompt(prompt_template, text)
            
            # Generate response
            response = model(
                full_prompt,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stop=["Transcription:", "\n\n\n"],  # Stop tokens
                echo=False,
            )
            
            result = response["choices"][0]["text"].strip()
            
            # Clean up any artifacts
            result = self._clean_response(result)
            
            return result if result else text
            
        except Exception as e:
            raise PostProcessingError(f"llama.cpp processing failed: {e}")
    
    def _clean_response(self, text: str) -> str:
        """Clean up LLM response artifacts."""
        if not text:
            return text
        
        result = text.strip()
        
        # Strip surrounding quotes (single, double, or smart quotes)
        quote_pairs = [
            ('"', '"'), ("'", "'"), ('"', '"'), (''', '''), ('「', '」'), ('«', '»')
        ]
        for open_q, close_q in quote_pairs:
            if result.startswith(open_q) and result.endswith(close_q):
                result = result[len(open_q):-len(close_q)].strip()
        
        # Remove common artifacts line by line
        lines = result.split("\n")
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            # Skip empty lines at start
            if not cleaned_lines and not line:
                continue
            # Skip lines that look like prompts/instructions/metadata
            lower_line = line.lower()
            if lower_line.startswith((
                "transcription:", "cleaned text:", "result:", "formatted",
                "output:", "corrected:", "here is", "here's the",
                "- removed", "- corrected", "- changed", "- fixed",
                "i removed", "i corrected", "i changed", "i fixed",
            )):
                continue
            # Skip lines that are just metadata descriptions
            if "filler words" in lower_line and ("removed" in lower_line or ":" in lower_line):
                continue
            cleaned_lines.append(line)
        
        result = "\n".join(cleaned_lines).strip()
        
        # Final quote strip in case quotes were inside other content
        for open_q, close_q in quote_pairs:
            if result.startswith(open_q) and result.endswith(close_q):
                result = result[len(open_q):-len(close_q)].strip()
        
        return result


# =============================================================================
# Anthropic Claude Backend (Cloud)
# =============================================================================

class AnthropicBackend(PostProcessorBackend):
    """
    Cloud LLM backend using Anthropic Claude API.
    Uses Claude Haiku for fast, cheap, high-quality results.
    """
    
    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-3-haiku-20240307",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = None
    
    def get_name(self) -> str:
        return "Anthropic Claude (Cloud)"
    
    def is_available(self) -> bool:
        """Check if anthropic is installed and API key is set."""
        try:
            import anthropic
            return bool(self.api_key)
        except ImportError:
            return False
    
    def _get_client(self):
        """Get or create the Anthropic client."""
        if self._client is not None:
            return self._client
        
        if not self.api_key:
            raise PostProcessingError(
                "Anthropic API key not set. "
                "Set ANTHROPIC_API_KEY environment variable or configure in settings."
            )
        
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
            return self._client
            
        except Exception as e:
            raise PostProcessingError(f"Failed to initialize Anthropic client: {e}")
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using Anthropic Claude API.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            if not self.api_key:
                raise PostProcessingError("Anthropic API key not configured.")
            raise PostProcessingError(
                "anthropic package is not installed. "
                "Install with: pip install anthropic"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            client = self._get_client()
            
            # Format the prompt
            full_prompt = format_prompt(prompt_template, text)
            
            # Call Claude API
            message = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[
                    {
                        "role": "user",
                        "content": full_prompt,
                    }
                ],
            )
            
            result = message.content[0].text.strip()
            
            return result if result else text
            
        except Exception as e:
            raise PostProcessingError(f"Anthropic API call failed: {e}")


# =============================================================================
# OpenAI Backend (Cloud)
# =============================================================================

class OpenAIBackend(PostProcessorBackend):
    """
    Cloud LLM backend using OpenAI API.
    Uses GPT-4o-mini for fast, cheap, high-quality results.
    """
    
    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = None
    
    def get_name(self) -> str:
        return "OpenAI (Cloud)"
    
    def is_available(self) -> bool:
        """Check if openai is installed and API key is set."""
        try:
            import openai
            return bool(self.api_key)
        except ImportError:
            return False
    
    def _get_client(self):
        """Get or create the OpenAI client."""
        if self._client is not None:
            return self._client
        
        if not self.api_key:
            raise PostProcessingError(
                "OpenAI API key not set. "
                "Set OPENAI_API_KEY environment variable or configure in settings."
            )
        
        try:
            import openai
            self._client = openai.OpenAI(api_key=self.api_key)
            return self._client
            
        except Exception as e:
            raise PostProcessingError(f"Failed to initialize OpenAI client: {e}")
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using OpenAI API.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            if not self.api_key:
                raise PostProcessingError("OpenAI API key not configured.")
            raise PostProcessingError(
                "openai package is not installed. "
                "Install with: pip install openai"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            client = self._get_client()
            
            # Format the prompt
            full_prompt = format_prompt(prompt_template, text)
            
            # Call OpenAI API
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[
                    {
                        "role": "user",
                        "content": full_prompt,
                    }
                ],
            )
            
            result = response.choices[0].message.content.strip()
            
            return result if result else text
            
        except Exception as e:
            raise PostProcessingError(f"OpenAI API call failed: {e}")


# =============================================================================
# Ollama Backend (Local)
# =============================================================================

class OllamaBackend(PostProcessorBackend):
    """
    Local LLM backend using Ollama API.
    Requires Ollama to be installed and running locally.
    Recommended models: phi3:mini, qwen2.5:1.5b, llama3.2:1b
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "phi3:mini",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = None
    
    def get_name(self) -> str:
        return "Ollama (Local)"
    
    def is_available(self) -> bool:
        """Check if Ollama is installed and running."""
        try:
            import requests
            response = requests.get(f"{self.base_url}/api/tags", timeout=2)
            return response.status_code == 200
        except:
            return False
    
    def _get_client(self):
        """Get or create the requests session."""
        if self._client is not None:
            return self._client
        
        try:
            import requests
            self._client = requests.Session()
            return self._client
        except ImportError:
            raise PostProcessingError(
                "requests package is not installed. "
                "Install with: pip install requests"
            )
    
    def process(self, text: str, prompt_template: str) -> str:
        """
        Process text using Ollama API.
        
        Args:
            text: The raw transcription text
            prompt_template: The prompt template to use
            
        Returns:
            Cleaned/formatted text
        """
        if not self.is_available():
            raise PostProcessingError(
                "Ollama is not running. "
                "Please start Ollama service: ollama serve"
            )
        
        if not text or not text.strip():
            return text
        
        try:
            import requests
            
            # Format the prompt
            full_prompt = format_prompt(prompt_template, text)
            
            # Call Ollama API
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": full_prompt,
                    "stream": False,
                    "options": {
                        "num_predict": self.max_tokens,
                        "temperature": self.temperature,
                    },
                },
                timeout=30,
            )
            
            if response.status_code != 200:
                raise PostProcessingError(f"Ollama API error: {response.status_code}")
            
            result = response.json().get("response", "").strip()
            
            # Clean up any artifacts
            result = self._clean_response(result)
            
            return result if result else text
            
        except requests.exceptions.RequestException as e:
            raise PostProcessingError(f"Ollama API call failed: {e}")
        except Exception as e:
            raise PostProcessingError(f"Ollama processing failed: {e}")
    
    def _clean_response(self, text: str) -> str:
        """Clean up LLM response artifacts."""
        if not text:
            return text
        
        # Strip surrounding quotes (models often wrap output in quotes)
        text = text.strip()
        if (text.startswith('"') and text.endswith('"')) or \
           (text.startswith("'") and text.endswith("'")):
            text = text[1:-1].strip()
        
        # Also handle smart quotes
        if (text.startswith('"') and text.endswith('"')) or \
           (text.startswith(''') and text.endswith(''')):
            text = text[1:-1].strip()
        
        # Remove common artifacts
        lines = text.split("\n")
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            # Skip empty lines at start
            if not cleaned_lines and not line:
                continue
            # Skip lines that look like prompts/instructions
            if line.lower().startswith(("transcription:", "cleaned text:", "result:", "formatted")):
                continue
            # Skip meta-commentary about changes made (bullet points explaining edits)
            if line.startswith("- ") and any(word in line.lower() for word in 
                ["removed", "corrected", "changed", "fixed", "filler", "error"]):
                continue
            # Skip lines that explain what was done
            if any(phrase in line.lower() for phrase in 
                ["here is", "here's the", "i have", "i've", "the corrected", "the cleaned"]):
                continue
            cleaned_lines.append(line)
        
        return "\n".join(cleaned_lines).strip()


# =============================================================================
# Factory Functions
# =============================================================================

def get_backend(config: dict) -> PostProcessorBackend:
    """
    Factory function to create the appropriate post-processing backend.
    
    Args:
        config: Configuration dictionary with post-processing settings
        
    Returns:
        A PostProcessorBackend instance
    """
    backend_type = config.get("post_processing_backend", "llama_cpp")
    
    if backend_type == "ollama":
        return OllamaBackend(
            base_url=config.get("ollama_base_url", "http://localhost:11434"),
            model=config.get("ollama_model", "phi3:mini"),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
        )
    elif backend_type == "anthropic":
        # API key is read from environment variable only (secure)
        return AnthropicBackend(
            api_key="",  # Will be read from ANTHROPIC_API_KEY env var
            model=config.get("anthropic_model", "claude-3-haiku-20240307"),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
        )
    elif backend_type == "openai":
        # API key is read from environment variable only (secure)
        return OpenAIBackend(
            api_key="",  # Will be read from OPENAI_API_KEY env var
            model=config.get("openai_model", "gpt-4o-mini"),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
        )
    else:
        # Default to llama.cpp
        return LlamaCppBackend(
            model_path=config.get("llama_cpp_model_path", ""),
            n_ctx=config.get("llama_cpp_n_ctx", 2048),
            n_threads=config.get("llama_cpp_n_threads", 4),
            n_gpu_layers=config.get("llama_cpp_n_gpu_layers", -1),
            max_tokens=config.get("post_processing_max_tokens", 1024),
            temperature=config.get("post_processing_temperature", 0.1),
            use_gpu=config.get("use_gpu", True),
        )


def process_with_config(text: str, config: dict) -> str:
    """
    Post-process transcription using settings from config dictionary.
    This is the main entry point for post-processing.
    
    Uses the new tone-based system with optional smart formatting:
    - output_tone: professional | casual | technical
    - smart_formatting: True (auto-detect content type) | False (clean only)
    
    Args:
        text: The raw transcription text
        config: Configuration dictionary with post-processing settings
        
    Returns:
        Cleaned/formatted text, or original text if post-processing is disabled/fails
    """
    # Check if post-processing is enabled
    if not config.get("post_processing_enabled", False):
        return text
    
    if not text or not text.strip():
        return text
    
    try:
        # Build the prompt using new tone-based system
        prompt = build_prompt(text, config)
        
        # Get backend and process
        backend = get_backend(config)
        
        if not backend.is_available():
            backend_type = config.get("post_processing_backend", "llama_cpp")
            if backend_type == "openai":
                print(f"[Post-processing] ⚠ OpenAI not available - check OPENAI_API_KEY env var")
            elif backend_type == "anthropic":
                print(f"[Post-processing] ⚠ Anthropic not available - check ANTHROPIC_API_KEY env var")
            else:
                print(f"[Post-processing] ⚠ llama.cpp not available - no model selected")
            return text
        
        # Process with the built prompt (prompt already includes the text)
        result = backend.process(text, prompt)
        return result
        
    except PostProcessingError as e:
        # If post-processing fails, return original text
        print(f"[Post-processing] ✗ Error: {e}")
        return text
    except Exception as e:
        # Catch-all for unexpected errors
        print(f"[Post-processing] ✗ Unexpected error: {e}")
        return text


def get_available_backends() -> list:
    """
    Get a list of available post-processing backends.
    
    Returns:
        List of dicts with backend info
    """
    backends = []
    
    # Check llama.cpp
    llama_backend = LlamaCppBackend()
    try:
        import llama_cpp
        llama_available = True
    except ImportError:
        llama_available = False
    
    backends.append({
        "id": "llama_cpp",
        "name": "llama.cpp (Local)",
        "available": llama_available,
        "requires_model": True,
        "description": "Fast local inference using GGUF models. Recommended: Phi-3-mini, Qwen2.5-1.5B",
    })
    
    # Check Anthropic
    try:
        import anthropic
        anthropic_available = True
    except ImportError:
        anthropic_available = False
    
    backends.append({
        "id": "anthropic",
        "name": "Anthropic Claude (Cloud)",
        "available": anthropic_available,
        "requires_api_key": True,
        "description": "High-quality cloud processing using Claude Haiku. Fast and affordable.",
    })
    
    # Check OpenAI
    try:
        import openai
        openai_available = True
    except ImportError:
        openai_available = False
    
    backends.append({
        "id": "openai",
        "name": "OpenAI (Cloud)",
        "available": openai_available,
        "requires_api_key": True,
        "description": "Cloud processing using GPT-4o-mini. Fast and reliable.",
    })
    
    # Check Ollama
    ollama_backend = OllamaBackend()
    ollama_available = ollama_backend.is_available()
    
    backends.append({
        "id": "ollama",
        "name": "Ollama (Local)",
        "available": ollama_available,
        "requires_model": True,
        "description": "Local inference using Ollama. Recommended: phi3:mini, qwen2.5:1.5b",
    })
    
    return backends


def get_template_names() -> list:
    """Legacy function - returns empty list as templates are replaced by tone system."""
    return []


def get_template_descriptions() -> Dict[str, str]:
    """Legacy function - returns empty dict as templates are replaced by tone system."""
    return {}


def get_tone_options() -> list:
    """Get list of available output tones."""
    return [
        {"id": "professional", "name": "Professional", "description": "Formal, polished, business-appropriate"},
        {"id": "casual", "name": "Casual", "description": "Relaxed, conversational, friendly"},
        {"id": "technical", "name": "Technical", "description": "Precise, detailed, developer-focused"},
    ]

