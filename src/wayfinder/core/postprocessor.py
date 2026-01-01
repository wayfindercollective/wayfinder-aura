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

# Tone guidance for different output styles with intensity levels
# Each style has light/standard/strong variations
TONE_GUIDANCE: Dict[str, Dict[str, str]] = {
    "professional": {
        "light": "Use clear, professional language. Maintain a polished but approachable tone.",
        "standard": "Use formal, polished, business-appropriate language. Be clear and concise.",
        "strong": "Use highly formal, executive-level language. Be extremely polished, precise, and authoritative. Avoid any casual expressions.",
    },
    "casual": {
        "light": "Use friendly, natural language. Keep it conversational but still clear.",
        "standard": "Use relaxed, conversational, friendly language. Keep it natural and approachable.",
        "strong": "Use very casual, laid-back language. Be super friendly and informal, like texting a close friend. Contractions and casual expressions encouraged.",
    },
    "technical": {
        "light": "Use clear technical language. Include relevant terminology where appropriate.",
        "standard": "Use precise, detailed, developer-focused language. Be accurate and specific.",
        "strong": "Use highly technical, expert-level language. Include specific technical terms, code references, and implementation details. Assume deep technical knowledge.",
    },
}

# =============================================================================
# Formatting Rules (Options A + B: Tone + Intensity aware)
# =============================================================================
# These control punctuation, capitalization, and structure based on BOTH
# the tone AND intensity settings. This prevents conflicts like casual-strong
# still having perfect punctuation.

FORMATTING_RULES: Dict[str, Dict[str, str]] = {
    "professional": {
        "light": "Use proper punctuation and capitalization. Keep formatting clean and readable.",
        "standard": "Use perfect punctuation and capitalization. Format professionally with clear paragraph structure.",
        "strong": "Use impeccable punctuation and grammar. Formal structure with precise formatting. Executive-level polish on every sentence.",
    },
    "casual": {
        "light": "Use standard punctuation, but don't over-formalize. Natural flow is more important than perfect grammar.",
        "standard": "Relaxed punctuation is fine. Lowercase sentence starts are acceptable. Keep it natural like speaking.",
        "strong": "Minimal punctuation - skip periods at end of messages, lowercase is totally fine, abbreviations ok. Text-message style vibes.",
    },
    "technical": {
        "light": "Use standard punctuation with technical clarity. Code terms should be clear.",
        "standard": "Use precise punctuation. Technical terms should be exact. Code references in backticks when appropriate.",
        "strong": "Use exact punctuation for technical precision. Documentation-ready formatting. Code blocks, specific terminology, and structured output.",
    },
}

# Filler word removal rules - also intensity-aware
FILLER_RULES: Dict[str, str] = {
    "light": "Remove obvious filler words (um, uh, ah) but keep natural speech patterns like 'like' or 'you know' if they add conversational flow.",
    "standard": "Remove filler words: um, uh, ah, like, you know, basically, actually, I mean, so, well, right.",
    "strong": "Remove ALL filler words and verbal tics. Clean up any hesitation markers, false starts, and repetitions.",
}


# =============================================================================
# Model Compatibility System
# =============================================================================
# Defines model tiers and known issues to provide better UX

# Model size tiers based on parameter count
MODEL_TIERS = {
    "tiny": {  # < 500M params
        "description": "Very small models, limited instruction following",
        "max_intensity": "light",  # Recommend light intensity max
        "smart_formatting": False,  # Disable smart formatting
        "patterns": ["360m", "135m", "tiny", "0.5b"],
    },
    "small": {  # 500M - 2B params
        "description": "Small models, good for basic cleanup",
        "max_intensity": "standard",
        "smart_formatting": True,  # Works but may have quirks
        "patterns": ["1b", "1.5b", "2b", "small", "mini"],
    },
    "standard": {  # 2B - 7B params
        "description": "Standard models, full capability",
        "max_intensity": "strong",
        "smart_formatting": True,
        "patterns": ["3b", "4b", "7b", "8b", "medium"],
    },
    "large": {  # 7B+ params
        "description": "Large models, best quality",
        "max_intensity": "strong",
        "smart_formatting": True,
        "patterns": ["13b", "14b", "32b", "70b", "large", "gpt-4", "claude"],
    },
}

