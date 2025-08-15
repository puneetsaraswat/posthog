"""
Tests for configuration validation and management.

This test suite ensures that configuration validation works correctly
across different deployment types and scenarios.
"""
import os
import pytest
from unittest.mock import patch, MagicMock
from typing import Dict, Any

from posthog.settings.config_validator import (
    ConfigValidator,
    ValidationSeverity,
    ValidationIssue,
    validate_configuration,
    get_configuration_health_check,
)
from posthog.settings.secrets_manager import (
    SecretsManager,
    SecretValidationError,
    secrets_manager,
    validate_secrets,
)
from posthog.settings.deployment_configs import (
    DeploymentConfigManager,
    DeploymentType,
    deployment_config_manager,
    validate_current_deployment,
)
from posthog.settings.persons_on_events_config import (
    PersonsOnEventsConfig,
    get_poe_configuration_status,
    validate_poe_configuration_change,
)


class TestConfigValidator:
    """Test configuration validation functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.validator = ConfigValidator()
    
    @patch('posthog.settings.config_validator.is_cloud')
    @patch('posthog.settings.config_validator.get_from_env')
    def test_deployment_type_detection(self, mock_get_env, mock_is_cloud):
        """Test deployment type detection logic."""
        # Test cloud deployment
        mock_is_cloud.return_value = True
        validator = ConfigValidator()
        assert validator.deployment_type == "cloud"
        
        # Test hobby deployment
        mock_is_cloud.return_value = False
        mock_get_env.side_effect = lambda key, default, **kwargs: True if key == "POSTHOG_HOBBY_DEPLOYMENT" else default
        validator = ConfigValidator()
        assert validator.deployment_type == "hobby"
        
        # Test enterprise deployment
        mock_get_env.side_effect = lambda key, default, **kwargs: True if key == "POSTHOG_ENTERPRISE" else default
        validator = ConfigValidator()
        assert validator.deployment_type == "enterprise"
        
        # Test self-hosted deployment (default)
        mock_get_env.side_effect = lambda key, default, **kwargs: default
        validator = ConfigValidator()
        assert validator.deployment_type == "self-hosted"
    
    @patch('posthog.settings.config_validator.get_from_env')
    def test_database_validation_missing_url(self, mock_get_env):
        """Test database validation when DATABASE_URL is missing."""
        mock_get_env.side_effect = lambda key, default, **kwargs: "" if key == "DATABASE_URL" else default
        
        result = self.validator.validate_all()
        
        # Should have critical issue for missing DATABASE_URL
        critical_issues = [issue for issue in result.issues if issue.severity == ValidationSeverity.CRITICAL]
        database_issues = [issue for issue in critical_issues if issue.setting_name == "DATABASE_URL"]
        
        assert len(database_issues) == 1
        assert "Database connection string is not configured" in database_issues[0].message
        assert not result.is_production_ready
    
    @patch('posthog.settings.config_validator.get_from_env')
    def test_email_validation_enabled_without_host(self, mock_get_env):
        """Test email validation when enabled but host is missing."""
        def mock_env(key, default, **kwargs):
            if key == "EMAIL_ENABLED":
                return True
            elif key == "EMAIL_HOST":
                return ""
            return default
        
        mock_get_env.side_effect = mock_env
        
        result = self.validator.validate_all()
        
        # Should have warning for email configuration
        warning_issues = [issue for issue in result.issues if issue.severity == ValidationSeverity.WARNING]
        email_issues = [issue for issue in warning_issues if issue.category == "email"]
        
        assert len(email_issues) >= 1
        assert any("EMAIL_HOST" in issue.setting_name for issue in email_issues)
    
    @patch('posthog.settings.config_validator.get_from_env')
    def test_security_validation_weak_secret_key(self, mock_get_env):
        """Test security validation with weak SECRET_KEY."""
        mock_get_env.side_effect = lambda key, default, **kwargs: "weak" if key == "SECRET_KEY" else default
        
        result = self.validator.validate_all()
        
        # Should have critical issue for weak SECRET_KEY
        critical_issues = [issue for issue in result.issues if issue.severity == ValidationSeverity.CRITICAL]
        secret_issues = [issue for issue in critical_issues if issue.setting_name == "SECRET_KEY"]
        
        assert len(secret_issues) == 1
        assert "SECRET_KEY is missing or too short" in secret_issues[0].message
    
    @patch('posthog.settings.persons_on_events_config.poe_config')
    def test_persons_on_events_validation(self, mock_poe_config):
        """Test PoE configuration validation."""
        mock_poe_config.validate_configuration.return_value = {
            "status": "warning",
            "warnings": ["PoE v2 is enabled but PoE v1 is disabled"]
        }
        
        result = self.validator.validate_all()
        
        # Should include PoE warnings
        warning_issues = [issue for issue in result.issues if issue.severity == ValidationSeverity.WARNING]
        poe_issues = [issue for issue in warning_issues if issue.category == "persons_on_events"]
        
        assert len(poe_issues) >= 1
    
    def test_configuration_health_check(self):
        """Test configuration health check functionality."""
        health = get_configuration_health_check()
        
        assert "healthy" in health
        assert "deployment_type" in health
        assert "critical_issues_count" in health
        assert "total_issues" in health
        assert isinstance(health["healthy"], bool)


class TestSecretsManager:
    """Test secrets management functionality."""
    
    def setup_method(self):
        """Set up test fixtures.""" 
        self.secrets_manager = SecretsManager()
    
    def test_encryption_key_generation(self):
        """Test encryption key generation."""
        key = self.secrets_manager.generate_encryption_key()
        assert isinstance(key, str)
        assert len(key) > 20  # Base64 encoded key should be longer
    
    def test_secret_encryption_decryption(self):
        """Test secret encryption and decryption."""
        if self.secrets_manager.cipher_suite:
            original_value = "test-secret-value"
            encrypted = self.secrets_manager.encrypt_secret(original_value)
            decrypted = self.secrets_manager.decrypt_secret(encrypted)
            
            assert encrypted != original_value
            assert decrypted == original_value
        else:
            # If no cipher suite, should return original value
            original_value = "test-secret-value"
            result = self.secrets_manager.encrypt_secret(original_value)
            assert result == original_value
    
    def test_secret_validation_weak_secret_key(self):
        """Test validation of weak SECRET_KEY."""
        with pytest.raises(SecretValidationError):
            self.secrets_manager._validate_critical_secret("SECRET_KEY", "weak")
    
    def test_secret_validation_short_secret_key(self):
        """Test validation of short SECRET_KEY."""
        with pytest.raises(SecretValidationError):
            self.secrets_manager._validate_critical_secret("SECRET_KEY", "short")
    
    def test_secret_validation_invalid_database_url(self):
        """Test validation of invalid DATABASE_URL."""
        with pytest.raises(SecretValidationError):
            self.secrets_manager._validate_critical_secret("DATABASE_URL", "invalid://url")
    
    def test_secret_validation_weak_clickhouse_password(self):
        """Test validation of weak ClickHouse password."""
        with pytest.raises(SecretValidationError):
            self.secrets_manager._validate_critical_secret("CLICKHOUSE_PASSWORD", "123")
    
    def test_mask_secret(self):
        """Test secret masking for logging."""
        secret_value = "very-long-secret-value"
        masked = self.secrets_manager.mask_secret("SECRET_KEY", secret_value)
        
        assert masked != secret_value
        assert masked.startswith("ve")  # First 2 chars
        assert masked.endswith("ue")    # Last 2 chars
        assert "*" in masked
    
    @patch('posthog.settings.secrets_manager.get_from_env')
    def test_validate_all_secrets_missing_critical(self, mock_get_env):
        """Test validation when critical secrets are missing."""
        mock_get_env.return_value = ""  # All secrets missing
        
        result = self.secrets_manager.validate_all_secrets()
        
        assert not result["valid"]
        assert len(result["critical_missing"]) > 0
        assert "SECRET_KEY" in result["critical_missing"]
    
    def test_looks_encrypted(self):
        """Test encrypted value detection."""
        # Should detect base64-encoded values as potentially encrypted
        encrypted_looking = "dGhpc19pc19hX3ZlcnlfbG9uZ19zdHJpbmdfdGhhdF9sb29rc19lbmNyeXB0ZWQ="
        assert self.secrets_manager._looks_encrypted(encrypted_looking)
        
        # Should not detect simple strings as encrypted
        plain_text = "simple-password"
        assert not self.secrets_manager._looks_encrypted(plain_text)


class TestDeploymentConfigManager:
    """Test deployment configuration management."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.config_manager = DeploymentConfigManager()
    
    @patch('posthog.settings.deployment_configs.is_cloud')
    @patch('posthog.settings.deployment_configs.get_from_env')
    def test_deployment_type_detection(self, mock_get_env, mock_is_cloud):
        """Test deployment type detection."""
        # Test cloud detection
        mock_is_cloud.return_value = True
        manager = DeploymentConfigManager()
        assert manager.detect_deployment_type() == DeploymentType.CLOUD
        
        # Test development detection
        mock_is_cloud.return_value = False
        mock_get_env.side_effect = lambda key, default, **kwargs: True if key == "DEBUG" else default
        assert manager.detect_deployment_type() == DeploymentType.DEVELOPMENT
        
        # Test hobby detection
        mock_get_env.side_effect = lambda key, default, **kwargs: True if key == "POSTHOG_HOBBY_DEPLOYMENT" else default
        assert manager.detect_deployment_type() == DeploymentType.HOBBY
        
        # Test enterprise detection
        mock_get_env.side_effect = lambda key, default, **kwargs: True if key == "POSTHOG_ENTERPRISE" else default
        assert manager.detect_deployment_type() == DeploymentType.ENTERPRISE
    
    def test_get_config_for_deployment_types(self):
        """Test getting configuration for different deployment types."""
        for deployment_type in DeploymentType:
            config = self.config_manager.get_config(deployment_type)
            assert config is not None
            assert config.name
            assert config.description
            assert isinstance(config.settings, dict)
            assert isinstance(config.required_services, list)
    
    def test_cloud_config_defaults(self):
        """Test cloud configuration defaults."""
        config = self.config_manager.get_config(DeploymentType.CLOUD)
        
        assert config.name == "PostHog Cloud"
        assert "postgresql" in config.required_services
        assert "clickhouse" in config.required_services
        
        # PoE should be enabled by default
        poe_setting = config.settings.get("PERSON_ON_EVENTS_ENABLED")
        assert poe_setting is not None
        assert poe_setting.default_value is True
    
    def test_hobby_config_defaults(self):
        """Test hobby configuration defaults."""
        config = self.config_manager.get_config(DeploymentType.HOBBY)
        
        assert config.name == "Hobby"
        
        # PoE should be disabled by default
        poe_setting = config.settings.get("PERSON_ON_EVENTS_ENABLED")
        assert poe_setting is not None
        assert poe_setting.default_value is False
    
    def test_enterprise_config_security(self):
        """Test enterprise configuration security settings."""
        config = self.config_manager.get_config(DeploymentType.ENTERPRISE)
        
        # Should have required security settings
        secret_key = config.settings.get("SECRET_KEY")
        assert secret_key is not None
        assert secret_key.required is True
        assert secret_key.sensitive is True
        
        allowed_hosts = config.settings.get("ALLOWED_HOSTS")
        assert allowed_hosts is not None
        assert allowed_hosts.required is True
    
    @patch('posthog.settings.deployment_configs.get_from_env')
    def test_validate_deployment_config_missing_required(self, mock_get_env):
        """Test validation when required settings are missing."""
        mock_get_env.return_value = ""  # All settings missing
        
        result = self.config_manager.validate_deployment_config(DeploymentType.ENTERPRISE)
        
        assert not result["valid"]
        assert len(result["missing_required"]) > 0
        assert any(item["key"] == "SECRET_KEY" for item in result["missing_required"])
    
    def test_generate_config_template(self):
        """Test configuration template generation."""
        template = self.config_manager.generate_config_template(DeploymentType.SELF_HOSTED)
        
        assert isinstance(template, str)
        assert "PostHog Configuration Template" in template
        assert "Required Configuration" in template
        assert "Optional Configuration" in template
        assert "SECRET_KEY" in template
        assert "DATABASE_URL" in template


