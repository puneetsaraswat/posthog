"""
Secrets management for PostHog deployments.

This module provides secure handling of sensitive configuration data,
including encryption, validation, and safe storage of secrets.
"""

import base64
import hashlib
import os
import re
from typing import Dict, List, Any, Optional, Union
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from posthog.settings.utils import get_from_env, str_to_bool


class SecretValidationError(Exception):
    """Raised when secret validation fails."""
    pass


class SecretsManager:
    """Manages secure handling of configuration secrets."""
    
    def __init__(self):
        self.encryption_key = get_from_env("SECRET_ENCRYPTION_KEY", "")
        self.cipher_suite = self._initialize_cipher() if self.encryption_key else None
    
    def _initialize_cipher(self) -> Optional[Fernet]:
        """Initialize encryption cipher from the encryption key."""
        try:
            # Use the encryption key to derive a Fernet key
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'posthog_salt',  # In production, use a random salt
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(self.encryption_key.encode()))
            return Fernet(key)
        except Exception:
            return None
    
    def generate_encryption_key(self) -> str:
        """Generate a new encryption key for secrets."""
        key = os.urandom(32)
        return base64.urlsafe_b64encode(key).decode()
    
    def encrypt_secret(self, value: str) -> str:
        """Encrypt a secret value."""
        if not self.cipher_suite:
            return value  # Return original if encryption not available
        
        try:
            encrypted = self.cipher_suite.encrypt(value.encode())
            return base64.urlsafe_b64encode(encrypted).decode()
        except Exception:
            return value  # Fallback to original value if encryption fails
    
    def decrypt_secret(self, encrypted_value: str) -> str:
        """Decrypt a secret value."""
        if not self.cipher_suite:
            return encrypted_value  # Return as-is if encryption not available
        
        try:
            decoded = base64.urlsafe_b64decode(encrypted_value.encode())
            decrypted = self.cipher_suite.decrypt(decoded)
            return decrypted.decode()
        except Exception:
            return encrypted_value  # Fallback to original if decryption fails
    
    def mask_secret(self, setting_name: str, value: str, mask_char: str = "*") -> str:
        """Mask a secret value for safe logging/display."""
        if not value:
            return "[NOT SET]"
        
        if len(value) <= 4:
            return mask_char * len(value)
        
        # Show first 2 and last 2 characters
        visible_start = 2
        visible_end = 2
        
        # For very long values, show more
        if len(value) > 20:
            visible_start = 4
            visible_end = 4
        
        masked_middle = mask_char * max(1, len(value) - visible_start - visible_end)
        return f"{value[:visible_start]}{masked_middle}{value[-visible_end:]}"
    
    def _looks_encrypted(self, value: str) -> bool:
        """Determine if a value looks like it might be encrypted."""
        if not value:
            return False
        
        # Check if it looks like base64 encoded data
        try:
            if len(value) > 20 and len(value) % 4 == 0:
                base64.urlsafe_b64decode(value.encode())
                return True
        except Exception:
            pass
        
        # Check for certain patterns that suggest encryption
        encryption_patterns = [
            r'^[A-Za-z0-9+/]{40,}={0,2}$',  # Base64 pattern
            r'^[A-Za-z0-9_-]{40,}$',        # URL-safe base64 pattern
        ]
        
        for pattern in encryption_patterns:
            if re.match(pattern, value):
                return True
        
        return False
    
    def _validate_critical_secret(self, setting_name: str, value: str) -> None:
        """Validate a critical secret value."""
        if setting_name == "SECRET_KEY":
            if not value:
                raise SecretValidationError("SECRET_KEY is required")
            if len(value) < 50:
                raise SecretValidationError("SECRET_KEY must be at least 50 characters long")
            if value in ["changeme", "secret", "password", "django-insecure-dev-key"]:
                raise SecretValidationError("SECRET_KEY appears to be a default or weak value")
        
        elif setting_name == "DATABASE_URL":
            if not value:
                raise SecretValidationError("DATABASE_URL is required")
            try:
                parsed = urlparse(value)
                if not parsed.scheme.startswith('postgres'):
                    raise SecretValidationError("DATABASE_URL must be a PostgreSQL connection string")
                if not parsed.hostname:
                    raise SecretValidationError("DATABASE_URL must include a hostname")
            except Exception as e:
                raise SecretValidationError(f"Invalid DATABASE_URL format: {e}")
        
        elif setting_name == "CLICKHOUSE_PASSWORD":
            if value and len(value) < 8:
                raise SecretValidationError("ClickHouse password should be at least 8 characters")
            if value in ["password", "123", "admin", "clickhouse"]:
                raise SecretValidationError("ClickHouse password appears to be weak")
        
        elif setting_name in ["EMAIL_HOST_PASSWORD", "SLACK_APP_CLIENT_SECRET"]:
            if value and len(value) < 6:
                raise SecretValidationError(f"{setting_name} appears to be too short")
    
    def validate_all_secrets(self) -> Dict[str, Any]:
        """Validate all configured secrets."""
        # Define critical secrets that must be validated
        critical_secrets = {
            "SECRET_KEY": get_from_env("SECRET_KEY", ""),
            "DATABASE_URL": get_from_env("DATABASE_URL", ""),
        }
        
        # Optional secrets that should be validated if present
        optional_secrets = {
            "CLICKHOUSE_PASSWORD": get_from_env("CLICKHOUSE_PASSWORD", ""),
            "EMAIL_HOST_PASSWORD": get_from_env("EMAIL_HOST_PASSWORD", ""),
            "SLACK_APP_CLIENT_SECRET": get_from_env("SLACK_APP_CLIENT_SECRET", ""),
            "GITHUB_TOKEN": get_from_env("GITHUB_TOKEN", ""),
            "GITLAB_TOKEN": get_from_env("GITLAB_TOKEN", ""),
        }
        
        result = {
            "valid": True,
            "critical_missing": [],
            "weak_secrets": [],
            "warnings": [],
            "encrypted_secrets": [],
            "issues": []
        }
        
        # Validate critical secrets
        for name, value in critical_secrets.items():
            if not value:
                result["critical_missing"].append(name)
                result["valid"] = False
                result["issues"].append(f"Critical secret {name} is not configured")
            else:
                try:
                    self._validate_critical_secret(name, value)
                    if self._looks_encrypted(value):
                        result["encrypted_secrets"].append(name)
                except SecretValidationError as e:
                    result["weak_secrets"].append({"name": name, "issue": str(e)})
                    result["issues"].append(f"{name}: {e}")
        
        # Validate optional secrets
        for name, value in optional_secrets.items():
            if value:
                try:
                    self._validate_critical_secret(name, value)
                    if self._looks_encrypted(value):
                        result["encrypted_secrets"].append(name)
                except SecretValidationError as e:
                    result["warnings"].append(f"{name}: {e}")
        
        return result
    
    def get_secrets_summary(self) -> Dict[str, Any]:
        """Get a summary of secrets configuration (safely masked)."""
        secrets = {
            "SECRET_KEY": get_from_env("SECRET_KEY", ""),
            "DATABASE_URL": get_from_env("DATABASE_URL", ""),
            "CLICKHOUSE_PASSWORD": get_from_env("CLICKHOUSE_PASSWORD", ""),
            "REDIS_URL": get_from_env("REDIS_URL", ""),
            "EMAIL_HOST_PASSWORD": get_from_env("EMAIL_HOST_PASSWORD", ""),
            "SLACK_APP_CLIENT_SECRET": get_from_env("SLACK_APP_CLIENT_SECRET", ""),
        }
        
        summary = {}
        for name, value in secrets.items():
            summary[name] = {
                "configured": bool(value),
                "masked_value": self.mask_secret(name, value),
                "looks_encrypted": self._looks_encrypted(value) if value else False,
                "length": len(value) if value else 0
            }
        
        return summary
    
    def generate_secrets_template(self, deployment_type: str = "self-hosted") -> str:
        """Generate a template for secrets configuration."""
        template = f"""# PostHog Secrets Configuration Template
# Deployment Type: {deployment_type}
# 
# CRITICAL: Keep this file secure and never commit it to version control!
# 
# Instructions:
# 1. Copy this template to a secure location
# 2. Fill in all required values
# 3. Use strong, unique passwords for all secrets
# 4. Consider using encrypted storage or secrets management systems

# === REQUIRED SECRETS ===

# Django Secret Key (Required)
# Generate with: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
SECRET_KEY=""

# Database Connection (Required)
# Format: postgresql://username:password@host:port/database
DATABASE_URL=""

# === CLICKHOUSE CONFIGURATION ===

# ClickHouse Database (Required for production)
CLICKHOUSE_HOST=""
CLICKHOUSE_USER=""
CLICKHOUSE_PASSWORD=""

# === REDIS CONFIGURATION ===

# Redis Connection (Required)
# Format: redis://username:password@host:port/database
REDIS_URL=""

# === EMAIL CONFIGURATION ===

# Email Service (Optional but recommended)
EMAIL_HOST=""
EMAIL_PORT="587"
EMAIL_HOST_USER=""
EMAIL_HOST_PASSWORD=""
EMAIL_USE_TLS="true"
EMAIL_DEFAULT_FROM=""

# === SECURITY CONFIGURATION ===

# Domain Configuration
SITE_URL=""
ALLOWED_HOSTS=""

# === OPTIONAL INTEGRATIONS ===

# Slack Integration (Optional)
SLACK_APP_CLIENT_ID=""
SLACK_APP_CLIENT_SECRET=""
SLACK_APP_SIGNING_SECRET=""

# GitHub/GitLab Tokens for Plugin Installation (Optional)
GITHUB_TOKEN=""
GITLAB_TOKEN=""

# === ENCRYPTION ===

# Secret Encryption Key (Optional but recommended)
# Generate with: python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
SECRET_ENCRYPTION_KEY=""
"""
        
        return template


# Global instance
secrets_manager = SecretsManager()


def validate_secrets() -> Dict[str, Any]:
    """Validate all secrets configuration."""
    return secrets_manager.validate_all_secrets()


def get_masked_secrets_summary() -> Dict[str, Any]:
    """Get a safely masked summary of secrets configuration."""
    return secrets_manager.get_secrets_summary()


def encrypt_secret(value: str) -> str:
    """Encrypt a secret value."""
    return secrets_manager.encrypt_secret(value)


def decrypt_secret(encrypted_value: str) -> str:
    """Decrypt a secret value."""
    return secrets_manager.decrypt_secret(encrypted_value)