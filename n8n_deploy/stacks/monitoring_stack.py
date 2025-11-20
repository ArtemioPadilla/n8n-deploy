"""Monitoring stack for CloudWatch alarms and dashboards."""

from typing import Optional

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subscriptions
from constructs import Construct

from ..config.models import AccessType, N8nConfig
from .base_stack import N8nBaseStack
from .compute_stack import ComputeStack
from .database_stack import DatabaseStack
from .storage_stack import StorageStack


class MonitoringStack(N8nBaseStack):
    """Stack for monitoring resources (CloudWatch alarms, dashboards)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        config: N8nConfig,
        environment: str,
        compute_stack: ComputeStack,
        storage_stack: Optional[StorageStack] = None,
        database_stack: Optional[DatabaseStack] = None,
        **kwargs,
    ) -> None:
        """Initialize monitoring stack.

        Args:
            scope: CDK scope
            construct_id: Stack ID
            config: N8n configuration
            environment: Environment name
            compute_stack: Compute stack to monitor
            storage_stack: Optional storage stack to monitor
            database_stack: Optional database stack to monitor
            **kwargs: Additional stack properties
        """
        super().__init__(scope, construct_id, config, environment, **kwargs)

        self.compute_stack = compute_stack
        self.storage_stack = storage_stack
        self.database_stack = database_stack
        self.monitoring_config = self.env_config.settings.monitoring

        # Create SNS topic for alarms
        self.alarm_topic = self._create_alarm_topic()

        # Create alarms
        self._create_compute_alarms()
        if storage_stack:
            self._create_storage_alarms()
        if database_stack:
            self._create_database_alarms()

        # Create Cloudflare Tunnel alarms if enabled
        if self.env_config.settings.access and self.env_config.settings.access.type == AccessType.CLOUDFLARE:
            self._create_cloudflare_tunnel_alarms()

        # Create dashboard
        self.dashboard = self._create_dashboard()

        # Create custom n8n metrics
        self._create_custom_n8n_metrics()

        # Add outputs
        self._add_outputs()

    def _create_alarm_topic(self) -> sns.Topic:
        """Create SNS topic for alarm notifications."""
        topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=self.get_resource_name("alarms"),
            display_name=f"n8n {self.environment_name} Alarms",
        )

        # Add email subscription if configured
        if self.monitoring_config and self.monitoring_config.alarm_email:
            topic.add_subscription(sns_subscriptions.EmailSubscription(self.monitoring_config.alarm_email))

        return topic

    def _create_compute_alarms(self) -> None:
        """Create alarms for compute resources."""
        # Create alarm action
        alarm_action = cloudwatch_actions.SnsAction(self.alarm_topic)

        # CPU utilization alarm
        cpu_alarm = cloudwatch.Alarm(
            self,
            "CpuAlarm",
            alarm_name=f"{self.stack_prefix}-cpu-high",
            alarm_description="n8n CPU utilization is too high",
            metric=cloudwatch.Metric(
                namespace="AWS/ECS",
                metric_name="CPUUtilization",
                dimensions_map={
                    "ServiceName": self.compute_stack.n8n_service.service.service_name,
                    "ClusterName": self.compute_stack.cluster.cluster_name,
                },
                statistic="Average",
            ),
            threshold=80,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        cpu_alarm.add_alarm_action(alarm_action)

        # Memory utilization alarm
        memory_alarm = cloudwatch.Alarm(
            self,
            "MemoryAlarm",
            alarm_name=f"{self.stack_prefix}-memory-high",
            alarm_description="n8n memory utilization is too high",
            metric=cloudwatch.Metric(
                namespace="AWS/ECS",
                metric_name="MemoryUtilization",
                dimensions_map={
                    "ServiceName": self.compute_stack.n8n_service.service.service_name,
                    "ClusterName": self.compute_stack.cluster.cluster_name,
                },
                statistic="Average",
            ),
            threshold=85,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        memory_alarm.add_alarm_action(alarm_action)

        # Task count alarm (service health)
        task_count_alarm = cloudwatch.Alarm(
            self,
            "TaskCountAlarm",
            alarm_name=f"{self.stack_prefix}-task-count-low",
            alarm_description="n8n service has insufficient running tasks",
            metric=cloudwatch.Metric(
                namespace="AWS/ECS",
                metric_name="RunningTaskCount",
                dimensions_map={
                    "ServiceName": self.compute_stack.n8n_service.service.service_name,
                    "ClusterName": self.compute_stack.cluster.cluster_name,
                },
                statistic="Average",
            ),
            threshold=1,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        task_count_alarm.add_alarm_action(alarm_action)

    def _create_storage_alarms(self) -> None:
        """Create alarms for storage resources."""
        alarm_action = cloudwatch_actions.SnsAction(self.alarm_topic)

        # EFS burst credit balance alarm
        burst_credit_alarm = cloudwatch.Alarm(
            self,
            "EfsBurstCreditAlarm",
            alarm_name=f"{self.stack_prefix}-efs-burst-credits-low",
            alarm_description="EFS burst credits are running low",
            metric=cloudwatch.Metric(
                namespace="AWS/EFS",
                metric_name="BurstCreditBalance",
                dimensions_map={
                    "FileSystemId": self.storage_stack.file_system.file_system_id,
                },
                statistic="Average",
            ),
            threshold=1000000000000,  # 1 TB in bytes
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        burst_credit_alarm.add_alarm_action(alarm_action)

    def _create_database_alarms(self) -> None:
        """Create alarms for database resources."""
        if not hasattr(self.database_stack, "instance") and not hasattr(self.database_stack, "cluster"):
            return

        alarm_action = cloudwatch_actions.SnsAction(self.alarm_topic)

        # Database CPU alarm
        if hasattr(self.database_stack, "instance"):
            # RDS instance
            db_cpu_alarm = cloudwatch.Alarm(
                self,
                "DatabaseCpuAlarm",
                alarm_name=f"{self.stack_prefix}-db-cpu-high",
                alarm_description="Database CPU utilization is too high",
                metric=self.database_stack.instance.metric_cpu_utilization(),
                threshold=80,
                evaluation_periods=3,
                datapoints_to_alarm=2,
            )
            db_cpu_alarm.add_alarm_action(alarm_action)

            # Database connections alarm
            db_connections_alarm = cloudwatch.Alarm(
                self,
                "DatabaseConnectionsAlarm",
                alarm_name=f"{self.stack_prefix}-db-connections-high",
                alarm_description="Database connections are too high",
                metric=self.database_stack.instance.metric_database_connections(),
                threshold=50,  # Adjust based on instance class
                evaluation_periods=2,
            )
            db_connections_alarm.add_alarm_action(alarm_action)

        elif hasattr(self.database_stack, "cluster"):
            # Aurora cluster
            # Aurora Serverless v2 metrics are different
            pass

    def _create_cloudflare_tunnel_alarms(self) -> None:
        """Create alarms for Cloudflare Tunnel health."""
        alarm_action = cloudwatch_actions.SnsAction(self.alarm_topic)

        # Create custom metric namespace for Cloudflare
        cf_namespace = "Cloudflare/Tunnel"

        # Tunnel health metric filter
        logs.MetricFilter(
            self,
            "CloudflareTunnelHealthMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="TunnelHealthy",
            metric_namespace=cf_namespace,
            metric_value="1",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, container="cloudflare-tunnel", level="info", message="Tunnel status*healthy*"]'
            ),
            default_value=0,
        )

        # Tunnel connection errors metric filter
        logs.MetricFilter(
            self,
            "CloudflareTunnelErrorMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="TunnelConnectionErrors",
            metric_namespace=cf_namespace,
            metric_value="1",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, container="cloudflare-tunnel", level="error", '
                'message="*connection*" || message="*tunnel*"]'
            ),
            default_value=0,
        )

        # Tunnel metrics from container health check
        tunnel_health_alarm = cloudwatch.Alarm(
            self,
            "CloudflareTunnelHealthAlarm",
            alarm_name=f"{self.stack_prefix}-cloudflare-tunnel-unhealthy",
            alarm_description="Cloudflare Tunnel is unhealthy",
            metric=cloudwatch.Metric(
                namespace="AWS/ECS",
                metric_name="ContainerHealthCheck",
                dimensions_map={
                    "ServiceName": self.compute_stack.n8n_service.service.service_name,
                    "ClusterName": self.compute_stack.cluster.cluster_name,
                    "ContainerName": "cloudflare-tunnel",
                },
                statistic="Average",
            ),
            threshold=1,
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        tunnel_health_alarm.add_alarm_action(alarm_action)

        # Connection error rate alarm
        tunnel_error_alarm = cloudwatch.Alarm(
            self,
            "CloudflareTunnelErrorAlarm",
            alarm_name=f"{self.stack_prefix}-cloudflare-tunnel-errors-high",
            alarm_description="High Cloudflare Tunnel error rate",
            metric=cloudwatch.Metric(
                namespace=cf_namespace,
                metric_name="TunnelConnectionErrors",
                statistic="Sum",
            ),
            threshold=10,  # More than 10 errors in evaluation period
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        tunnel_error_alarm.add_alarm_action(alarm_action)

    def _create_dashboard(self) -> cloudwatch.Dashboard:
        """Create CloudWatch dashboard."""
        dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=self.get_resource_name("dashboard"),
            period_override=cloudwatch.PeriodOverride.INHERIT,
            default_interval=Duration.hours(3),
        )

        # Add compute metrics
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="n8n Service Metrics",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/ECS",
                        metric_name="CPUUtilization",
                        dimensions_map={
                            "ServiceName": self.compute_stack.n8n_service.service.service_name,
                            "ClusterName": self.compute_stack.cluster.cluster_name,
                        },
                        statistic="Average",
                        label="CPU %",
                    ),
                ],
                right=[
                    cloudwatch.Metric(
                        namespace="AWS/ECS",
                        metric_name="MemoryUtilization",
                        dimensions_map={
                            "ServiceName": self.compute_stack.n8n_service.service.service_name,
                            "ClusterName": self.compute_stack.cluster.cluster_name,
                        },
                        statistic="Average",
                        label="Memory %",
                    ),
                ],
                width=12,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Task Count",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/ECS",
                        metric_name="RunningTaskCount",
                        dimensions_map={
                            "ServiceName": self.compute_stack.n8n_service.service.service_name,
                            "ClusterName": self.compute_stack.cluster.cluster_name,
                        },
                        statistic="Average",
                        label="Running Tasks",
                    ),
                    cloudwatch.Metric(
                        namespace="AWS/ECS",
                        metric_name="DesiredTaskCount",
                        dimensions_map={
                            "ServiceName": self.compute_stack.n8n_service.service.service_name,
                            "ClusterName": self.compute_stack.cluster.cluster_name,
                        },
                        statistic="Average",
                        label="Desired Tasks",
                    ),
                ],
                width=12,
                height=6,
            ),
        )

        # Add log insights widget
        dashboard.add_widgets(
            cloudwatch.LogQueryWidget(
                title="Recent Errors",
                log_group_names=[self.compute_stack.n8n_service.log_group.log_group_name],
                width=24,
                height=4,
                query_lines=[
                    "fields @timestamp, @message",
                    "filter @message like /ERROR/",
                    "sort @timestamp desc",
                    "limit 50",
                ],
            )
        )

        # Add storage metrics if available
        if self.storage_stack:
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="EFS Metrics",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/EFS",
                            metric_name="ClientConnections",
                            dimensions_map={
                                "FileSystemId": self.storage_stack.file_system.file_system_id,
                            },
                            statistic="Sum",
                            label="Client Connections",
                        ),
                    ],
                    right=[
                        cloudwatch.Metric(
                            namespace="AWS/EFS",
                            metric_name="BurstCreditBalance",
                            dimensions_map={
                                "FileSystemId": self.storage_stack.file_system.file_system_id,
                            },
                            statistic="Average",
                            label="Burst Credits",
                        ),
                    ],
                    width=12,
                    height=6,
                ),
            )

        # Add Cloudflare Tunnel metrics if enabled
        if self.env_config.settings.access and self.env_config.settings.access.type == AccessType.CLOUDFLARE:
            dashboard.add_widgets(
                cloudwatch.GraphWidget(
                    title="Cloudflare Tunnel Health",
                    left=[
                        cloudwatch.Metric(
                            namespace="Cloudflare/Tunnel",
                            metric_name="TunnelHealthy",
                            statistic="Average",
                            label="Tunnel Health Status",
                            color=cloudwatch.Color.GREEN,
                        ),
                    ],
                    right=[
                        cloudwatch.Metric(
                            namespace="Cloudflare/Tunnel",
                            metric_name="TunnelConnectionErrors",
                            statistic="Sum",
                            label="Connection Errors",
                            color=cloudwatch.Color.RED,
                        ),
                    ],
                    width=12,
                    height=6,
                ),
                cloudwatch.LogQueryWidget(
                    title="Cloudflare Tunnel Logs",
                    log_group_names=[self.compute_stack.n8n_service.log_group.log_group_name],
                    width=12,
                    height=6,
                    query_lines=[
                        "fields @timestamp, @message",
                        "filter @logStream like /cloudflare/",
                        "sort @timestamp desc",
                        "limit 20",
                    ],
                ),
            )

        return dashboard

    def _create_custom_n8n_metrics(self) -> None:
        """Create custom metrics for n8n-specific monitoring."""
        # Create custom metric namespace
        custom_namespace = (
            self.monitoring_config.custom_metrics_namespace if self.monitoring_config else "N8n/Serverless"
        )

        # Create custom metrics filters for log insights
        self._create_workflow_execution_metrics(custom_namespace)
        self._create_webhook_metrics(custom_namespace)
        self._create_error_metrics(custom_namespace)
        self._create_performance_metrics(custom_namespace)

        # Add custom metric alarms
        self._create_custom_metric_alarms(custom_namespace)

        # Add custom widgets to dashboard
        self._add_custom_metrics_to_dashboard(custom_namespace)

    def _create_workflow_execution_metrics(self, namespace: str) -> None:
        """Create metrics for workflow executions."""
        # Metric filter for successful workflow executions
        logs.MetricFilter(
            self,
            "WorkflowSuccessMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="WorkflowExecutionSuccess",
            metric_namespace=namespace,
            metric_value="1",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="info", message="Workflow execution finished successfully*"]'
            ),
            default_value=0,
        )

        # Metric filter for failed workflow executions
        logs.MetricFilter(
            self,
            "WorkflowFailureMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="WorkflowExecutionFailure",
            metric_namespace=namespace,
            metric_value="1",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="error", message="Workflow execution failed*"]'
            ),
            default_value=0,
        )

        # Metric filter for workflow execution duration
        logs.MetricFilter(
            self,
            "WorkflowDurationMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="WorkflowExecutionDuration",
            metric_namespace=namespace,
            metric_value="$duration",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="info", message="Workflow execution finished*", duration]'
            ),
            default_value=0,
        )

    def _create_webhook_metrics(self, namespace: str) -> None:
        """Create metrics for webhook handling."""
        # Metric filter for webhook requests
        logs.MetricFilter(
            self,
            "WebhookRequestMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="WebhookRequests",
            metric_namespace=namespace,
            metric_value="1",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="info", message="Webhook received*"]'
            ),
            default_value=0,
        )

        # Metric filter for webhook response time
        logs.MetricFilter(
            self,
            "WebhookResponseTimeMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="WebhookResponseTime",
            metric_namespace=namespace,
            metric_value="$response_time",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="info", message="Webhook processed*", response_time]'
            ),
            default_value=0,
            unit=cloudwatch.Unit.MILLISECONDS,
        )

    def _create_error_metrics(self, namespace: str) -> None:
        """Create metrics for error tracking."""
        # Metric filter for authentication errors
        logs.MetricFilter(
            self,
            "AuthErrorMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="AuthenticationErrors",
            metric_namespace=namespace,
            metric_value="1",
            filter_pattern=logs.FilterPattern.literal("Authentication failed || Unauthorized access"),
            default_value=0,
        )

        # Metric filter for database connection errors
        logs.MetricFilter(
            self,
            "DatabaseErrorMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="DatabaseConnectionErrors",
            metric_namespace=namespace,
            metric_value="1",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="error", message="Database connection failed*"]'
            ),
            default_value=0,
        )

    def _create_performance_metrics(self, namespace: str) -> None:
        """Create performance-related metrics."""
        # Metric filter for node execution time
        logs.MetricFilter(
            self,
            "NodeExecutionTimeMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="NodeExecutionTime",
            metric_namespace=namespace,
            metric_value="$execution_time",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="info", message="Node executed*", node_type, execution_time]'
            ),
            default_value=0,
            unit=cloudwatch.Unit.MILLISECONDS,
        )

        # Metric filter for queue depth
        logs.MetricFilter(
            self,
            "QueueDepthMetric",
            log_group=self.compute_stack.n8n_service.log_group,
            metric_name="WorkflowQueueDepth",
            metric_namespace=namespace,
            metric_value="$queue_size",
            filter_pattern=logs.FilterPattern.literal(
                '[timestamp, request_id, level="info", message="Queue status*", queue_size]'
            ),
            default_value=0,
        )

    def _create_custom_metric_alarms(self, namespace: str) -> None:
        """Create alarms for custom n8n metrics."""
        alarm_action = cloudwatch_actions.SnsAction(self.alarm_topic)

        # Workflow failure rate alarm
        workflow_failure_alarm = cloudwatch.Alarm(
            self,
            "WorkflowFailureRateAlarm",
            alarm_name=f"{self.stack_prefix}-workflow-failure-rate-high",
            alarm_description="High workflow failure rate detected",
            metric=cloudwatch.MathExpression(
                expression="(failures / (successes + failures)) * 100",
                using_metrics={
                    "failures": cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WorkflowExecutionFailure",
                        statistic="Sum",
                        period=Duration.minutes(5),
                    ),
                    "successes": cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WorkflowExecutionSuccess",
                        statistic="Sum",
                        period=Duration.minutes(5),
                    ),
                },
                label="Workflow Failure Rate %",
                period=Duration.minutes(5),
            ),
            threshold=10,  # 10% failure rate
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        workflow_failure_alarm.add_alarm_action(alarm_action)

        # Webhook response time alarm
        webhook_response_alarm = cloudwatch.Alarm(
            self,
            "WebhookResponseTimeAlarm",
            alarm_name=f"{self.stack_prefix}-webhook-response-time-high",
            alarm_description="Webhook response time is too high",
            metric=cloudwatch.Metric(
                namespace=namespace,
                metric_name="WebhookResponseTime",
                statistic="Average",
            ),
            threshold=1000,  # 1 second
            evaluation_periods=3,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        webhook_response_alarm.add_alarm_action(alarm_action)

        # Database error rate alarm
        if self.database_stack:
            db_error_alarm = cloudwatch.Alarm(
                self,
                "DatabaseErrorRateAlarm",
                alarm_name=f"{self.stack_prefix}-database-error-rate-high",
                alarm_description="High database error rate detected",
                metric=cloudwatch.Metric(
                    namespace=namespace,
                    metric_name="DatabaseConnectionErrors",
                    statistic="Sum",
                ),
                threshold=5,  # 5 errors in evaluation period
                evaluation_periods=2,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            db_error_alarm.add_alarm_action(alarm_action)

    def _add_custom_metrics_to_dashboard(self, namespace: str) -> None:
        """Add custom n8n metrics widgets to the dashboard."""
        # Workflow metrics widget
        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Workflow Execution Metrics",
                left=[
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WorkflowExecutionSuccess",
                        statistic="Sum",
                        label="Successful Executions",
                        color=cloudwatch.Color.GREEN,
                    ),
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WorkflowExecutionFailure",
                        statistic="Sum",
                        label="Failed Executions",
                        color=cloudwatch.Color.RED,
                    ),
                ],
                right=[
                    cloudwatch.MathExpression(
                        expression="(m2 / (m1 + m2)) * 100",
                        using_metrics={
                            "m1": cloudwatch.Metric(
                                namespace=namespace,
                                metric_name="WorkflowExecutionSuccess",
                                statistic="Sum",
                            ),
                            "m2": cloudwatch.Metric(
                                namespace=namespace,
                                metric_name="WorkflowExecutionFailure",
                                statistic="Sum",
                            ),
                        },
                        label="Failure Rate %",
                        color=cloudwatch.Color.ORANGE,
                    ),
                ],
                width=12,
                height=6,
            ),
            cloudwatch.GraphWidget(
                title="Webhook Performance",
                left=[
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WebhookRequests",
                        statistic="Sum",
                        label="Total Requests",
                    ),
                ],
                right=[
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WebhookResponseTime",
                        statistic="Average",
                        label="Avg Response Time (ms)",
                        color=cloudwatch.Color.BLUE,
                    ),
                ],
                width=12,
                height=6,
            ),
        )

        # Performance metrics widget
        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Performance Metrics",
                left=[
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="NodeExecutionTime",
                        statistic="Average",
                        label="Avg Node Execution Time (ms)",
                    ),
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WorkflowExecutionDuration",
                        statistic="Average",
                        label="Avg Workflow Duration (ms)",
                    ),
                ],
                right=[
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="WorkflowQueueDepth",
                        statistic="Maximum",
                        label="Max Queue Depth",
                        color=cloudwatch.Color.PURPLE,
                    ),
                ],
                width=24,
                height=6,
            ),
        )

        # Error tracking widget
        self.dashboard.add_widgets(
            cloudwatch.SingleValueWidget(
                title="Authentication Errors (24h)",
                metrics=[
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="AuthenticationErrors",
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                ],
                width=6,
                height=4,
            ),
            cloudwatch.SingleValueWidget(
                title="Database Errors (24h)",
                metrics=[
                    cloudwatch.Metric(
                        namespace=namespace,
                        metric_name="DatabaseConnectionErrors",
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                ],
                width=6,
                height=4,
            ),
            cloudwatch.SingleValueWidget(
                title="Workflow Success Rate (24h)",
                metrics=[
                    cloudwatch.MathExpression(
                        expression="(successes / (successes + failures)) * 100",
                        using_metrics={
                            "successes": cloudwatch.Metric(
                                namespace=namespace,
                                metric_name="WorkflowExecutionSuccess",
                                statistic="Sum",
                                period=Duration.days(1),
                            ),
                            "failures": cloudwatch.Metric(
                                namespace=namespace,
                                metric_name="WorkflowExecutionFailure",
                                statistic="Sum",
                                period=Duration.days(1),
                            ),
                        },
                        label="Success Rate %",
                    ),
                ],
                width=6,
                height=4,
            ),
            cloudwatch.SingleValueWidget(
                title="Total Workflows (24h)",
                metrics=[
                    cloudwatch.MathExpression(
                        expression="successes + failures",
                        using_metrics={
                            "successes": cloudwatch.Metric(
                                namespace=namespace,
                                metric_name="WorkflowExecutionSuccess",
                                statistic="Sum",
                                period=Duration.days(1),
                            ),
                            "failures": cloudwatch.Metric(
                                namespace=namespace,
                                metric_name="WorkflowExecutionFailure",
                                statistic="Sum",
                                period=Duration.days(1),
                            ),
                        },
                        label="Total Executions",
                    ),
                ],
                width=6,
                height=4,
            ),
        )

    def _add_outputs(self) -> None:
        """Add stack outputs."""
        # Alarm topic
        self.add_output(
            "AlarmTopicArn",
            value=self.alarm_topic.topic_arn,
            description="SNS topic for alarms",
        )

        # Dashboard URL
        self.add_output(
            "DashboardUrl",
            value=(
                f"https://{self.region}.console.aws.amazon.com/cloudwatch/home?"
                f"region={self.region}#dashboards:name={self.dashboard.dashboard_name}"
            ),
            description="CloudWatch dashboard URL",
        )