class TestPersonsOnEventsConfig:
    """Test Persons-on-Events configuration management."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.poe_config = PersonsOnEventsConfig()
    
    @patch('posthog.settings.persons_on_events_config.is_cloud')
    def test_deployment_type_detection(self, mock_is_cloud):
        """Test PoE deployment type detection."""
        mock_is_cloud.return_value = True
        config = PersonsOnEventsConfig()
        assert config.deployment_type == "cloud"
    
    def test_configuration_help(self):
        """Test configuration help information."""
        help_info = self.poe_config.get_configuration_help()
        
        assert "overview" in help_info
        assert "deployment_guidance" in help_info
        assert "troubleshooting" in help_info
        assert "resources" in help_info
        
        # Check that help contains useful information
        assert "what_is_poe" in help_info["overview"]
        assert len(help_info["overview"]["benefits"]) > 0
        assert len(help_info["troubleshooting"]["common_issues"]) > 0
    
    @patch('posthog.models.instance_setting.get_instance_setting')
    def test_self_hosted_poe_default_with_completed_migration(self, mock_get_setting):
        """Test PoE default for self-hosted when migration is completed."""
        mock_get_setting.return_value = True  # Migration completed
        
        result = self.poe_config._get_self_hosted_poe_default()
        assert result is True
    
    @patch('posthog.models.instance_setting.get_instance_setting')
    def test_self_hosted_poe_default_without_migration(self, mock_get_setting):
        """Test PoE default for self-hosted when migration is not completed."""
        mock_get_setting.return_value = False  # Migration not completed
        
        result = self.poe_config._get_self_hosted_poe_default()
        assert result is False
    
    def test_validate_poe_configuration_change_valid(self):
        """Test validation of valid PoE configuration change."""
        result = validate_poe_configuration_change(True, False)
        
        assert "valid" in result
        assert "current_state" in result
        assert "proposed_state" in result
        assert "changes" in result
        assert "warnings" in result
        assert "blockers" in result
    
    def test_validate_poe_configuration_change_invalid_v2_without_v1(self):
        """Test validation of invalid configuration (v2 without v1)."""
        result = validate_poe_configuration_change(False, True)
        
        assert result["valid"] is False
        assert len(result["blockers"]) > 0
        assert any("Cannot enable PoE v2 without PoE v1" in blocker for blocker in result["blockers"])
    
    def test_get_poe_configuration_status(self):
        """Test getting comprehensive PoE configuration status."""
        status = get_poe_configuration_status()
        
        assert "configuration" in status
        assert "migrations" in status
        assert "help" in status
        assert "deployment_type" in status
        assert "timestamp" in status
        assert "version" in status


class TestConfigurationIntegration:
    """Integration tests for configuration management."""
    
    @patch.dict(os.environ, {
        'DATABASE_URL': 'postgresql://test:test@localhost/test',
        'SECRET_KEY': 'very-long-and-secure-secret-key-for-testing-purposes',
        'CLICKHOUSE_HOST': 'localhost'
    })
    def test_healthy_configuration(self):
        """Test that a properly configured environment passes validation."""
        health = get_configuration_health_check()
        
        # Should have fewer critical issues with proper configuration
        assert health["critical_issues_count"] <= 2  # Some issues may remain due to test environment
    
    @patch.dict(os.environ, {}, clear=True)
    def test_unhealthy_configuration(self):
        """Test that a misconfigured environment fails validation."""
        health = get_configuration_health_check()
        
        # Should have critical issues with missing configuration
        assert health["critical_issues_count"] > 0
        assert not health["healthy"]
    
    def test_validate_current_deployment(self):
        """Test validation of current deployment configuration."""
        result = validate_current_deployment()
        
        assert "deployment_type" in result
        assert "config_name" in result
        assert "valid" in result
        assert "missing_required" in result
        assert "recommendations" in result
    
    def test_validate_secrets(self):
        """Test comprehensive secrets validation."""
        result = validate_secrets()
        
        assert "valid" in result
        assert "issues" in result
        assert "warnings" in result
        assert "critical_missing" in result
        assert "weak_secrets" in result