"""
Configuration validation for PostHog deployments.

This module provides comprehensive validation for PostHog configuration settings,
ensuring that deployments are properly configured for their environment and use case.
"""

import os
import re
from enum import Enum
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from urllib.parse import urlparse

from posthog.cloud_utils import is_cloud
from posthog.settings.utils import get_from_env, str_to_bool


class ValidationSeverity(Enum):
    """Severity levels for configuration validation issues."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class ValidationIssue:
    """Represents a configuration validation issue."""
    severity: ValidationSeverity
    setting_name: str
    message: str
    category: str = "general"
    recommendation: str = ""
    documentation_url: str = ""


@dataclass
class ValidationResult:
    """Results of configuration validation."""
    issues: List[ValidationIssue] = field(default_factory=list)
    is_production_ready: bool = True
    deployment_type: str = "unknown"
    
    @property
    def critical_issues(self) -> List[ValidationIssue]:
        """Get all critical issues."""
        return [issue for issue in self.issues if issue.severity == ValidationSeverity.CRITICAL]
    
    @property
    def warning_issues(self) -> List[ValidationIssue]:
        """Get all warning issues."""
        return [issue for issue in self.issues if issue.severity == ValidationSeverity.WARNING]
    
    @property
    def info_issues(self) -> List[ValidationIssue]:
        """Get all info issues."""
        return [issue for issue in self.issues if issue.severity == ValidationSeverity.INFO]


class ConfigValidator:
    """Main configuration validator class."""
    
    def __init__(self):
        self.deployment_type = self._detect_deployment_type()
    
    def _detect_deployment_type(self) -> str:
        """Detect the current deployment type."""
        if is_cloud():
            return "cloud"
        
        if get_from_env("DEBUG", False, type_cast=str_to_bool):
            return "development"
        
        if get_from_env("POSTHOG_HOBBY_DEPLOYMENT", False, type_cast=str_to_bool):
            return "hobby"
        
        if get_from_env("POSTHOG_ENTERPRISE", False, type_cast=str_to_bool):
            return "enterprise"
        
        return "self-hosted"
    
    def validate_all(self) -> ValidationResult:
        """Validate all configuration aspects."""
        result = ValidationResult(deployment_type=self.deployment_type)
        
        # Run all validation checks
        result.issues.extend(self._validate_database())
        result.issues.extend(self._validate_security())
        result.issues.extend(self._validate_email())
        result.issues.extend(self._validate_clickhouse())
        result.issues.extend(self._validate_redis())
        result.issues.extend(self._validate_persons_on_events())
        result.issues.extend(self._validate_deployment_specific())
        
        # Determine if production ready
        result.is_production_ready = len(result.critical_issues) == 0
        
        return result
    
    def _validate_database(self) -> List[ValidationIssue]:
        """Validate database configuration."""
        issues = []
        
        database_url = get_from_env("DATABASE_URL", "")
        if not database_url:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.CRITICAL,
                setting_name="DATABASE_URL",
                message="Database connection string is not configured",
                category="database",
                recommendation="Set DATABASE_URL environment variable with PostgreSQL connection string",
                documentation_url="https://posthog.com/docs/self-host/configure/environment-variables#database_url"
            ))
        else:
            # Validate database URL format
            try:
                parsed = urlparse(database_url)
                if not parsed.scheme.startswith('postgres'):
                    issues.append(ValidationIssue(
                        severity=ValidationSeverity.WARNING,
                        setting_name="DATABASE_URL",
                        message="Database URL should use PostgreSQL (postgresql:// or postgres://)",
                        category="database",
                        recommendation="Ensure DATABASE_URL uses PostgreSQL scheme"
                    ))
            except Exception:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    setting_name="DATABASE_URL",
                    message="Invalid database URL format",
                    category="database",
                    recommendation="Ensure DATABASE_URL is a valid PostgreSQL connection string"
                ))
        
        return issues
    
    def _validate_security(self) -> List[ValidationIssue]:
        """Validate security configuration."""
        issues = []
        
        # Check SECRET_KEY
        secret_key = get_from_env("SECRET_KEY", "")
        if not secret_key or len(secret_key) < 50:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.CRITICAL,
                setting_name="SECRET_KEY",
                message="SECRET_KEY is missing or too short (should be at least 50 characters)",
                category="security",
                recommendation="Generate a strong SECRET_KEY using Django's get_random_secret_key() or similar",
                documentation_url="https://posthog.com/docs/self-host/configure/securing-posthog"
            ))
        
        # Check ALLOWED_HOSTS for production
        if self.deployment_type in ["self-hosted", "enterprise"]:
            allowed_hosts = get_from_env("ALLOWED_HOSTS", "")
            if not allowed_hosts or allowed_hosts == "*":
                severity = ValidationSeverity.CRITICAL if self.deployment_type == "enterprise" else ValidationSeverity.WARNING
                issues.append(ValidationIssue(
                    severity=severity,
                    setting_name="ALLOWED_HOSTS",
                    message="ALLOWED_HOSTS should be configured with specific domains for production",
                    category="security",
                    recommendation="Set ALLOWED_HOSTS to your domain(s), e.g., 'yourdomain.com,www.yourdomain.com'",
                    documentation_url="https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts"
                ))
        
        # Check CORS settings
        cors_origin_allow_all = get_from_env("CORS_ORIGIN_ALLOW_ALL", False, type_cast=str_to_bool)
        if cors_origin_allow_all and self.deployment_type in ["enterprise", "self-hosted"]:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                setting_name="CORS_ORIGIN_ALLOW_ALL",
                message="CORS is set to allow all origins, which may be insecure for production",
                category="security",
                recommendation="Configure specific CORS origins using CORS_ORIGIN_WHITELIST"
            ))
        
        return issues
    
    def _validate_email(self) -> List[ValidationIssue]:
        """Validate email configuration."""
        issues = []
        
        email_enabled = get_from_env("EMAIL_ENABLED", True, type_cast=str_to_bool)
        if email_enabled:
            email_host = get_from_env("EMAIL_HOST", "")
            if not email_host:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    setting_name="EMAIL_HOST",
                    message="Email is enabled but EMAIL_HOST is not configured",
                    category="email",
                    recommendation="Configure EMAIL_HOST for email functionality",
                    documentation_url="https://posthog.com/docs/self-host/configure/environment-variables#email"
                ))
            
            # Check for common misconfigurations
            email_port = get_from_env("EMAIL_PORT", 25, type_cast=int)
            email_use_tls = get_from_env("EMAIL_USE_TLS", False, type_cast=str_to_bool)
            email_use_ssl = get_from_env("EMAIL_USE_SSL", False, type_cast=str_to_bool)
            
            if email_use_tls and email_use_ssl:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    setting_name="EMAIL_USE_TLS",
                    message="Both EMAIL_USE_TLS and EMAIL_USE_SSL are enabled",
                    category="email",
                    recommendation="Use either TLS or SSL, not both"
                ))
            
            if email_port == 587 and not email_use_tls:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    setting_name="EMAIL_USE_TLS",
                    message="Port 587 typically requires TLS",
                    category="email",
                    recommendation="Enable EMAIL_USE_TLS for port 587"
                ))
            
            if email_port == 465 and not email_use_ssl:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    setting_name="EMAIL_USE_SSL",
                    message="Port 465 typically requires SSL",
                    category="email",
                    recommendation="Enable EMAIL_USE_SSL for port 465"
                ))
        
        return issues
    
    def _validate_clickhouse(self) -> List[ValidationIssue]:
        """Validate ClickHouse configuration."""
        issues = []
        
        clickhouse_host = get_from_env("CLICKHOUSE_HOST", "")
        if not clickhouse_host:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.CRITICAL,
                setting_name="CLICKHOUSE_HOST",
                message="ClickHouse host is not configured",
                category="clickhouse",
                recommendation="Set CLICKHOUSE_HOST environment variable",
                documentation_url="https://posthog.com/docs/self-host/configure/environment-variables#clickhouse"
            ))
        
        # Check ClickHouse credentials
        clickhouse_user = get_from_env("CLICKHOUSE_USER", "")
        clickhouse_password = get_from_env("CLICKHOUSE_PASSWORD", "")
        
        if clickhouse_user and not clickhouse_password:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                setting_name="CLICKHOUSE_PASSWORD",
                message="ClickHouse user is set but password is missing",
                category="clickhouse",
                recommendation="Set CLICKHOUSE_PASSWORD for secure ClickHouse access"
            ))
        
        if clickhouse_password and len(clickhouse_password) < 8:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.WARNING,
                setting_name="CLICKHOUSE_PASSWORD",
                message="ClickHouse password is too short",
                category="clickhouse",
                recommendation="Use a stronger password (at least 8 characters)"
            ))
        
        return issues
    
    def _validate_redis(self) -> List[ValidationIssue]:
        """Validate Redis configuration."""
        issues = []
        
        redis_url = get_from_env("REDIS_URL", "")
        if not redis_url:
            issues.append(ValidationIssue(
                severity=ValidationSeverity.CRITICAL,
                setting_name="REDIS_URL",
                message="Redis connection string is not configured",
                category="redis",
                recommendation="Set REDIS_URL environment variable",
                documentation_url="https://posthog.com/docs/self-host/configure/environment-variables#redis_url"
            ))
        else:
            # Validate Redis URL format
            try:
                parsed = urlparse(redis_url)
                if not parsed.scheme.startswith('redis'):
                    issues.append(ValidationIssue(
                        severity=ValidationSeverity.WARNING,
                        setting_name="REDIS_URL",
                        message="Redis URL should use redis:// or rediss:// scheme",
                        category="redis",
                        recommendation="Ensure REDIS_URL uses proper Redis scheme"
                    ))
            except Exception:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    setting_name="REDIS_URL",
                    message="Invalid Redis URL format",
                    category="redis",
                    recommendation="Ensure REDIS_URL is a valid Redis connection string"
                ))
        
        return issues
    
    def _validate_persons_on_events(self) -> List[ValidationIssue]:
        """Validate Persons-on-Events configuration."""
        issues = []
        
        # Import here to avoid circular imports
        try:
            from posthog.settings.persons_on_events_config import poe_config
            poe_validation = poe_config.validate_configuration()
            
            if poe_validation["status"] == "error":
                for error in poe_validation["errors"]:
                    issues.append(ValidationIssue(
                        severity=ValidationSeverity.CRITICAL,
                        setting_name="PERSON_ON_EVENTS",
                        message=error,
                        category="persons_on_events",
                        recommendation="Review Persons-on-Events configuration"
                    ))
            
            if poe_validation["status"] == "warning":
                for warning in poe_validation["warnings"]:
                    issues.append(ValidationIssue(
                        severity=ValidationSeverity.WARNING,
                        setting_name="PERSON_ON_EVENTS",
                        message=warning,
                        category="persons_on_events",
                        recommendation="Review Persons-on-Events configuration"
                    ))
        
        except ImportError:
            # PoE config module doesn't exist yet, check basic configuration
            poe_enabled = get_from_env("PERSON_ON_EVENTS_ENABLED", False, type_cast=str_to_bool)
            poe_v2_enabled = get_from_env("PERSON_ON_EVENTS_V2_ENABLED", False, type_cast=str_to_bool)
            
            if poe_v2_enabled and not poe_enabled:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    setting_name="PERSON_ON_EVENTS_V2_ENABLED",
                    message="PoE v2 is enabled but PoE v1 is disabled",
                    category="persons_on_events",
                    recommendation="Enable PERSON_ON_EVENTS_ENABLED when using v2"
                ))
            
            # For self-hosted, provide guidance on PoE configuration
            if self.deployment_type in ["self-hosted", "hobby"]:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    setting_name="PERSON_ON_EVENTS_ENABLED",
                    message=f"Persons-on-Events is {'enabled' if poe_enabled else 'disabled'} for {self.deployment_type} deployment",
                    category="persons_on_events",
                    recommendation="Consider enabling PoE for better query performance after data migration",
                    documentation_url="https://posthog.com/docs/self-host/configure/environment-variables#persons-on-events"
                ))
        
        return issues
    
    def _validate_deployment_specific(self) -> List[ValidationIssue]:
        """Validate deployment-specific configuration."""
        issues = []
        
        if self.deployment_type == "hobby":
            # Hobby deployments should have resource limits
            if not get_from_env("WORKER_CONCURRENCY", ""):
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    setting_name="WORKER_CONCURRENCY",
                    message="Consider setting WORKER_CONCURRENCY for hobby deployments",
                    category="performance",
                    recommendation="Set WORKER_CONCURRENCY to limit resource usage (e.g., '1' or '2')"
                ))
        
        elif self.deployment_type == "enterprise":
            # Enterprise deployments need specific configurations
            if not get_from_env("SITE_URL", "").startswith("https://"):
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.CRITICAL,
                    setting_name="SITE_URL",
                    message="Enterprise deployments should use HTTPS",
                    category="security",
                    recommendation="Configure SITE_URL with https:// scheme"
                ))
        
        elif self.deployment_type == "development":
            # Development-specific warnings
            if get_from_env("SECRET_KEY", "") == "django-insecure-dev-key":
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    setting_name="SECRET_KEY",
                    message="Using default development SECRET_KEY",
                    category="development",
                    recommendation="Generate a unique SECRET_KEY for development consistency"
                ))
        
        return issues


def validate_configuration() -> ValidationResult:
    """Validate the current PostHog configuration."""
    validator = ConfigValidator()
    return validator.validate_all()


def get_configuration_health_check() -> Dict[str, Any]:
    """Get a simple health check summary of configuration status."""
    result = validate_configuration()
    
    return {
        "healthy": result.is_production_ready,
        "deployment_type": result.deployment_type,
        "critical_issues_count": len(result.critical_issues),
        "warning_issues_count": len(result.warning_issues),
        "total_issues": len(result.issues),
        "timestamp": os.environ.get("DEPLOYMENT_TIMESTAMP", "unknown")
    }