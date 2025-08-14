# Shared metrics and labels for prometheus metrics
from contextlib import contextmanager

import structlog
from django.conf import settings
from prometheus_client import CollectorRegistry, Counter, Histogram, push_to_gateway
from posthog.exceptions_capture import capture_exception

logger = structlog.get_logger(__name__)

__doc__ = """
This module holds common labels, metrics and helpers for Prometheus instrumentation.

- Common label names should be imported from this module for consistency across metrics.
- Metrics should be declared in the same file than the code that sets them,
  but they could be declared here if set from several code paths.
"""

# Common metric labels
LABEL_PATH = "path"
LABEL_RESOURCE_TYPE = "resource_type"
LABEL_TEAM_ID = "team_id"

KLUDGES_COUNTER = Counter(
    "posthog_kludges_total",
    "Tracking code paths eligible for deletion if they are not used.",
    labelnames=["kludge"],
)

# Production readiness monitoring metrics
PERSONS_ON_EVENTS_QUERY_COUNTER = Counter(
    "posthog_persons_on_events_queries_total",
    "Count of queries using persons on events feature",
    labelnames=[LABEL_TEAM_ID, "query_type", "enabled"],
)

PERSONS_ON_EVENTS_QUERY_DURATION = Histogram(
    "posthog_persons_on_events_query_duration_seconds",
    "Duration of queries using persons on events feature",
    labelnames=[LABEL_TEAM_ID, "query_type", "enabled"],
)

METADATA_PARSING_DURATION = Histogram(
    "posthog_metadata_parsing_duration_seconds",
    "Duration of metadata parsing operations",
    labelnames=[LABEL_TEAM_ID, "operation_type"],
)

METADATA_PARSING_COUNTER = Counter(
    "posthog_metadata_parsing_total",
    "Count of metadata parsing operations",
    labelnames=[LABEL_TEAM_ID, "operation_type", "status"],
)

DECIDE_ENDPOINT_COUNTER = Counter(
    "posthog_decide_endpoint_requests_total",
    "Count of decide endpoint requests",
    labelnames=[LABEL_TEAM_ID, "status", "race_condition_detected"],
)

DECIDE_ENDPOINT_DURATION = Histogram(
    "posthog_decide_endpoint_duration_seconds",
    "Duration of decide endpoint requests",
    labelnames=[LABEL_TEAM_ID, "status"],
)

INGESTION_WARNING_COUNTER = Counter(
    "posthog_ingestion_warnings_total",
    "Count of ingestion warnings by type",
    labelnames=[LABEL_TEAM_ID, "warning_type"],
)


def _push(settings, job, registry):
    push_to_gateway(settings, job, registry)


@contextmanager
def pushed_metrics_registry(job_name: str):
    """
    Return a temporary Prometheus registry that will be pushed to the
    PushGateway when the context closes.

    Parameter job_name: a unique job name to use, all metrics previously
    pushed with that name will be deleted.

    NOTE: only use to expose gauges, for use cases where one value per
    region makes sense (e.g. instance metrics computed by celery jobs).
    """

    registry = CollectorRegistry()
    yield registry
    try:
        if settings.PROM_PUSHGATEWAY_ADDRESS:
            _push(settings.PROM_PUSHGATEWAY_ADDRESS, job=job_name, registry=registry)
    except Exception as err:
        logger.exception("push_to_gateway", target=settings.PROM_PUSHGATEWAY_ADDRESS, exception=err)
        capture_exception(err)