# Known model-specific issues
MODEL_QUIRKS: Dict[str, Dict[str, Any]] = {
    "llama3.2:1b": {
        "issues": ["safety_filter_email"],
        "workaround": "Disable smart_formatting for professional/technical modes",
        "avoid_words": ["email"],  # These words trigger false-positive safety
    },
    "llama3.2:3b": {
        "issues": ["safety_filter_email"],
        "workaround": "Disable smart_formatting for professional/technical modes",
        "avoid_words": ["email"],
    },
    "smollm2:360m": {
        "issues": ["weak_instruction_following"],
        "workaround": "Use simplified prompts only",
        "tier_override": "tiny",
    },
    "phi3:mini": {
        "issues": [],  # Generally works well
        "tier_override": "small",
    },
    "qwen2.5:1.5b": {
        "issues": [],  # Generally works well
        "tier_override": "small",
    },
}


def detect_model_tier(model_name: str) -> str:
    """
    Detect the capability tier of a model based on its name.
    
    Returns: "tiny", "small", "standard", or "large"
    """
    model_lower = model_name.lower()
    
    # Check for specific model overrides first
    for known_model, quirks in MODEL_QUIRKS.items():
        if known_model in model_lower:
            if "tier_override" in quirks:
                return quirks["tier_override"]
    
    # Check patterns in reverse order (largest first) to match correctly
    for tier in ["large", "standard", "small", "tiny"]:
        tier_info = MODEL_TIERS[tier]
        for pattern in tier_info["patterns"]:
            if pattern in model_lower:
                return tier
    
    # Default to small (conservative but capable)
    return "small"


def get_model_quirks(model_name: str) -> Dict[str, Any]:
    """Get known issues/quirks for a specific model."""
    model_lower = model_name.lower()
    
    for known_model, quirks in MODEL_QUIRKS.items():
        if known_model in model_lower:
            return quirks
    
    return {"issues": [], "workaround": None}


def get_model_compatibility(model_name: str, tone: str, intensity: str, smart_formatting: bool) -> Dict[str, Any]:
    """
    Check model compatibility with current settings and return recommendations.
    
    Returns dict with:
        - compatible: bool - whether settings are fully compatible
        - warnings: list[str] - any warnings to show user
        - recommendations: list[str] - suggested changes
        - auto_adjustments: dict - automatic adjustments to apply
    """
    tier = detect_model_tier(model_name)
    tier_info = MODEL_TIERS[tier]
    quirks = get_model_quirks(model_name)
    
    result = {
        "compatible": True,
        "tier": tier,
        "warnings": [],
        "recommendations": [],
        "auto_adjustments": {},
    }
    
    # Check intensity vs tier capability
    intensity_order = ["light", "standard", "strong"]
    max_intensity = tier_info["max_intensity"]
    if intensity_order.index(intensity) > intensity_order.index(max_intensity):
        result["compatible"] = False
        result["warnings"].append(
            f"⚠️ {model_name} may struggle with '{intensity}' intensity. "
            f"Recommended max: '{max_intensity}'"
        )
        result["recommendations"].append(f"Consider using '{max_intensity}' intensity instead")
        result["auto_adjustments"]["intensity"] = max_intensity
    
    # Check smart_formatting for tiny models
    if tier == "tiny" and smart_formatting:
        result["warnings"].append(
            f"⚠️ Smart formatting may not work well with small models like {model_name}"
        )
        result["auto_adjustments"]["smart_formatting"] = False
    
    # Check for model-specific quirks
    if "safety_filter_email" in quirks.get("issues", []):
        if smart_formatting and tone in ["professional", "technical"]:
            result["warnings"].append(
                f"⚠️ {model_name} may refuse '{tone}' mode due to safety filters. "
                "Disabling smart formatting automatically."
            )
            result["auto_adjustments"]["smart_formatting"] = False
    
    if "weak_instruction_following" in quirks.get("issues", []):
        result["warnings"].append(
            f"💡 {model_name} has limited capability. Results may vary."
        )
        result["recommendations"].append("Consider upgrading to a 1B+ model for better results")
    
    return result


