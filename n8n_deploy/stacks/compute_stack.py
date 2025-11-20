"""Compute stack for ECS cluster and Fargate services."""

from typing import Optional

from aws_cdk import Duration
from aws_cdk import aws_applicationautoscaling as autoscaling
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from ..config.models import AccessType, N8nConfig
from ..constructs.cloudflare_tunnel import (
    CloudflareTunnelConfiguration,
    CloudflareTunnelSidecar,
)
from ..constructs.fargate_n8n import N8nFargateService
from .base_stack import N8nBaseStack
from .network_stack import NetworkStack
from .storage_stack import StorageStack


class ComputeStack(N8nBaseStack):
    """Stack for compute resources (ECS cluster, Fargate service)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: N8nConfig,
        environment: str,
        network_stack: NetworkStack,
        storage_stack: StorageStack,
        database_endpoint: Optional[str] = None,
        database_secret: Optional[secretsmanager.ISecret] = None,
        **kwargs,
    ) -> None:
        """Initialize compute stack.

        Args:
            scope: CDK scope
            construct_id: Stack ID
            config: N8n configuration
            environment: Environment name
            network_stack: Network stack with VPC and security groups
            storage_stack: Storage stack with EFS
            database_endpoint: Optional RDS endpoint
            database_secret: Optional database credentials secret
            **kwargs: Additional stack properties
        """
        super().__init__(scope, construct_id, config, environment, **kwargs)

        self.network_stack = network_stack
        self.storage_stack = storage_stack

        # Add explicit dependencies
        self.add_dependency(network_stack)
        self.add_dependency(storage_stack)

        # Create ECS cluster
        self.cluster = self._create_ecs_cluster()

        # Create n8n Fargate service
        self.n8n_service = N8nFargateService(
            self,
            "N8nService",
            cluster=self.cluster,
            vpc=network_stack.vpc,
            subnets=network_stack.subnets,
            security_group=network_stack.n8n_security_group,
            file_system=storage_stack.file_system,
            access_point=storage_stack.n8n_access_point,
            env_config=self.env_config,
            environment=environment,
            database_endpoint=database_endpoint,
            database_secret=database_secret,
        )

        # Add Cloudflare Tunnel if configured
        if (
            self.env_config.settings.access
            and self.env_config.settings.access.type == AccessType.CLOUDFLARE
            and self.env_config.settings.access.cloudflare
        ):
            self._setup_cloudflare_tunnel()

        # Set up auto-scaling if enabled
        if (
            self.env_config.settings.scaling
            and self.env_config.settings.scaling.max_tasks > self.env_config.settings.scaling.min_tasks
        ):
            self._setup_auto_scaling()

        # Add resilience mechanisms if enabled
        if self.env_config.settings.features and self.env_config.settings.features.get("resilience_enabled", False):
            self._add_resilience_mechanisms()

        # Add outputs
        self._add_outputs()

    def _create_ecs_cluster(self) -> ecs.Cluster:
        """Create ECS cluster."""
        cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=self.get_resource_name("ecs-cluster"),
            vpc=self.network_stack.vpc,
            container_insights=self._should_enable_container_insights(),
        )

        # Add capacity providers if not already added
        if self.is_spot_enabled:
            # The cluster automatically has FARGATE and FARGATE_SPOT capacity providers
            # We just need to set the default strategy if needed
            pass

        return cluster

    def _should_enable_container_insights(self) -> bool:
        """Determine if Container Insights should be enabled."""
        if self.env_config.settings.monitoring:
            return self.env_config.settings.monitoring.enable_container_insights
        return self.is_production()

    def _setup_auto_scaling(self) -> None:
        """Set up auto-scaling for the Fargate service."""
        scaling_config = self.env_config.settings.scaling

        # Create scalable target
        scalable_target = self.n8n_service.service.auto_scale_task_count(
            min_capacity=scaling_config.min_tasks,
            max_capacity=scaling_config.max_tasks,
        )

        # CPU-based scaling
        scalable_target.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=scaling_config.target_cpu_utilization,
            scale_in_cooldown=Duration.seconds(scaling_config.scale_in_cooldown),
            scale_out_cooldown=Duration.seconds(scaling_config.scale_out_cooldown),
        )

        # Memory-based scaling (optional)
        if self.is_production():
            scalable_target.scale_on_metric(
                "MemoryScaling",
                metric=cloudwatch.Metric(
                    namespace="AWS/ECS",
                    metric_name="MemoryUtilization",
                    dimensions_map={
                        "ServiceName": self.n8n_service.service.service_name,
                        "ClusterName": self.cluster.cluster_name,
                    },
                ),
                adjustment_type=autoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
                scaling_steps=[
                    autoscaling.ScalingInterval(
                        lower=80,
                        change=1,
                    ),
                    autoscaling.ScalingInterval(
                        lower=90,
                        change=2,
                    ),
                ],
                cooldown=Duration.seconds(300),
            )

    def _add_resilience_mechanisms(self) -> None:
        """Add resilience mechanisms using ResilientN8n construct."""
        from aws_cdk import aws_sns as sns

        from ..constructs.resilient_n8n import ResilientN8n

        # Create or get monitoring SNS topic
        monitoring_topic = sns.Topic(
            self,
            "ResilienceAlertTopic",
            topic_name=self.get_resource_name("resilience-alerts"),
            display_name=f"n8n {self.environment_name} Resilience Alerts",
        )

        # Add resilience construct
        self.resilient_n8n = ResilientN8n(
            self,
            "ResilientN8n",
            compute_stack=self,
            monitoring_topic=monitoring_topic,
            environment=self.environment_name,
        )

        # Pass DLQ URLs to n8n container as environment variables
        self.n8n_service.task_definition.default_container.add_environment(
            "WEBHOOK_DLQ_URL", self.resilient_n8n.webhook_dlq.queue_url
        )
        self.n8n_service.task_definition.default_container.add_environment(
            "WORKFLOW_DLQ_URL", self.resilient_n8n.workflow_dlq.queue_url
        )
        self.n8n_service.task_definition.default_container.add_environment(
            "CIRCUIT_BREAKER_FUNCTION",
            self.resilient_n8n.get_circuit_breaker_function_name(),
        )

    def _setup_cloudflare_tunnel(self) -> None:
        """Set up Cloudflare Tunnel for zero-trust access."""
        cf_config = self.env_config.settings.access.cloudflare

        # Create Cloudflare tunnel configuration
        tunnel_name = cf_config.tunnel_name or f"n8n-{self.environment_name}"
        tunnel_domain = cf_config.tunnel_domain or f"n8n-{self.environment_name}.example.com"

        # Create tunnel configuration
        self.cloudflare_config = CloudflareTunnelConfiguration(
            self,
            "CloudflareConfig",
            tunnel_name=tunnel_name,
            tunnel_domain=tunnel_domain,
            service_url="http://localhost:5678",
            environment=self.environment_name,
            tunnel_secret_name=cf_config.tunnel_token_secret_name,
            access_config=(
                {
                    "enabled": cf_config.access_enabled,
                    "allowed_emails": cf_config.access_allowed_emails,
                    "allowed_domains": cf_config.access_allowed_domains,
                }
                if cf_config.access_enabled
                else None
            ),
        )

        # Add Cloudflare tunnel sidecar to the task definition
        self.cloudflare_sidecar = CloudflareTunnelSidecar(
            self,
            "CloudflareSidecar",
            task_definition=self.n8n_service.task_definition,
            tunnel_secret=self.cloudflare_config.tunnel_secret,
            tunnel_config={
                "tunnel_name": tunnel_name,
                "tunnel_domain": tunnel_domain,
            },
            log_group=self.n8n_service.log_group,
            environment=self.environment_name,
        )

        # With Cloudflare Tunnel, no inbound rules are needed
        # The tunnel establishes an outbound-only connection
        # Security group already allows all outbound traffic by default

    def _add_outputs(self) -> None:
        """Add stack outputs."""
        # Cluster outputs
        self.add_output(
            "ClusterName",
            value=self.cluster.cluster_name,
            description="ECS cluster name",
        )

        self.add_output("ClusterArn", value=self.cluster.cluster_arn, description="ECS cluster ARN")

        # Service outputs
        self.add_output(
            "ServiceName",
            value=self.n8n_service.service.service_name,
            description="n8n service name",
        )

        self.add_output(
            "ServiceArn",
            value=self.n8n_service.service.service_arn,
            description="n8n service ARN",
        )

        # Task definition
        self.add_output(
            "TaskDefinitionArn",
            value=self.n8n_service.task_definition.task_definition_arn,
            description="Task definition ARN",
        )

        # CloudMap service (for internal DNS)
        if self.n8n_service.service.cloud_map_service:
            self.add_output(
                "ServiceDiscoveryName",
                value=self.n8n_service.service.cloud_map_service.service_name,
                description="Service discovery name",
            )

        # Log group
        self.add_output(
            "LogGroupName",
            value=self.n8n_service.log_group.log_group_name,
            description="CloudWatch log group name",
        )

        # Cloudflare outputs if enabled
        if (
            self.env_config.settings.access
            and self.env_config.settings.access.type == AccessType.CLOUDFLARE
            and hasattr(self, "cloudflare_config")
        ):
            self.add_output(
                "CloudflareTunnelName",
                value=self.cloudflare_config.tunnel_name,
                description="Cloudflare tunnel name",
            )

            self.add_output(
                "CloudflareTunnelDomain",
                value=self.cloudflare_config.tunnel_domain,
                description="Cloudflare tunnel domain",
            )

            self.add_output(
                "CloudflareTunnelSecretArn",
                value=self.cloudflare_config.tunnel_secret.secret_arn,
                description="Cloudflare tunnel token secret ARN",
            )

    @property
    def service(self) -> ecs.FargateService:
        """Get the n8n Fargate service."""
        return self.n8n_service.service

    @property
    def service_security_group(self) -> ec2.SecurityGroup:
        """Get the service security group."""
        return self.network_stack.n8n_security_group
