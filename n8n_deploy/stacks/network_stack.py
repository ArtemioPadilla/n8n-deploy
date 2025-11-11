"""Network stack for VPC and related resources."""

from typing import List

from aws_cdk import Fn
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from ..config.models import N8nConfig, NetworkingConfig
from .base_stack import N8nBaseStack


class NetworkStack(N8nBaseStack):
    """Stack for network resources (VPC, subnets, security groups)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: N8nConfig,
        environment: str,
        **kwargs,
    ) -> None:
        """Initialize network stack.

        Args:
            scope: CDK scope
            construct_id: Stack ID
            config: N8n configuration
            environment: Environment name
            **kwargs: Additional stack properties
        """
        super().__init__(scope, construct_id, config, environment, **kwargs)

        self.network_config = self.env_config.settings.networking or NetworkingConfig()

        # Create or import VPC
        if self.network_config.use_existing_vpc:
            self.vpc = self._import_vpc()
            self.subnets = self._import_subnets()
        else:
            self.vpc = self._create_vpc()
            self.subnets = self._get_created_subnets()

        # Create security groups
        self.n8n_security_group = self._create_n8n_security_group()
        self.efs_security_group = self._create_efs_security_group()

        # Add outputs
        self._add_outputs()

    def _import_vpc(self) -> ec2.IVpc:
        """Import existing VPC from configuration."""
        if not self.network_config.vpc_id:
            raise ValueError("vpc_id is required when use_existing_vpc is True")

        # Import VPC by ID
        vpc = ec2.Vpc.from_lookup(self, "ImportedVpc", vpc_id=self.network_config.vpc_id)

        return vpc

    def _import_subnets(self) -> List[ec2.ISubnet]:
        """Import existing subnets from configuration."""
        if not self.network_config.subnet_ids:
            # If no subnet IDs provided, use default VPC subnets
            return self.vpc.public_subnets if self.vpc.public_subnets else self.vpc.private_subnets

        # Import specific subnets
        subnets = []
        for idx, subnet_id in enumerate(self.network_config.subnet_ids):
            subnet = ec2.Subnet.from_subnet_id(self, f"ImportedSubnet{idx}", subnet_id)
            subnets.append(subnet)

        return subnets

    def _create_vpc(self) -> ec2.Vpc:
        """Create new VPC with configuration."""
        vpc_name = self.get_resource_name("vpc")

        # Determine subnet configuration based on NAT gateway settings
        if self.network_config.nat_gateways > 0:
            # Create public and private subnets
            subnet_configuration = [
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ]
        else:
            # Only public subnets for cost optimization
            subnet_configuration = [
                ec2.SubnetConfiguration(name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)
            ]

        # Create VPC
        vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=vpc_name,
            ip_addresses=ec2.IpAddresses.cidr(self.network_config.vpc_cidr),
            max_azs=self._get_max_azs(),
            nat_gateways=self.network_config.nat_gateways,
            subnet_configuration=subnet_configuration,
            enable_dns_hostnames=True,
            enable_dns_support=True,
        )

        # Add VPC flow logs for production
        if self.is_production():
            vpc.add_flow_log(
                "FlowLog",
                destination=ec2.FlowLogDestination.to_cloud_watch_logs(),
                traffic_type=ec2.FlowLogTrafficType.REJECT,
            )

        return vpc

    def _get_max_azs(self) -> int:
        """Get number of availability zones to use."""
        if self.network_config.availability_zones:
            return len(self.network_config.availability_zones)

        # Default based on environment
        if self.is_production():
            return 3
        elif self.environment_name == "staging":
            return 2
        else:
            return 1

    def _get_created_subnets(self) -> List[ec2.ISubnet]:
        """Get subnets from created VPC."""
        # Prefer private subnets if available (when NAT gateways are used)
        if self.vpc.private_subnets:
            return self.vpc.private_subnets
        else:
            return self.vpc.public_subnets

    def _create_n8n_security_group(self) -> ec2.SecurityGroup:
        """Create security group for n8n Fargate tasks."""
        sg = ec2.SecurityGroup(
            self,
            "N8nSecurityGroup",
            vpc=self.vpc,
            security_group_name=self.get_resource_name("sg", "n8n"),
            description="Security group for n8n Fargate tasks",
            allow_all_outbound=True,
        )

        # Allow inbound traffic from API Gateway (will be added by access stack)
        # For now, we'll add a self-reference for container-to-container communication
        sg.add_ingress_rule(
            peer=sg,
            connection=ec2.Port.all_tcp(),
            description="Allow communication between n8n containers",
        )

        return sg

    def _create_efs_security_group(self) -> ec2.SecurityGroup:
        """Create security group for EFS mount targets."""
        sg = ec2.SecurityGroup(
            self,
            "EfsSecurityGroup",
            vpc=self.vpc,
            security_group_name=self.get_resource_name("sg", "efs"),
            description="Security group for EFS mount targets",
            allow_all_outbound=False,  # EFS doesn't need outbound
        )

        # Allow NFS traffic from n8n security group
        sg.add_ingress_rule(
            peer=self.n8n_security_group,
            connection=ec2.Port.tcp(2049),
            description="Allow NFS traffic from n8n containers",
        )

        return sg

    def _add_outputs(self) -> None:
        """Add stack outputs."""
        # VPC outputs
        self.add_output("VpcId", value=self.vpc.vpc_id, description="VPC ID for n8n deployment")

        # Subnet outputs
        subnet_ids = [subnet.subnet_id for subnet in self.subnets]
        self.add_output(
            "SubnetIds",
            value=Fn.join(",", subnet_ids),
            description="Subnet IDs for n8n deployment",
        )

        # Security group outputs
        self.add_output(
            "N8nSecurityGroupId",
            value=self.n8n_security_group.security_group_id,
            description="Security group ID for n8n tasks",
        )

        self.add_output(
            "EfsSecurityGroupId",
            value=self.efs_security_group.security_group_id,
            description="Security group ID for EFS",
        )

        # Availability zones (only for created VPCs, not imported ones)
        if not self.network_config.use_existing_vpc:
            azs = [subnet.availability_zone for subnet in self.subnets]
            self.add_output(
                "AvailabilityZones",
                value=Fn.join(",", list(set(azs))),
                description="Availability zones used",
            )

    @staticmethod
    def import_from_outputs(
        scope: Construct,
        construct_id: str,
        vpc_id: str,
        subnet_ids: List[str],
        n8n_sg_id: str,
        efs_sg_id: str,
    ) -> "NetworkStack":
        """Import network resources from another stack's outputs.

        Args:
            scope: CDK scope
            construct_id: Construct ID
            vpc_id: VPC ID to import
            subnet_ids: List of subnet IDs
            n8n_sg_id: N8n security group ID
            efs_sg_id: EFS security group ID

        Returns:
            NetworkStack instance with imported resources
        """
        # Create a dummy stack instance
        from aws_cdk import Stack

        stack = NetworkStack.__new__(NetworkStack)
        Stack.__init__(stack, scope, construct_id)

        # Import resources
        stack.vpc = ec2.Vpc.from_lookup(stack, "ImportedVpc", vpc_id=vpc_id)

        stack.subnets = []
        for idx, subnet_id in enumerate(subnet_ids):
            subnet = ec2.Subnet.from_subnet_id(stack, f"ImportedSubnet{idx}", subnet_id)
            stack.subnets.append(subnet)

        stack.n8n_security_group = ec2.SecurityGroup.from_security_group_id(stack, "ImportedN8nSg", n8n_sg_id)

        stack.efs_security_group = ec2.SecurityGroup.from_security_group_id(stack, "ImportedEfsSg", efs_sg_id)

        return stack