def get_tone_guidance(tone: str, intensity: str = "standard") -> str:
    """Get the tone guidance string for a given tone and intensity."""
    tone_dict = TONE_GUIDANCE.get(tone, TONE_GUIDANCE["professional"])
    return tone_dict.get(intensity, tone_dict["standard"])


def get_formatting_rules(tone: str, intensity: str = "standard") -> str:
    """Get the formatting/punctuation rules for a given tone and intensity."""
    tone_dict = FORMATTING_RULES.get(tone, FORMATTING_RULES["professional"])
    return tone_dict.get(intensity, tone_dict["standard"])


def get_filler_rules(intensity: str = "standard") -> str:
    """Get the filler word removal rules for a given intensity."""
    return FILLER_RULES.get(intensity, FILLER_RULES["standard"])

# Smart formatting prompt - auto-detects content type and formats appropriately
# Uses dynamic placeholders for tone-aware formatting rules
# NOTE: Avoid words like "email" that trigger safety filters in some models
SMART_FORMAT_PROMPT = """You are a transcription cleanup assistant. Process this speech transcription:

1. FILLER WORDS: {filler_rules}

2. FORMATTING: {formatting_rules}

3. STRUCTURE: Auto-detect the content type and format appropriately:
   - If it's a message to someone: format with greeting/sign-off
   - If it contains lists or multiple items: use bullet points
   - If it describes code or technical concepts: use documentation style
   - Otherwise: clean prose paragraphs

4. TONE: {tone_guidance}

IMPORTANT: Output ONLY the cleaned text. No explanations, no labels, no "Here is...".

Transcription:
{text}

Cleaned:"""

# Clean-only prompt - minimal processing, just removes fillers
# Also uses dynamic formatting rules based on tone/intensity
CLEAN_ONLY_PROMPT = """Clean up this transcribed speech. Keep the exact structure and meaning - only clean up the delivery.

1. FILLER WORDS: {filler_rules}

2. FORMATTING: {formatting_rules}

3. TONE: {tone_guidance}

IMPORTANT: Output ONLY the cleaned text. No explanations, no labels.

Transcription:
{text}

Cleaned:"""

# Simplified prompt for tiny models (<500M params)
# Uses minimal instructions that small models can follow
SIMPLE_CLEANUP_PROMPT = """Clean this text. Remove "um", "uh", "like", "you know". Fix grammar. Be {tone_simple}.

Text: {text}

Clean:"""

# Simple tone descriptions for tiny models
SIMPLE_TONES = {
    "professional": "formal and polished",
    "casual": "friendly and relaxed",
    "technical": "precise and clear",
}


