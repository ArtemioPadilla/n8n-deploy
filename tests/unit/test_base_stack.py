"""Unit tests for base stack."""

import pytest
from aws_cdk import RemovalPolicy
from aws_cdk.assertions import Template

from n8n_deploy.stacks.base_stack import N8nBaseStack


class TestBaseStack:
    """Test base stack functionality."""

    def test_base_stack_initialization(self, mock_app, test_config):
        """Test base stack initialization."""
        stack = N8nBaseStack(mock_app, "test-stack", config=test_config, environment="test")

        assert stack.environment_name == "test"
        assert stack.config == test_config
        assert stack.env_config == test_config.get_environment("test")
        assert stack.stack_prefix == "test-n8n-test"

    @pytest.mark.skip(reason="Template synthesis requires valid AWS environment format")
    def test_tag_application(self, mock_app, test_config):
        """Test that tags are properly applied."""
        stack = N8nBaseStack(mock_app, "test-stack", config=test_config, environment="test")

        Template.from_stack(stack)

        # Check that tags are present in the stack template
        # Note: CDK applies tags at synthesis time, so we'd need to verify
        # through the synthesized template or use CDK's tag APIs
        assert stack.node.find_all()  # Verify stack has nodes

    def test_resource_naming(self, mock_app, test_config):
        """Test resource naming convention."""
        stack = N8nBaseStack(mock_app, "test-stack", config=test_config, environment="test")

        # Test resource naming
        assert stack.get_resource_name("vpc") == "test-n8n-test-vpc"
        assert stack.get_resource_name("vpc", "main") == "test-n8n-test-vpc-main"
        assert stack.get_resource_name("sg", "n8n") == "test-n8n-test-sg-n8n"

    def test_removal_policy(self, mock_app, test_config):
        """Test removal policy based on environment."""
        # Add dev environment to config
        test_config.environments["dev"] = test_config.environments["test"]

        # Test dev environment
        dev_stack = N8nBaseStack(
            mock_app,
            "dev-stack",
            config=test_config,
            environment="dev",  # Using dev environment for DESTROY policy
        )
        assert dev_stack.removal_policy == RemovalPolicy.DESTROY

        # Test production environment
        test_config.environments["production"] = test_config.environments["test"]
        prod_stack = N8nBaseStack(mock_app, "prod-stack", config=test_config, environment="production")
        assert prod_stack.removal_policy == RemovalPolicy.RETAIN

    def test_is_production_is_development(self, mock_app, test_config):
        """Test environment detection methods."""
        # Add production environment
        test_config.environments["production"] = test_config.environments["test"]
        test_config.environments["dev"] = test_config.environments["test"]

        prod_stack = N8nBaseStack(mock_app, "prod-stack", config=test_config, environment="production")
        assert prod_stack.is_production() is True
        assert prod_stack.is_development() is False

        dev_stack = N8nBaseStack(mock_app, "dev-stack", config=test_config, environment="dev")
        assert dev_stack.is_production() is False
        assert dev_stack.is_development() is True

    def test_spot_enabled(self, mock_app, test_config):
        """Test spot instance detection."""
        stack = N8nBaseStack(mock_app, "test-stack", config=test_config, environment="test")

        # Test config has spot_percentage = 80
        assert stack.is_spot_enabled is True

        # Test with spot disabled
        test_config.environments["test"].settings.fargate.spot_percentage = 0
        stack2 = N8nBaseStack(mock_app, "test-stack-2", config=test_config, environment="test")
        assert stack2.is_spot_enabled is False

    def test_get_shared_resource(self, mock_app, test_config):
        """Test shared resource retrieval."""
        # Add shared resources to config
        from n8n_deploy.config.models import SharedResources

        test_config.shared_resources = SharedResources(
            security={
                "kms_key_arn": "arn:aws:kms:us-east-1:123:key/test",
                "certificate_arn": "arn:aws:acm:us-east-1:123:cert/test",
            },
            networking={"vpc_id": "vpc-shared123"},
        )

        stack = N8nBaseStack(mock_app, "test-stack", config=test_config, environment="test")

        assert stack.get_shared_resource("security", "kms_key_arn") == "arn:aws:kms:us-east-1:123:key/test"
        assert stack.get_shared_resource("networking", "vpc_id") == "vpc-shared123"
        assert stack.get_shared_resource("storage", "bucket") is None
        assert stack.get_shared_resource("invalid", "test") is None

    def test_output_export_logic(self, mock_app, test_config):
        """Test output export determination."""
        stack = N8nBaseStack(mock_app, "test-stack", config=test_config, environment="test")

        # Test exportable outputs
        assert stack.should_export_output("VpcId") is True
        assert stack.should_export_output("SubnetIds") is True
        assert stack.should_export_output("SecurityGroupId") is True
        assert stack.should_export_output("ServiceArn") is True

        # Test non-exportable outputs
        assert stack.should_export_output("RandomMetric") is False
        assert stack.should_export_output("InternalValue") is False

    def test_component_enabled(self, mock_app, test_config):
        """Test component enablement check."""
        # Add components to features
        test_config.environments["test"].settings.features = {"components": ["fargate", "efs", "monitoring"]}

        stack = N8nBaseStack(mock_app, "test-stack", config=test_config, environment="test")

        assert stack.get_component_enabled("fargate") is True
        assert stack.get_component_enabled("efs") is True
        assert stack.get_component_enabled("monitoring") is True
        assert stack.get_component_enabled("database") is False
        assert stack.get_component_enabled("waf") is False
