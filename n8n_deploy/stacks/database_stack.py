"""Database stack for optional RDS PostgreSQL."""

from aws_cdk import Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from ..config.models import DatabaseConfig, N8nConfig
from .base_stack import N8nBaseStack
from .network_stack import NetworkStack


class DatabaseStack(N8nBaseStack):
    """Stack for RDS PostgreSQL database resources."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: N8nConfig,
        environment: str,
        network_stack: NetworkStack,
        **kwargs,
    ) -> None:
        """Initialize database stack.

        Args:
            scope: CDK scope
            construct_id: Stack ID
            config: N8n configuration
            environment: Environment name
            network_stack: Network stack with VPC and security groups
            **kwargs: Additional stack properties
        """
        super().__init__(scope, construct_id, config, environment, **kwargs)

        self.network_stack = network_stack
        self.db_config = self.env_config.settings.database or DatabaseConfig()

        # Create database security group
        self.db_security_group = self._create_database_security_group()

        # Create or import database
        if self.db_config.use_existing:
            self._import_existing_database()
        else:
            if self.db_config.aurora_serverless:
                self._create_aurora_serverless()
            else:
                self._create_rds_instance()

        # Add outputs
        self._add_outputs()

    def _create_database_security_group(self) -> ec2.SecurityGroup:
        """Create security group for database."""
        sg = ec2.SecurityGroup(
            self,
            "DatabaseSecurityGroup",
            vpc=self.network_stack.vpc,
            security_group_name=self.get_resource_name("sg", "database"),
            description="Security group for n8n database",
            allow_all_outbound=False,  # Databases don't need outbound
        )

        # Allow access from n8n containers
        sg.add_ingress_rule(
            peer=self.network_stack.n8n_security_group,
            connection=ec2.Port.tcp(5432),
            description="Allow PostgreSQL access from n8n containers",
        )

        return sg

    def _import_existing_database(self) -> None:
        """Import existing database from configuration."""
        if not self.db_config.connection_secret_arn:
            raise ValueError("connection_secret_arn required when use_existing is True")

        # Import secret
        self.secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "ImportedDatabaseSecret",
            secret_complete_arn=self.db_config.connection_secret_arn,
        )

        # Extract endpoint from secret (this is a simplified version)
        # In practice, you'd need to handle this more robustly
        self.endpoint = None  # Will be resolved from secret at runtime

    def _create_aurora_serverless(self) -> None:
        """Create Aurora Serverless v2 PostgreSQL cluster."""
        # Create credentials secret
        self.secret = secretsmanager.Secret(
            self,
            "DatabaseSecret",
            secret_name=f"n8n/{self.environment_name}/db-credentials",
            description=f"n8n database credentials for {self.environment_name}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "n8nadmin"}',
                generate_string_key="password",
                exclude_characters=" %+~`#$&*()|[]{}:;<>?!'/@\"\\",
                password_length=30,
            ),
        )

        # Create subnet group
        subnet_group = rds.SubnetGroup(
            self,
            "SubnetGroup",
            description=f"Subnet group for n8n {self.environment_name}",
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=self.network_stack.subnets),
            removal_policy=self.removal_policy,
        )

        # Create Aurora Serverless v2 cluster
        aurora_config = self.db_config.aurora_serverless or {}
        min_capacity = aurora_config.get("min_capacity", 0.5)
        max_capacity = aurora_config.get("max_capacity", 1.0)

        self.cluster = rds.DatabaseCluster(
            self,
            "AuroraCluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(version=rds.AuroraPostgresEngineVersion.VER_15_3),
            credentials=rds.Credentials.from_secret(self.secret),
            default_database_name="n8n",
            cluster_identifier=self.get_resource_name("aurora"),
            serverless_v2_scaling_configuration=rds.ServerlessV2ScalingConfiguration(
                min_capacity=min_capacity,
                max_capacity=max_capacity,
            ),
            vpc=self.network_stack.vpc,
            subnet_group=subnet_group,
            security_groups=[self.db_security_group],
            backup=rds.BackupProps(
                retention=Duration.days(self.db_config.backup_retention_days),
                preferred_window="03:00-04:00",
            ),
            enable_data_api=True,  # Enable Data API for serverless access
            storage_encrypted=True,
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=logs.RetentionDays.ONE_MONTH,
            deletion_protection=self.is_production(),
            removal_policy=self.removal_policy,
        )

        # Add writer instance
        self.cluster.add_instance(
            "WriterInstance",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3,
                ec2.InstanceSize.MEDIUM,
            ),
            enable_performance_insights=self.is_production(),
        )

        self.endpoint = self.cluster.cluster_endpoint.socket_address

    def _create_rds_instance(self) -> None:
        """Create standard RDS PostgreSQL instance."""
        # Create credentials secret
        self.secret = secretsmanager.Secret(
            self,
            "DatabaseSecret",
            secret_name=f"n8n/{self.environment_name}/db-credentials",
            description=f"n8n database credentials for {self.environment_name}",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "n8nadmin"}',
                generate_string_key="password",
                exclude_characters=" %+~`#$&*()|[]{}:;<>?!'/@\"\\",
                password_length=30,
            ),
        )

        # Determine instance class
        instance_class = ec2.InstanceType.of(
            ec2.InstanceClass.T4G,  # Graviton for cost savings
            ec2.InstanceSize.MICRO,
        )
        if self.db_config.instance_class:
            # Parse instance class from string (e.g., "db.t4g.micro")
            parts = self.db_config.instance_class.split(".")
            if len(parts) == 3:
                class_name = parts[1].upper()
                size_name = parts[2].upper()
                instance_class = ec2.InstanceType.of(
                    getattr(ec2.InstanceClass, class_name),
                    getattr(ec2.InstanceSize, size_name),
                )

        # Create RDS instance
        self.instance = rds.DatabaseInstance(
            self,
            "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_15_3),
            instance_type=instance_class,
            credentials=rds.Credentials.from_secret(self.secret),
            database_name="n8n",
            instance_identifier=self.get_resource_name("rds"),
            vpc=self.network_stack.vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=self.network_stack.subnets),
            security_groups=[self.db_security_group],
            allocated_storage=20,  # Minimum for PostgreSQL
            storage_type=rds.StorageType.GP3,
            multi_az=self.db_config.multi_az,
            backup_retention=Duration.days(self.db_config.backup_retention_days),
            preferred_backup_window="03:00-04:00",
            preferred_maintenance_window="sun:04:00-sun:05:00",
            enable_performance_insights=self.is_production(),
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=logs.RetentionDays.ONE_MONTH,
            deletion_protection=self.is_production(),
            removal_policy=self.removal_policy,
            # Cost optimization
            publicly_accessible=False,
            storage_encrypted=True,
            auto_minor_version_upgrade=False,  # Control updates
        )

        self.endpoint = self.instance.db_instance_endpoint_address + ":" + self.instance.db_instance_endpoint_port

    def _add_outputs(self) -> None:
        """Add stack outputs."""
        # Database endpoint
        if hasattr(self, "endpoint") and self.endpoint:
            self.add_output("DatabaseEndpoint", value=self.endpoint, description="Database endpoint")

        # Secret ARN
        if hasattr(self, "secret"):
            self.add_output(
                "DatabaseSecretArn",
                value=self.secret.secret_arn,
                description="Database credentials secret ARN",
            )

        # Security group
        self.add_output(
            "DatabaseSecurityGroupId",
            value=self.db_security_group.security_group_id,
            description="Database security group ID",
        )
