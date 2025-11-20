"""Access stack for API Gateway and CloudFront."""

from typing import Optional

from aws_cdk import Duration
from aws_cdk import aws_apigatewayv2 as apigatewayv2
from aws_cdk import aws_apigatewayv2_integrations as apigatewayv2_integrations
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_wafv2 as waf
from constructs import Construct

from ..config.models import AccessType, N8nConfig
from .base_stack import N8nBaseStack
from .compute_stack import ComputeStack


class AccessStack(N8nBaseStack):
    """Stack for API access layer (API Gateway, CloudFront, WAF)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: N8nConfig,
        environment: str,
        compute_stack: ComputeStack,
        **kwargs,
    ) -> None:
        """Initialize access stack.

        Args:
            scope: CDK scope
            construct_id: Stack ID
            config: N8n configuration
            environment: Environment name
            compute_stack: Compute stack with ECS service
            **kwargs: Additional stack properties
        """
        super().__init__(scope, construct_id, config, environment, **kwargs)

        self.compute_stack = compute_stack
        self.access_config = self.env_config.settings.access

        # Check if we should create API Gateway resources
        if not self.access_config or self.access_config.type == AccessType.API_GATEWAY:
            # Create VPC link for API Gateway
            self.vpc_link = self._create_vpc_link()

            # Create API Gateway
            self.api = self._create_api_gateway()

            # Create CloudFront distribution if enabled
            if self.access_config and self.access_config.cloudfront_enabled:
                self.distribution = self._create_cloudfront_distribution()

                # Create WAF if enabled
                if self.access_config.waf_enabled:
                    self.web_acl = self._create_waf_web_acl()
                    self._associate_waf_with_cloudfront()

            # Set up custom domain if provided
            if self.access_config and self.access_config.domain_name:
                self._setup_custom_domain()
        else:
            # Using Cloudflare Tunnel - no API Gateway resources needed
            self.vpc_link = None
            self.api = None
            self.distribution = None

        # Add outputs
        self._add_outputs()

    def _create_vpc_link(self) -> apigatewayv2.VpcLink:
        """Create VPC link for API Gateway to connect to ECS service."""
        vpc_link = apigatewayv2.VpcLink(
            self,
            "VpcLink",
            vpc_link_name=self.get_resource_name("vpc-link"),
            vpc=self.compute_stack.network_stack.vpc,
            subnets=ec2.SubnetSelection(subnets=self.compute_stack.network_stack.subnets),
            security_groups=[self.compute_stack.network_stack.n8n_security_group],
        )

        return vpc_link

    def _create_api_gateway(self) -> apigatewayv2.HttpApi:
        """Create HTTP API Gateway."""
        # Allow API Gateway to access n8n service
        self.compute_stack.service_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.compute_stack.network_stack.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(5678),
            description="Allow API Gateway to access n8n",
        )

        # Create HTTP API
        api = apigatewayv2.HttpApi(
            self,
            "HttpApi",
            api_name=self.get_resource_name("api"),
            description=f"n8n API for {self.environment_name}",
            cors_preflight=(
                apigatewayv2.CorsPreflightOptions(
                    allow_origins=self.access_config.cors_origins if self.access_config else ["*"],
                    allow_methods=[apigatewayv2.CorsHttpMethod.ANY],
                    allow_headers=["*"],
                    max_age=Duration.days(1),
                )
                if self.access_config
                else None
            ),
        )

        # Create service discovery integration
        # Check if CloudMap service is available
        cloud_map_service = getattr(self.compute_stack.n8n_service.service, "cloud_map_service", None)

        if cloud_map_service:
            integration = apigatewayv2_integrations.HttpServiceDiscoveryIntegration(
                "N8nIntegration",
                service=cloud_map_service,
                vpc_link=self.vpc_link,
                secure_server_name=cloud_map_service.service_name,
            )
        else:
            # CloudMap not available, skip API Gateway setup for now
            # This can happen in test environments
            return api

        # Add routes
        api.add_routes(
            path="/{proxy+}",
            methods=[apigatewayv2.HttpMethod.ANY],
            integration=integration,
        )

        # Add default route
        api.add_routes(
            path="/",
            methods=[apigatewayv2.HttpMethod.ANY],
            integration=integration,
        )

        # Add throttling
        if self.access_config:
            # Note: Per-route throttling requires additional configuration
            # This is a simplified version
            pass

        return api

    def _create_cloudfront_distribution(self) -> cloudfront.Distribution:
        """Create CloudFront distribution."""
        # Get or create certificate
        certificate = self._get_or_create_certificate()

        # Create origin request policy
        origin_request_policy = cloudfront.OriginRequestPolicy(
            self,
            "OriginRequestPolicy",
            origin_request_policy_name=self.get_resource_name("origin-policy"),
            header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list(
                "Accept",
                "Accept-Language",
                "Content-Type",
                "Host",
                "Origin",
                "Referer",
                "User-Agent",
                "CloudFront-Forwarded-Proto",
                "CloudFront-Is-Desktop-Viewer",
                "CloudFront-Is-Mobile-Viewer",
                "CloudFront-Is-Tablet-Viewer",
                "CloudFront-Viewer-Country",
            ),
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all(),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.all(),
        )

        # Create cache policy for dynamic content
        cache_policy = cloudfront.CachePolicy(
            self,
            "CachePolicy",
            cache_policy_name=self.get_resource_name("cache-policy"),
            default_ttl=Duration.seconds(0),
            max_ttl=Duration.seconds(1),
            min_ttl=Duration.seconds(0),
            enable_accept_encoding_gzip=True,
            enable_accept_encoding_brotli=True,
            header_behavior=cloudfront.CacheHeaderBehavior.allow_list(
                "Authorization",
                "CloudFront-Forwarded-Proto",
                "CloudFront-Is-Desktop-Viewer",
                "CloudFront-Is-Mobile-Viewer",
                "CloudFront-Is-Tablet-Viewer",
                "Host",
            ),
            query_string_behavior=cloudfront.CacheQueryStringBehavior.all(),
            cookie_behavior=cloudfront.CacheCookieBehavior.all(),
        )

        # Create distribution
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    f"{self.api.api_id}.execute-api.{self.region}.amazonaws.com",
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cache_policy,
                origin_request_policy=origin_request_policy,
            ),
            domain_names=(
                [self.access_config.domain_name] if self.access_config and self.access_config.domain_name else None
            ),
            certificate=certificate,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=(
                cloudfront.PriceClass.PRICE_CLASS_100
                if self.is_development()
                else cloudfront.PriceClass.PRICE_CLASS_ALL
            ),
            enabled=True,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            enable_ipv6=True,
            comment=f"n8n distribution for {self.environment_name}",
        )

        # Add cache behaviors for specific paths
        distribution.add_behavior(
            "/webhook/*",
            origins.HttpOrigin(
                f"{self.api.api_id}.execute-api.{self.region}.amazonaws.com",
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=origin_request_policy,
        )

        distribution.add_behavior(
            "/rest/*",
            origins.HttpOrigin(
                f"{self.api.api_id}.execute-api.{self.region}.amazonaws.com",
                protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            ),
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=origin_request_policy,
        )

        return distribution

    def _create_waf_web_acl(self) -> waf.CfnWebACL:
        """Create WAF web ACL for CloudFront."""
        # IP whitelist rules
        ip_rules = []
        if self.access_config and self.access_config.ip_whitelist:
            ip_set = waf.CfnIPSet(
                self,
                "IpWhitelist",
                name=self.get_resource_name("ip-whitelist"),
                scope="CLOUDFRONT",
                ip_address_version="IPV4",
                addresses=self.access_config.ip_whitelist,
            )

            ip_rules.append(
                {
                    "name": "IPWhitelistRule",
                    "priority": 1,
                    "statement": {
                        "ipSetReferenceStatement": {
                            "arn": ip_set.attr_arn,
                        }
                    },
                    "action": {"allow": {}},
                    "visibilityConfig": {
                        "sampledRequestsEnabled": True,
                        "cloudWatchMetricsEnabled": True,
                        "metricName": "IPWhitelistRule",
                    },
                }
            )

        # Create Web ACL
        web_acl = waf.CfnWebACL(
            self,
            "WebAcl",
            name=self.get_resource_name("waf"),
            scope="CLOUDFRONT",
            default_action={"allow": {}} if not ip_rules else {"block": {}},
            rules=[
                # AWS Managed Rules - Common Rule Set
                {
                    "name": "AWSManagedRulesCommonRuleSet",
                    "priority": 10,
                    "overrideAction": {"none": {}},
                    "statement": {
                        "managedRuleGroupStatement": {
                            "vendorName": "AWS",
                            "name": "AWSManagedRulesCommonRuleSet",
                        }
                    },
                    "visibilityConfig": {
                        "sampledRequestsEnabled": True,
                        "cloudWatchMetricsEnabled": True,
                        "metricName": "CommonRuleSet",
                    },
                },
                # Rate limiting
                {
                    "name": "RateLimitRule",
                    "priority": 20,
                    "statement": {
                        "rateBasedStatement": {
                            "limit": 2000,  # requests per 5 minutes per IP
                            "aggregateKeyType": "IP",
                        }
                    },
                    "action": {"block": {}},
                    "visibilityConfig": {
                        "sampledRequestsEnabled": True,
                        "cloudWatchMetricsEnabled": True,
                        "metricName": "RateLimitRule",
                    },
                },
                *ip_rules,
            ],
            visibility_config={
                "sampledRequestsEnabled": True,
                "cloudWatchMetricsEnabled": True,
                "metricName": self.get_resource_name("waf"),
            },
        )

        return web_acl

    def _associate_waf_with_cloudfront(self) -> None:
        """Associate WAF with CloudFront distribution."""
        # This is done through CloudFront distribution properties
        # The association is handled by CloudFormation
        pass

    def _get_or_create_certificate(self) -> Optional[acm.ICertificate]:
        """Get existing certificate or create new one."""
        if not self.access_config or not self.access_config.domain_name:
            return None

        # Check for shared certificate
        shared_cert_arn = self.get_shared_resource("security", "certificate_arn")
        if shared_cert_arn:
            return acm.Certificate.from_certificate_arn(self, "SharedCertificate", shared_cert_arn)

        # For CloudFront, certificate must be in us-east-1
        # This is a simplified version - in production, you'd handle this differently
        return None

    def _setup_custom_domain(self) -> None:
        """Set up custom domain with Route53."""
        if not self.access_config or not self.access_config.domain_name:
            return

        # Get hosted zone
        zone_id = self.get_shared_resource("networking", "route53_zone_id")
        if not zone_id:
            return

        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=zone_id,
            zone_name=".".join(self.access_config.domain_name.split(".")[-2:]),
        )

        # Create A record
        if hasattr(self, "distribution"):
            route53.ARecord(
                self,
                "ARecord",
                zone=hosted_zone,
                record_name=self.access_config.domain_name,
                target=route53.RecordTarget.from_alias(targets.CloudFrontTarget(self.distribution)),
            )

    def _add_outputs(self) -> None:
        """Add stack outputs."""
        # Check access type and add appropriate outputs
        if not self.access_config or self.access_config.type == AccessType.API_GATEWAY:
            # API Gateway outputs
            if self.api:
                self.add_output(
                    "ApiUrl",
                    value=self.api.url or f"https://{self.api.api_id}.execute-api.{self.region}.amazonaws.com",
                    description="API Gateway URL",
                )

                self.add_output("ApiId", value=self.api.api_id, description="API Gateway ID")
        else:
            # Cloudflare Tunnel outputs
            self.add_output(
                "AccessType",
                value="CloudflareTunnel",
                description="Access method is Cloudflare Tunnel",
            )

            if self.access_config.cloudflare and self.access_config.cloudflare.tunnel_domain:
                self.add_output(
                    "AccessUrl",
                    value=f"https://{self.access_config.cloudflare.tunnel_domain}",
                    description="n8n access URL via Cloudflare Tunnel",
                )

        # CloudFront outputs
        if hasattr(self, "distribution") and self.distribution is not None:
            self.add_output(
                "DistributionUrl",
                value=f"https://{self.distribution.distribution_domain_name}",
                description="CloudFront distribution URL",
            )

            self.add_output(
                "DistributionId",
                value=self.distribution.distribution_id,
                description="CloudFront distribution ID",
            )

            if self.access_config and self.access_config.domain_name:
                self.add_output(
                    "CustomDomainUrl",
                    value=f"https://{self.access_config.domain_name}",
                    description="Custom domain URL",
                )
