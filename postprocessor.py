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
# Refusal and Hallucination Detection
# =============================================================================

# Refusal detection patterns - phrases that indicate model is refusing to process
REFUSAL_PATTERNS = [
    "i cannot provide",
    "i can't provide",
    "i cannot transcribe",
    "i can't transcribe",
    "i cannot help with",
    "i can't help with",
    "i'm unable to",
    "i am unable to",
    "i cannot assist",
    "i can't assist",
    "as an ai",
    "as a language model",
    "i apologize, but",
    "sorry, but i cannot",
    "i'm not able to",
    "cannot process this",
    "this could be interpreted as",
    "if you need help with",
    "can i help you with something else",
]


def is_refusal_response(response: str) -> bool:
    """Detect if the LLM response is a refusal rather than actual processed text."""
    if not response:
        return False
    
    response_lower = response.lower()
    
    for pattern in REFUSAL_PATTERNS:
        if pattern in response_lower:
            print(f"[Post-processing] Refusal detected: matched pattern '{pattern}'")
            return True
    
    if len(response) > 200 and ("apologize" in response_lower or "sorry" in response_lower):
        print(f"[Post-processing] Refusal detected: long apologetic response")
        return True
    
    return False


def is_hallucination(original: str, response: str, threshold: float = 0.3) -> bool:
    """
    Detect if the LLM response is a hallucination (completely different from input).
    Uses simple word overlap to detect when the model generates unrelated content.
    """
    if not original or not response:
        return False
    
    import re
    def get_words(text: str) -> set:
        return set(re.findall(r'\b[a-z]+\b', text.lower()))
    
    original_words = get_words(original)
    response_words = get_words(response)
    
    if not original_words:
        return False
    
    common_words = original_words & response_words
    
    stop_words = {'um', 'uh', 'like', 'you', 'know', 'basically', 'actually', 
                  'i', 'mean', 'so', 'well', 'right', 'the', 'a', 'an', 'and', 
                  'or', 'but', 'is', 'are', 'was', 'were', 'be', 'been', 'to',
                  'of', 'in', 'for', 'on', 'with', 'at', 'by', 'it', 'this', 'that'}
    
    meaningful_original = original_words - stop_words
    meaningful_common = common_words - stop_words
    
    if not meaningful_original:
        overlap_ratio = len(common_words) / len(original_words) if original_words else 0
    else:
        overlap_ratio = len(meaningful_common) / len(meaningful_original)
    
    length_ratio = len(response) / len(original) if original else 1
    effective_threshold = threshold * 1.5 if length_ratio > 3 else threshold
    
    is_hallucinated = overlap_ratio < effective_threshold
    
    if is_hallucinated:
        print(f"[Post-processing] ⚠ Hallucination detected: {overlap_ratio:.1%} word overlap (threshold: {effective_threshold:.1%})")
    
    return is_hallucinated


# =============================================================================
# Tone-Based Prompts (New Simplified System)
# =============================================================================

# Tone guidance for different output styles with intensity levels
# Each style has light/standard/strong variations
TONE_GUIDANCE: Dict[str, Dict[str, str]] = {
    "professional": {
        "light": "Professional tone.",
        "standard": "Formal, business-appropriate tone.",
        "strong": "Very formal, executive tone.",
    },
    "casual": {
        "light": "Friendly, natural tone.",
        "standard": "Casual, conversational tone.",
        "strong": "Very casual, like texting a friend.",
    },
    "ai_prompt": {
        "light": "Keep the natural phrasing. This is input for an AI assistant.",
        "standard": "Clean up for an AI assistant. Format as a clear prompt or question.",
        "strong": "Format as a well-structured prompt for an AI assistant.",
    },
}

# Backwards compatibility - get standard tone guidance
def get_tone_guidance(tone: str, intensity: str = "standard") -> str:
    """Get the tone guidance string for a given tone and intensity."""
    tone_dict = TONE_GUIDANCE.get(tone, TONE_GUIDANCE["professional"])
    return tone_dict.get(intensity, tone_dict["standard"])

# Smart formatting prompt - simplified to avoid model confusion
# NOTE: phi3 and similar models work best with very direct, simple instructions
# The exact phrasing "Clean up this text. Remove filler words. Keep exact meaning:" works best
SMART_FORMAT_PROMPT = """Clean up this text. Remove filler words. Keep exact meaning:

{text}"""

