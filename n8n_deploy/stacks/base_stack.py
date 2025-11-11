"""Base stack with common patterns for all n8n stacks."""

from typing import Dict, Optional

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from constructs import Construct

from ..config.models import N8nConfig


class N8nBaseStack(Stack):
    """Base stack class with common functionality for all n8n stacks."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: N8nConfig,
        environment: str,
        **kwargs,
    ) -> None:
        """Initialize base stack.

        Args:
            scope: CDK scope
            construct_id: Stack ID
            config: N8n configuration
            environment: Environment name (dev, staging, production)
            **kwargs: Additional stack properties
        """
        # Get environment config
        self.config = config
        self._environment = environment
        self.env_config = config.get_environment(environment)

        if not self.env_config:
            raise ValueError(f"Environment '{environment}' not found in configuration")

        # Merge with defaults
        self.env_config = config.merge_with_defaults(self.env_config)

        # Set stack properties
        stack_props = {
            "description": f"n8n Serverless - {construct_id} - {environment}",
            "termination_protection": environment == "production",
        }

        # Merge with provided kwargs
        stack_props.update(kwargs)

        super().__init__(scope, construct_id, **stack_props)

        # Apply tags
        self._apply_tags()

        # Set removal policy based on environment
        self.removal_policy = RemovalPolicy.DESTROY if environment == "dev" else RemovalPolicy.RETAIN

    def _apply_tags(self) -> None:
        """Apply tags to all resources in the stack."""
        # Global tags
        if self.config.global_config.tags:
            for key, value in self.config.global_config.tags.items():
                # Replace template variables
                tag_value = value.replace("{{ environment }}", self.environment_name)
                Tags.of(self).add(key, tag_value)

        # Environment-specific tags
        if self.env_config and self.env_config.tags:
            for key, value in self.env_config.tags.items():
                Tags.of(self).add(key, value)

        # Standard tags
        Tags.of(self).add("Environment", self.environment_name)
        Tags.of(self).add("Stack", self.stack_name)
        Tags.of(self).add("ProjectName", self.config.global_config.project_name)
        Tags.of(self).add("Organization", self.config.global_config.organization)

    def get_resource_name(self, resource_type: str, name: str = "") -> str:
        """Generate consistent resource names.

        Args:
            resource_type: Type of resource (e.g., 'vpc', 'ecs-cluster')
            name: Optional specific name

        Returns:
            Formatted resource name
        """
        parts = [
            self.config.global_config.project_name,
            self.environment_name,
            resource_type,
        ]

        if name:
            parts.append(name)

        return "-".join(parts)

    def add_output(
        self,
        name: str,
        value: str,
        description: Optional[str] = None,
        export_name: Optional[str] = None,
    ) -> CfnOutput:
        """Add a CloudFormation output with consistent naming.

        Args:
            name: Output name
            value: Output value
            description: Optional description
            export_name: Optional export name for cross-stack references

        Returns:
            CfnOutput instance
        """
        output_id = f"{self.stack_name}-{name}"

        if export_name is None and self.should_export_output(name):
            export_name = output_id

        return CfnOutput(
            self,
            name,
            value=value,
            description=description or f"{name} for {self.stack_name}",
            export_name=export_name,
        )

    def should_export_output(self, output_name: str) -> bool:
        """Determine if an output should be exported for cross-stack references.

        Args:
            output_name: Name of the output

        Returns:
            True if output should be exported
        """
        # Common outputs that should be exported
        exportable_outputs = [
            "VpcId",
            "SubnetIds",
            "SecurityGroupId",
            "ClusterArn",
            "ServiceArn",
            "LoadBalancerUrl",
            "ApiUrl",
            "DatabaseEndpoint",
            "FileSystemId",
        ]

        return any(export in output_name for export in exportable_outputs)

    def get_shared_resource(self, category: str, name: str) -> Optional[str]:
        """Get a shared resource ARN or ID from configuration.

        Args:
            category: Resource category (security, networking, storage)
            name: Resource name

        Returns:
            Resource ARN/ID if found, None otherwise
        """
        if not self.config.shared_resources:
            return None

        category_resources = getattr(self.config.shared_resources, category, None)
        if category_resources:
            return category_resources.get(name)

        return None

    def is_production(self) -> bool:
        """Check if this is a production environment."""
        return self.environment_name.lower() in ["production", "prod"]

    def is_development(self) -> bool:
        """Check if this is a development environment."""
        return self.environment_name.lower() in ["development", "dev"]

    def get_cost_allocation_tags(self) -> Dict[str, str]:
        """Get cost allocation tags for the environment."""
        tags = {}

        if self.config.global_config.cost_allocation_tags:
            for tag_key in self.config.global_config.cost_allocation_tags:
                # Check if tag exists in global or environment tags
                if self.config.global_config.tags and tag_key in self.config.global_config.tags:
                    tags[tag_key] = self.config.global_config.tags[tag_key]
                elif self.env_config.tags and tag_key in self.env_config.tags:
                    tags[tag_key] = self.env_config.tags[tag_key]

        return tags

    def get_component_enabled(self, component: str) -> bool:
        """Check if a component is enabled for this environment.

        Args:
            component: Component name

        Returns:
            True if component is enabled
        """
        if not self.env_config.settings.features:
            return False

        components = self.env_config.settings.features.get("components", [])
        return component in components

    @property
    def environment_name(self) -> str:
        """Get the environment name."""
        return self._environment

    @property
    def stack_prefix(self) -> str:
        """Get consistent stack prefix for resource naming."""
        return f"{self.config.global_config.project_name}-{self.environment_name}"

    @property
    def is_spot_enabled(self) -> bool:
        """Check if Spot instances should be used."""
        if self.env_config and self.env_config.settings and self.env_config.settings.fargate:
            return self.env_config.settings.fargate.spot_percentage > 0
        return False

    @property
    def account_id(self) -> str:
        """Get the AWS account ID."""
        return self.env_config.account

    @property
    def region(self) -> str:
        """Get the AWS region."""
        return self.env_config.region
