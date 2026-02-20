"""Configuration management for OpenFOIA.

Supports:
- Local config file (~/.openfoia/config.json)
- Environment variables (OPENFOIA_*)
- CLI overrides

Secrets (API keys, tokens) can be provided via:
1. Environment variables (recommended for CI/scripts)
2. Config file with underscore prefix (e.g., "_api_key" - not committed)
3. Interactive prompts on first run
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".openfoia" / "config.json"


@dataclass
class AIConfig:
    """AI/LLM configuration."""
    
    provider: str = "ollama"  # ollama, anthropic, openai
    model: str = "llama3.2"
    base_url: str | None = None
    api_key: str | None = None
    
    # For entity extraction
    extraction_temperature: float = 0.1
    extraction_max_tokens: int = 4096


@dataclass
class OCRConfig:
    """OCR configuration."""
    
    backend: str = "tesseract"  # tesseract, google, aws
    
    # Tesseract options
    tesseract_cmd: str | None = None
    tesseract_lang: str = "eng"
    
    # Google Cloud Vision
    google_credentials_file: str | None = None
    
    # AWS Textract
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None


@dataclass
class GatewayConfig:
    """Delivery gateway configuration."""
    
    # Email
    email_enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    from_name: str = "FOIA Requester"
    from_email: str | None = None
    
    # Fax (Twilio)
    fax_enabled: bool = False
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    
    # Mail (Lob)
    mail_enabled: bool = False
    lob_api_key: str | None = None
    return_address: dict[str, str] = field(default_factory=dict)


@dataclass
class EntityConfig:
    """Entity extraction configuration."""
    
    # Built-in entity types (always enabled)
    builtin_types: list[str] = field(default_factory=lambda: [
        "PERSON",
        "ORGANIZATION",
        "LOCATION",
        "DATE",
        "MONEY",
        "DOCUMENT_ID",
        "PHONE",
        "EMAIL",
        "ADDRESS",
    ])
    
    # Custom entity types defined by user
    custom_types: list[dict[str, str]] = field(default_factory=list)
    
    # Additional prompt instructions for extraction
    extraction_prompt_suffix: str = ""


@dataclass
class PrivacyConfig:
    """Privacy settings."""
    
    browser_default: str | None = None  # brave, firefox, safari, tor, etc.
    always_private_mode: bool = True
    auto_redact_pii_in_exports: bool = False
    delete_processed_originals: bool = False


@dataclass
class ServerConfig:
    """Server configuration."""
    
    host: str = "127.0.0.1"
    port: int = 0  # 0 = random


@dataclass
class OpenFOIAConfig:
    """Main configuration container."""
    
    ai: AIConfig = field(default_factory=AIConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    gateways: GatewayConfig = field(default_factory=GatewayConfig)
    entities: EntityConfig = field(default_factory=EntityConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    
    # Data directory
    data_dir: Path = field(default_factory=lambda: Path.home() / ".openfoia")


def load_config(
    config_path: Path | str | None = None,
    env_prefix: str = "OPENFOIA_",
) -> OpenFOIAConfig:
    """Load configuration from file and environment.
    
    Priority (highest to lowest):
    1. Environment variables
    2. Config file
    3. Defaults
    """
    config = OpenFOIAConfig()
    
    # Load from file
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            config = _merge_config(config, data)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Failed to load config from {path}: {e}")
    
    # Override with environment variables
    config = _apply_env_overrides(config, env_prefix)
    
    # Ensure data directory exists
    config.data_dir.mkdir(parents=True, exist_ok=True)
    
    return config


def _merge_config(config: OpenFOIAConfig, data: dict[str, Any]) -> OpenFOIAConfig:
    """Merge loaded data into config object."""
    
    if "ai" in data:
        ai = data["ai"]
        config.ai.provider = ai.get("provider", config.ai.provider)
        config.ai.model = ai.get("model") or ai.get(ai.get("provider", ""), {}).get("model", config.ai.model)
        config.ai.base_url = ai.get("base_url") or ai.get(ai.get("provider", ""), {}).get("base_url")
        config.ai.api_key = ai.get("api_key") or ai.get("_api_key") or ai.get(ai.get("provider", ""), {}).get("api_key")
        
        # Handle ollama specifically
        if config.ai.provider == "ollama" and "ollama" in ai:
            config.ai.base_url = ai["ollama"].get("base_url", "http://localhost:11434")
            config.ai.model = ai["ollama"].get("model", "llama3.2")
    
    if "ocr" in data:
        ocr = data["ocr"]
        config.ocr.backend = ocr.get("backend", config.ocr.backend)
        config.ocr.tesseract_cmd = ocr.get("tesseract_cmd")
        config.ocr.google_credentials_file = ocr.get("credentials_file")
        config.ocr.aws_region = ocr.get("region", config.ocr.aws_region)
        config.ocr.aws_access_key_id = ocr.get("access_key_id") or ocr.get("_access_key_id")
        config.ocr.aws_secret_access_key = ocr.get("secret_access_key") or ocr.get("_secret_access_key")
    
    if "gateways" in data:
        gw = data["gateways"]
        
        if "email" in gw:
            e = gw["email"]
            config.gateways.email_enabled = True
            config.gateways.smtp_host = e.get("smtp_host", config.gateways.smtp_host)
            config.gateways.smtp_port = e.get("smtp_port", config.gateways.smtp_port)
            config.gateways.smtp_user = e.get("smtp_user")
            config.gateways.smtp_password = e.get("smtp_password") or e.get("_smtp_password")
            config.gateways.from_name = e.get("from_name", config.gateways.from_name)
            config.gateways.from_email = e.get("from_email") or config.gateways.smtp_user
        
        if "fax" in gw:
            f = gw["fax"]
            config.gateways.fax_enabled = True
            config.gateways.twilio_account_sid = f.get("account_sid") or f.get("_account_sid")
            config.gateways.twilio_auth_token = f.get("auth_token") or f.get("_auth_token")
            config.gateways.twilio_from_number = f.get("from_number")
        
        if "mail" in gw:
            m = gw["mail"]
            config.gateways.mail_enabled = True
            config.gateways.lob_api_key = m.get("api_key") or m.get("_api_key")
            config.gateways.return_address = m.get("return_address", {})
    
    if "entities" in data:
        ent = data["entities"]
        config.entities.custom_types = ent.get("custom_types", [])
        config.entities.extraction_prompt_suffix = ent.get("extraction_prompt_suffix", "")
    
    if "privacy" in data:
        priv = data["privacy"]
        config.privacy.browser_default = priv.get("browser_default")
        config.privacy.always_private_mode = priv.get("always_private_mode", True)
        config.privacy.auto_redact_pii_in_exports = priv.get("auto_redact_pii_in_exports", False)
        config.privacy.delete_processed_originals = priv.get("delete_processed_originals", False)
    
    if "server" in data:
        srv = data["server"]
        config.server.host = srv.get("host", config.server.host)
        config.server.port = srv.get("port", config.server.port)
    
    return config


def _apply_env_overrides(config: OpenFOIAConfig, prefix: str) -> OpenFOIAConfig:
    """Apply environment variable overrides."""
    
    # AI
    if v := os.environ.get(f"{prefix}AI_PROVIDER"):
        config.ai.provider = v
    if v := os.environ.get(f"{prefix}AI_MODEL"):
        config.ai.model = v
    if v := os.environ.get(f"{prefix}AI_API_KEY"):
        config.ai.api_key = v
    if v := os.environ.get(f"{prefix}AI_BASE_URL"):
        config.ai.base_url = v
    
    # Specific providers
    if v := os.environ.get(f"{prefix}ANTHROPIC_API_KEY"):
        config.ai.provider = "anthropic"
        config.ai.api_key = v
    if v := os.environ.get(f"{prefix}OPENAI_API_KEY"):
        config.ai.provider = "openai"
        config.ai.api_key = v
    if v := os.environ.get(f"{prefix}OLLAMA_BASE_URL"):
        config.ai.provider = "ollama"
        config.ai.base_url = v
    
    # OCR
    if v := os.environ.get(f"{prefix}OCR_BACKEND"):
        config.ocr.backend = v
    
    # Gateways
    if v := os.environ.get(f"{prefix}TWILIO_ACCOUNT_SID"):
        config.gateways.fax_enabled = True
        config.gateways.twilio_account_sid = v
    if v := os.environ.get(f"{prefix}TWILIO_AUTH_TOKEN"):
        config.gateways.twilio_auth_token = v
    if v := os.environ.get(f"{prefix}LOB_API_KEY"):
        config.gateways.mail_enabled = True
        config.gateways.lob_api_key = v
    
    return config


def save_config(config: OpenFOIAConfig, config_path: Path | str | None = None) -> None:
    """Save configuration to file (excludes secrets)."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    
    data = {
        "ai": {
            "provider": config.ai.provider,
            "model": config.ai.model,
        },
        "ocr": {
            "backend": config.ocr.backend,
        },
        "entities": {
            "custom_types": config.entities.custom_types,
            "extraction_prompt_suffix": config.entities.extraction_prompt_suffix,
        },
        "privacy": {
            "browser_default": config.privacy.browser_default,
            "always_private_mode": config.privacy.always_private_mode,
            "auto_redact_pii_in_exports": config.privacy.auto_redact_pii_in_exports,
        },
        "server": {
            "host": config.server.host,
            "port": config.server.port,
        },
    }
    
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