# Clean-only prompt - same as smart format for consistency
CLEAN_ONLY_PROMPT = """Clean up this text. Remove filler words. Keep exact meaning:

{text}"""


def build_prompt(text: str, config: dict) -> str:
    """
    Build the appropriate prompt based on config settings.
    
    Args:
        text: The transcription text to process
        config: Configuration dictionary with style settings
        
    Returns:
        Formatted prompt ready for LLM
    """
    # Get tone and intensity
    tone = config.get("output_tone", "professional")
    intensity_key = f"{tone}_intensity"
    intensity = config.get(intensity_key, "standard")
    
    # Get the tone guidance for this tone and intensity
    tone_guidance = get_tone_guidance(tone, intensity)
    
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
# Model Compatibility System
# =============================================================================

# Model tiers based on parameter count
MODEL_TIERS: Dict[str, Dict[str, Any]] = {
    "tiny": {"min_params": 0, "max_params": 500_000_000, "max_intensity": "light", "smart_formatting": False},
    "small": {"min_params": 500_000_000, "max_params": 2_000_000_000, "max_intensity": "standard", "smart_formatting": True},
    "medium": {"min_params": 2_000_000_000, "max_params": 8_000_000_000, "max_intensity": "strong", "smart_formatting": True},
    "large": {"min_params": 8_000_000_000, "max_params": float("inf"), "max_intensity": "strong", "smart_formatting": True},
    "cloud": {"min_params": 0, "max_params": float("inf"), "max_intensity": "strong", "smart_formatting": True},
}


def get_model_tier_info(model_name: str) -> Dict[str, Any]:
    """
    Determine a model's tier based on its name.
    
    Returns dict with:
        - tier: str (tiny, small, medium, large, cloud)
        - max_intensity: str (light, standard, strong)
        - smart_formatting: bool
    """
    model_lower = model_name.lower()
    
    # Cloud models (OpenAI, Anthropic) - always capable
    cloud_indicators = ["gpt-", "claude", "o1-", "chatgpt"]
    if any(ind in model_lower for ind in cloud_indicators):
        return {"tier": "cloud", "max_intensity": "strong", "smart_formatting": True}
    
    # Parse parameter count from model name
    param_patterns = [
        (":0.5b", 500_000_000), ("0.5b", 500_000_000),
        (":1b", 1_000_000_000), ("1b", 1_000_000_000),
        (":1.5b", 1_500_000_000), ("1.5b", 1_500_000_000),
        (":3b", 3_000_000_000), ("3b", 3_000_000_000),
        (":7b", 7_000_000_000), ("7b", 7_000_000_000),
        (":8b", 8_000_000_000), ("8b", 8_000_000_000),
        (":13b", 13_000_000_000), ("13b", 13_000_000_000),
        (":70b", 70_000_000_000), ("70b", 70_000_000_000),
        ("360m", 360_000_000), ("135m", 135_000_000),
        (":mini", 3_800_000_000),  # phi3:mini is ~3.8B
        (":medium", 14_000_000_000),  # phi3:medium is ~14B
    ]
    
    params = None
    for pattern, count in param_patterns:
        if pattern in model_lower:
            params = count
            break
    
    if params is None:
        params = 1_500_000_000  # Default assumption for unknown models
    
    # Determine tier
    for tier, info in MODEL_TIERS.items():
        if tier == "cloud":
            continue
        if info["min_params"] <= params < info["max_params"]:
            return {"tier": tier, "max_intensity": info["max_intensity"], "smart_formatting": info["smart_formatting"]}
    
    return {"tier": "small", "max_intensity": "standard", "smart_formatting": True}


