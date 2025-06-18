#!/usr/bin/env python3
"""
n8n Deploy CDK Application

This application deploys n8n workflow automation tool using multiple deployment
targets (AWS Serverless, Docker, Cloudflare Tunnel), configured via system.yaml file.

Usage:
    cdk deploy -c environment=dev
    cdk deploy -c environment=staging
    cdk deploy -c environment=production
    cdk deploy -c environment=dev -c stack_type=minimal
"""
import os
import sys
from typing import Optional

import aws_cdk as cdk

from n8n_deploy.config import ConfigLoader
from n8n_deploy.config.models import DatabaseType
from n8n_deploy.stacks import AccessStack, ComputeStack, NetworkStack, StorageStack


def create_stacks(app: cdk.App, environment: str, stack_type: Optional[str] = None) -> None:
    """Create all stacks for the specified environment.

    Args:
        app: CDK application
        environment: Environment name from system.yaml
        stack_type: Optional stack type (minimal, standard, enterprise)
    """
    # Load configuration
    try:
        # Check if a custom config path is provided
        config_path = app.node.try_get_context("config_path")
        if config_path:
            config_loader = ConfigLoader(config_path)
        else:
            config_loader = ConfigLoader()
        config = config_loader.load_config(environment, stack_type)
    except FileNotFoundError:
        print("Error: system.yaml not found. Please create a system.yaml file.")
        print("You can use 'python -m n8n_deploy.config.config_loader' to generate an example.")
        sys.exit(1)
    except ValueError as e:
        print(f"Error loading configuration: {e}")
        sys.exit(1)

    env_config = config.get_environment(environment)
    if not env_config:
        print(f"Error: Environment '{environment}' not found in configuration")
        sys.exit(1)

    # Create stack name prefix
    stack_prefix = f"{config.global_config.project_name}-{environment}"

    # Determine which components to create
    components = []
    if env_config.settings.features and "components" in env_config.settings.features:
        components = env_config.settings.features["components"]
    else:
        # Default components based on configuration
        components = ["network", "storage", "compute", "access"]
        if env_config.settings.database and env_config.settings.database.type == DatabaseType.POSTGRES:
            components.append("database")
        if env_config.settings.monitoring:
            components.append("monitoring")

    # Create CDK environment
    # For synthesis with dummy accounts, use CDK's default env
    if env_config.account in ["123456789012", "111111111111", "000000000000"]:
        # Use CDK's default synthesis environment
        cdk_env = cdk.Environment(
            account=os.environ.get("CDK_DEFAULT_ACCOUNT", env_config.account),
            region=os.environ.get("CDK_DEFAULT_REGION", env_config.region),
        )
    else:
        cdk_env = cdk.Environment(account=env_config.account, region=env_config.region)

    # Create network stack
    network_stack = None
    if "network" in components or env_config.settings.networking:
        network_stack = NetworkStack(
            app,
            f"{stack_prefix}-network",
            config=config,
            environment=environment,
            stack_type=stack_type,
            env=cdk_env,
        )

    # Create storage stack
    storage_stack = None
    if "storage" in components or "efs" in components:
        if not network_stack:
            raise ValueError("Storage stack requires network stack")
        storage_stack = StorageStack(
            app,
            f"{stack_prefix}-storage",
            config=config,
            environment=environment,
            stack_type=stack_type,
            network_stack=network_stack,
            env=cdk_env,
        )

    # Create database stack if needed
    database_stack = None
    database_endpoint = None
    database_secret = None
    if "database" in components or (
        env_config.settings.database and env_config.settings.database.type == DatabaseType.POSTGRES
    ):
        # Import DatabaseStack when needed
        from n8n_deploy.stacks import DatabaseStack

        if not network_stack:
            raise ValueError("Database stack requires network stack")

        database_stack = DatabaseStack(
            app,
            f"{stack_prefix}-database",
            config=config,
            environment=environment,
            stack_type=stack_type,
            network_stack=network_stack,
            env=cdk_env,
        )
        database_endpoint = database_stack.endpoint
        database_secret = database_stack.secret

    # Create compute stack
    compute_stack = None
    if "compute" in components or "fargate" in components:
        if not network_stack or not storage_stack:
            raise ValueError("Compute stack requires network and storage stacks")

        compute_stack = ComputeStack(
            app,
            f"{stack_prefix}-compute",
            config=config,
            environment=environment,
            stack_type=stack_type,
            network_stack=network_stack,
            storage_stack=storage_stack,
            database_endpoint=database_endpoint,
            database_secret=database_secret,
            env=cdk_env,
        )

    # Create access stack
    if "access" in components or "api_gateway" in components:
        if not compute_stack:
            raise ValueError("Access stack requires compute stack")

        AccessStack(
            app,
            f"{stack_prefix}-access",
            config=config,
            environment=environment,
            stack_type=stack_type,
            compute_stack=compute_stack,
            env=cdk_env,
        )

    # Create monitoring stack if enabled
    if "monitoring" in components:
        # Import MonitoringStack when needed
        from n8n_deploy.stacks import MonitoringStack

        MonitoringStack(
            app,
            f"{stack_prefix}-monitoring",
            config=config,
            environment=environment,
            stack_type=stack_type,
            compute_stack=compute_stack,
            storage_stack=storage_stack,
            database_stack=database_stack,
            env=cdk_env,
        )

    # Tags are applied in the base stack class to avoid synthesis issues
    # when the environment parameter might be interpreted as an AWS environment specification


def main():
    """Main entry point for the CDK application."""
    app = cdk.App()

    # Get environment from context
    environment = app.node.try_get_context("environment")
    if not environment:
        print("Error: Please specify environment using -c environment=<env>")
        print("Available environments are defined in system.yaml")
        print("Example: cdk deploy -c environment=dev")

        # Try to list available environments
        try:
            config_loader = ConfigLoader()
            environments = config_loader.get_available_environments()
            if environments:
                print(f"\nAvailable environments: {', '.join(environments)}")
        except Exception:
            pass

        sys.exit(1)

    # Get optional stack type
    stack_type = app.node.try_get_context("stack_type")

    # Create stacks
    create_stacks(app, environment, stack_type)

    app.synth()


if __name__ == "__main__":
    main()