def build_prompt(text: str, config: dict, apply_compatibility: bool = True) -> tuple[str, Dict[str, Any]]:
    """
    Build the appropriate prompt based on config settings.
    
    Uses Options A + B: Both tone AND intensity affect formatting rules.
    This ensures casual-strong gets text-message style formatting while
    professional-strong gets executive-level polish.
    
    Also applies model compatibility adjustments when needed.
    
    Args:
        text: The transcription text to process
        config: Configuration dictionary with style settings
        apply_compatibility: Whether to check and apply model compatibility adjustments
        
    Returns:
        Tuple of (formatted_prompt, compatibility_info)
    """
    # Get tone and intensity
    tone = config.get("output_tone", "professional")
    intensity_key = f"{tone}_intensity"
    intensity = config.get(intensity_key, "standard")
    smart_formatting = config.get("smart_formatting", True)
    
    # Get model name for compatibility check
    backend = config.get("post_processing_backend", "llama_cpp")
    if backend == "ollama":
        model_name = config.get("ollama_model", "")
    elif backend == "llama_cpp":
        model_path = config.get("llama_cpp_model_path", "")
        model_name = Path(model_path).stem if model_path else ""
    else:
        model_name = config.get(f"{backend}_model", "")
    
    # Check compatibility and get auto-adjustments
    compatibility = {"warnings": [], "auto_adjustments": {}, "tier": "standard"}
    if apply_compatibility and model_name:
        compatibility = get_model_compatibility(model_name, tone, intensity, smart_formatting)
        
        # Apply auto-adjustments
        if "smart_formatting" in compatibility["auto_adjustments"]:
            smart_formatting = compatibility["auto_adjustments"]["smart_formatting"]
        if "intensity" in compatibility["auto_adjustments"]:
            intensity = compatibility["auto_adjustments"]["intensity"]
    
    # Detect if we should use the simple prompt for tiny models
    tier = compatibility.get("tier", detect_model_tier(model_name) if model_name else "standard")
    
    if tier == "tiny":
        # Use simplified prompt for tiny models
        tone_simple = SIMPLE_TONES.get(tone, "clear")
        prompt = SIMPLE_CLEANUP_PROMPT.format(tone_simple=tone_simple, text=text)
        return prompt, compatibility
    
    # Get all the dynamic rules based on tone + intensity
    tone_guidance = get_tone_guidance(tone, intensity)
    formatting_rules = get_formatting_rules(tone, intensity)
    filler_rules = get_filler_rules(intensity)
    
    # Choose prompt based on smart formatting setting
    if smart_formatting:
        template = SMART_FORMAT_PROMPT
    else:
        template = CLEAN_ONLY_PROMPT
    
    # Fill in all placeholders
    prompt = template.format(
        tone_guidance=tone_guidance,
        formatting_rules=formatting_rules,
        filler_rules=filler_rules,
        text=text
    )
    
    return prompt, compatibility


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
            cleaned_lines.append(line)
        
        return "\n".join(cleaned_lines).strip()


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
    
    Includes model compatibility checking and auto-adjustments.
    
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
        # Build the prompt using new tone-based system with compatibility checks
        prompt, compatibility = build_prompt(text, config)
        
        # Log any compatibility warnings
        for warning in compatibility.get("warnings", []):
            print(f"[Post-processing] {warning}")
        
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


def get_model_tier_info(model_name: str) -> Dict[str, Any]:
    """
    Get detailed tier information for a model.
    Useful for UI to display recommendations.
    
    Returns dict with:
        - tier: str (tiny, small, standard, large)
        - description: str
        - max_intensity: str
        - smart_formatting_ok: bool
        - quirks: list of known issues
        - recommendations: list of suggestions
    """
    tier = detect_model_tier(model_name)
    tier_info = MODEL_TIERS[tier]
    quirks = get_model_quirks(model_name)
    
    return {
        "tier": tier,
        "description": tier_info["description"],
        "max_intensity": tier_info["max_intensity"],
        "smart_formatting_ok": tier_info["smart_formatting"],
        "quirks": quirks.get("issues", []),
        "recommendations": [
            f"Best with intensity: {tier_info['max_intensity']} or lower",
        ] + ([f"Note: {quirks['workaround']}"] if quirks.get("workaround") else []),
    }


def get_recommended_models() -> list:
    """
    Get list of recommended models for post-processing.
    
    Returns list of dicts with model recommendations by tier.
    """
    return [
        {
            "tier": "recommended",
            "models": [
                {"name": "qwen2.5:1.5b", "description": "Best balance of speed and quality"},
                {"name": "phi3:mini", "description": "Fast, good instruction following"},
                {"name": "llama3.2:3b", "description": "Higher quality, slightly slower"},
            ],
        },
        {
            "tier": "budget",
            "models": [
                {"name": "llama3.2:1b", "description": "Fast but has quirks with some modes"},
                {"name": "smollm2:360m", "description": "Very fast, basic cleanup only"},
            ],
        },
        {
            "tier": "premium",
            "models": [
                {"name": "qwen2.5:7b", "description": "Excellent quality, slower"},
                {"name": "gpt-4o-mini", "description": "Cloud API, very high quality"},
                {"name": "claude-3-haiku", "description": "Cloud API, excellent quality"},
            ],
        },
    ]