def get_model_compatibility(model_name: str, tone: str, intensity: str, smart_formatting: bool) -> Dict[str, Any]:
    """
    Check if the requested settings are compatible with the model.
    
    Returns dict with:
        - compatible: bool
        - tier: str
        - warnings: list of warning messages
        - auto_adjustments: dict of adjustments that will be made
        - upgrade_suggestion: dict or None
    """
    tier_info = get_model_tier_info(model_name)
    tier = tier_info["tier"]
    max_intensity = tier_info["max_intensity"]
    supports_smart = tier_info["smart_formatting"]
    
    intensity_order = ["light", "standard", "strong"]
    requested_level = intensity_order.index(intensity) if intensity in intensity_order else 1
    max_level = intensity_order.index(max_intensity) if max_intensity in intensity_order else 1
    
    warnings = []
    auto_adjustments = {}
    upgrade_suggestion = None
    compatible = True
    
    # Check intensity compatibility
    if requested_level > max_level:
        compatible = False
        warnings.append(f"⚠️ '{intensity}' intensity requires a larger model. Using '{max_intensity}' instead.")
        auto_adjustments["intensity"] = max_intensity
        upgrade_suggestion = get_upgrade_suggestion_for_intensity(intensity)
    
    # Check smart formatting compatibility
    if smart_formatting and not supports_smart and tier != "cloud":
        warnings.append(f"💡 Smart formatting may be limited with this model size.")
    
    return {
        "compatible": compatible,
        "tier": tier,
        "warnings": warnings,
        "auto_adjustments": auto_adjustments,
        "upgrade_suggestion": upgrade_suggestion,
    }


def get_upgrade_suggestion_for_intensity(intensity: str) -> Dict[str, Any]:
    """
    Get specific model upgrade suggestions based on desired intensity.
    """
    if intensity == "strong":
        return {
            "min_params": "3B+",
            "recommended_models": [
                {"name": "qwen2.5:3b", "type": "ollama", "description": "Best for strong intensity"},
                {"name": "llama3.2:3b", "type": "ollama", "description": "Good alternative"},
                {"name": "phi3:medium", "type": "ollama", "description": "Fast and capable"},
            ],
            "message": "Strong intensity works best with 3B+ parameter models. Try: ollama pull qwen2.5:3b",
        }
    elif intensity == "standard":
        return {
            "min_params": "1B+",
            "recommended_models": [
                {"name": "qwen2.5:1.5b", "type": "ollama", "description": "Great balance"},
                {"name": "llama3.2:1b", "type": "ollama", "description": "Fast option"},
            ],
            "message": "Standard intensity works with 1B+ parameter models.",
        }
    else:
        return {
            "min_params": "500M+",
            "recommended_models": [
                {"name": "smollm2:360m", "type": "ollama", "description": "Ultra fast"},
            ],
            "message": "Light intensity works with most models.",
        }


def check_settings_compatibility(config: dict) -> Dict[str, Any]:
    """
    Check if current config settings are compatible with the selected model.
    
    This is the main function for UI to call when settings change.
    Returns a complete compatibility report with actionable feedback.
    
    Args:
        config: Full configuration dictionary
        
    Returns:
        Dict with:
            - is_compatible: bool
            - issues: list of issue descriptions
            - recommendations: list of actionable recommendations  
            - upgrade_message: str or None
            - severity: "ok" | "warning" | "incompatible"
    """
    # Get model name based on backend
    backend = config.get("post_processing_backend", "llama_cpp")
    if backend == "ollama":
        model_name = config.get("ollama_model", "")
    elif backend == "llama_cpp":
        model_path = config.get("llama_cpp_model_path", "")
        model_name = Path(model_path).stem if model_path else ""
    elif backend == "openai":
        model_name = config.get("openai_model", "gpt-4o-mini")
    elif backend == "anthropic":
        model_name = config.get("anthropic_model", "claude-3-haiku")
    else:
        model_name = ""
    
    # Skip check if post-processing is disabled
    if not config.get("post_processing_enabled", False):
        return {
            "is_compatible": True,
            "issues": [],
            "recommendations": [],
            "upgrade_message": None,
            "severity": "ok",
        }
    
    # Skip check if no model selected
    if not model_name:
        return {
            "is_compatible": False,
            "issues": ["No post-processing model selected"],
            "recommendations": ["Select a model in Post-Processing settings"],
            "upgrade_message": None,
            "severity": "warning",
        }
    
    # Get tone and intensity
    tone = config.get("output_tone", "professional")
    intensity_key = f"{tone}_intensity"
    intensity = config.get(intensity_key, "standard")
    smart_formatting = config.get("smart_formatting", True)
    
    # Run compatibility check
    compat = get_model_compatibility(model_name, tone, intensity, smart_formatting)
    
    # Build result
    issues = []
    recommendations = []
    upgrade_message = None
    
    if not compat["compatible"]:
        tier = compat["tier"]
        tier_info = MODEL_TIERS.get(tier, {})
        max_intensity = tier_info.get("max_intensity", "standard")
        
        issues.append(f"'{intensity.title()}' intensity requires a larger model")
        
        if compat["upgrade_suggestion"]:
            suggestion = compat["upgrade_suggestion"]
            upgrade_message = suggestion["message"]
            recommendations.append(f"Upgrade to a {suggestion['min_params']} model")
            
            for model in suggestion["recommended_models"][:2]:
                recommendations.append(f"Try: {model['name']} ({model['description']})")
        
        recommendations.append(f"Or use '{max_intensity}' intensity with current model")
    
    # Add any other warnings (skip duplicates and intensity warnings already covered)
    for warning in compat.get("warnings", []):
        cleaned = warning.replace("⚠️ ", "").replace("💡 ", "")
        # Skip if this is about intensity (already covered above)
        if "intensity requires" in cleaned.lower():
            continue
        if cleaned not in issues:
            issues.append(cleaned)
    
    return {
        "is_compatible": compat["compatible"],
        "issues": issues,
        "recommendations": recommendations,
        "upgrade_message": upgrade_message,
        "severity": "incompatible" if not compat["compatible"] else ("warning" if issues else "ok"),
        "current_model": model_name,
        "current_tier": compat["tier"],
        "requested_intensity": intensity,
        "effective_intensity": compat["auto_adjustments"].get("intensity", intensity),
    }


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
            prompt_template: The prompt template to use (already formatted with text)
            
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
            
            # The prompt_template is already fully formatted (text embedded by build_prompt)
            full_prompt = prompt_template
            
            # Debug: Log what we're sending
            print(f"[Post-processing] Sending to {self.model}...")
            
            # Call Ollama API using chat endpoint for better instruction following
            # NOTE: No system prompt - the user message contains all necessary instructions
            # System prompts can confuse phi3 and similar models
            try:
                response = requests.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "user", 
                                "content": full_prompt
                            }
                        ],
                        "stream": False,
                        "options": {
                            "num_predict": self.max_tokens,
                            "temperature": self.temperature,
                        },
                    },
                    timeout=60,  # Longer timeout for model loading
                )
            except requests.exceptions.Timeout:
                print(f"[Post-processing] ⚠ Ollama timeout after 60s - model may still be loading")
                raise PostProcessingError("Ollama timeout - model may be loading, try again")
            
            if response.status_code != 200:
                raise PostProcessingError(f"Ollama API error: {response.status_code}")
            
            # Chat API returns message.content instead of response
            response_json = response.json()
            if "message" in response_json:
                result = response_json.get("message", {}).get("content", "").strip()
            else:
                # Fallback for generate API format
                result = response_json.get("response", "").strip()
            
            # Debug: Log response preview
            result_preview = result[:150] + "..." if len(result) > 150 else result
            print(f"[Post-processing] Response: {result_preview}")
            
            # Clean up any artifacts and check for refusals/hallucinations
            result = self._clean_response(result, original_text=text)
            
            return result if result else text
            
        except requests.exceptions.RequestException as e:
            raise PostProcessingError(f"Ollama API call failed: {e}")
        except Exception as e:
            raise PostProcessingError(f"Ollama processing failed: {e}")
    
    def _clean_response(self, text: str, original_text: str = "") -> str:
        """
        Clean up LLM response artifacts and detect refusals/hallucinations.
        
        Args:
            text: The LLM response
            original_text: The original input (returned if refusal/hallucination detected)
        """
        if not text:
            return original_text if original_text else text
        
        # Check for refusal first
        if is_refusal_response(text):
            print("[Post-processing] ⚠ Model refused to process - using original text")
            return original_text if original_text else text
        
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
            # Skip prompt artifacts
            if line.lower().startswith(("spoken text:", "cleaned version:", "cleaned:")):
                continue
            cleaned_lines.append(line)
        
        cleaned = "\n".join(cleaned_lines).strip()
        
        # Check for hallucination (model generated unrelated content)
        if original_text and is_hallucination(original_text, cleaned):
            print("[Post-processing] ⚠ Model hallucinated - using original text")
            return original_text
        
        return cleaned


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
    - output_tone: professional | casual | ai_prompt
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
        {"id": "ai_prompt", "name": "AI Prompt", "description": "Optimized for speaking with AI assistants and LLMs"},
    ]

