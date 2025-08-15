"""
Advanced performance monitoring for session recordings API.

This module provides:
1. Real-time performance metrics collection
2. Query performance alerts and thresholds
3. Performance regression detection
4. Optimization recommendations
"""
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from prometheus_client import Counter, Histogram, Gauge

logger = structlog.get_logger(__name__)

# Performance metrics
QUERY_PERFORMANCE_HISTOGRAM = Histogram(
    "session_recordings_query_duration_seconds",
    "Time spent executing session recordings queries",
    labelnames=["operation", "team_id", "query_type"],
)

QUERY_COUNT_COUNTER = Counter(
    "session_recordings_query_count_total",
    "Total number of database queries executed",
    labelnames=["operation", "team_id"],
)

QUERY_PERFORMANCE_ALERT_COUNTER = Counter(
    "session_recordings_performance_alerts_total",
    "Number of performance alerts triggered",
    labelnames=["alert_type", "team_id", "operation"],
)

ACTIVE_QUERIES_GAUGE = Gauge(
    "session_recordings_active_queries",
    "Number of currently active session recordings queries",
    labelnames=["operation"],
)


@dataclass
class PerformanceThresholds:
    """Define performance thresholds for different operations."""
    
    # Query execution time thresholds (seconds)
    max_query_time: float = 5.0
    warning_query_time: float = 2.0
    
    # Query count thresholds
    max_query_count: int = 10
    warning_query_count: int = 5
    
    # Memory usage thresholds (MB)
    max_memory_usage: float = 100.0
    warning_memory_usage: float = 50.0


@dataclass
class PerformanceMetrics:
    """Container for performance metrics collected during operations."""
    
    operation: str
    team_id: Optional[int]
    execution_time: float
    query_count: int
    memory_usage_mb: float
    cache_hits: int = 0
    cache_misses: int = 0
    optimization_opportunities: List[str] = None
    
    def __post_init__(self):
        if self.optimization_opportunities is None:
            self.optimization_opportunities = []


class SessionRecordingPerformanceMonitor:
    """
    Comprehensive performance monitoring for session recordings operations.
    
    Tracks query performance, identifies bottlenecks, and provides optimization recommendations.
    """
    
    def __init__(self, thresholds: Optional[PerformanceThresholds] = None):
        self.thresholds = thresholds or PerformanceThresholds()
        self._cache_prefix = "session_recording_perf"
        
    @contextmanager
    def monitor_operation(self, operation: str, team_id: Optional[int] = None):
        """
        Context manager to monitor a session recordings operation.
        
        Usage:
            with monitor.monitor_operation("load_recordings", team_id=123):
                # Your operation code here
                recordings = load_recordings()
        """
        start_time = time.time()
        start_memory = self._get_memory_usage()
        initial_query_count = len(connection.queries)
        
        # Update active queries gauge
        ACTIVE_QUERIES_GAUGE.labels(operation=operation).inc()
        
        try:
            yield
            
            # Calculate metrics
            execution_time = time.time() - start_time
            query_count = len(connection.queries) - initial_query_count
            memory_usage = self._get_memory_usage() - start_memory
            
            # Create metrics object
            metrics = PerformanceMetrics(
                operation=operation,
                team_id=team_id,
                execution_time=execution_time,
                query_count=query_count,
                memory_usage_mb=memory_usage,
            )
            
            # Record metrics
            self._record_metrics(metrics)
            
            # Check for performance issues
            self._check_performance_thresholds(metrics)
            
            # Identify optimization opportunities
            self._identify_optimizations(metrics)
            
            # Log performance summary
            self._log_performance_summary(metrics)
            
        except Exception as e:
            logger.error(
                "Error during performance monitoring",
                operation=operation,
                team_id=team_id,
                error=str(e),
            )
            raise
        finally:
            ACTIVE_QUERIES_GAUGE.labels(operation=operation).dec()
    
    def _get_memory_usage(self) -> float:
        """Get current memory usage in MB."""
        try:
            import psutil
            import os
            process = psutil.Process(os.getpid())
            return process.memory_info().rss / 1024 / 1024  # Convert to MB
        except ImportError:
            return 0.0
    
    def _record_metrics(self, metrics: PerformanceMetrics):
        """Record metrics to Prometheus and cache."""
        team_id_str = str(metrics.team_id) if metrics.team_id else "unknown"
        
        # Record to Prometheus
        QUERY_PERFORMANCE_HISTOGRAM.labels(
            operation=metrics.operation,
            team_id=team_id_str,
            query_type="session_recordings"
        ).observe(metrics.execution_time)
        
        QUERY_COUNT_COUNTER.labels(
            operation=metrics.operation,
            team_id=team_id_str
        ).inc(metrics.query_count)
        
        # Store historical data in cache for trend analysis
        cache_key = f"{self._cache_prefix}:history:{metrics.operation}:{team_id_str}"
        historical_data = cache.get(cache_key, [])
        
        # Keep only last 100 measurements
        historical_data.append({
            "timestamp": time.time(),
            "execution_time": metrics.execution_time,
            "query_count": metrics.query_count,
            "memory_usage": metrics.memory_usage_mb,
        })
        
        if len(historical_data) > 100:
            historical_data = historical_data[-100:]
        
        cache.set(cache_key, historical_data, timeout=3600)  # 1 hour
    
    def _check_performance_thresholds(self, metrics: PerformanceMetrics):
        """Check if metrics exceed performance thresholds and trigger alerts."""
        team_id_str = str(metrics.team_id) if metrics.team_id else "unknown"
        
        # Check query execution time
        if metrics.execution_time > self.thresholds.max_query_time:
            self._trigger_alert(
                "query_time_critical",
                metrics.operation,
                team_id_str,
                f"Query execution time ({metrics.execution_time:.2f}s) exceeds critical threshold"
            )
        elif metrics.execution_time > self.thresholds.warning_query_time:
            self._trigger_alert(
                "query_time_warning",
                metrics.operation,
                team_id_str,
                f"Query execution time ({metrics.execution_time:.2f}s) exceeds warning threshold"
            )
        
        # Check query count
        if metrics.query_count > self.thresholds.max_query_count:
            self._trigger_alert(
                "query_count_critical",
                metrics.operation,
                team_id_str,
                f"Query count ({metrics.query_count}) exceeds critical threshold - possible N+1 query issue"
            )
        elif metrics.query_count > self.thresholds.warning_query_count:
            self._trigger_alert(
                "query_count_warning",
                metrics.operation,
                team_id_str,
                f"Query count ({metrics.query_count}) exceeds warning threshold"
            )
        
        # Check memory usage
        if metrics.memory_usage_mb > self.thresholds.max_memory_usage:
            self._trigger_alert(
                "memory_usage_critical",
                metrics.operation,
                team_id_str,
                f"Memory usage ({metrics.memory_usage_mb:.2f}MB) exceeds critical threshold"
            )
    
    def _trigger_alert(self, alert_type: str, operation: str, team_id: str, message: str):
        """Trigger a performance alert."""
        QUERY_PERFORMANCE_ALERT_COUNTER.labels(
            alert_type=alert_type,
            team_id=team_id,
            operation=operation
        ).inc()
        
        logger.warning(
            "Performance alert triggered",
            alert_type=alert_type,
            operation=operation,
            team_id=team_id,
            message=message
        )
        
        # Store alert in cache for dashboard display
        cache_key = f"{self._cache_prefix}:alerts:{team_id}"
        alerts = cache.get(cache_key, [])
        
        alerts.append({
            "timestamp": time.time(),
            "alert_type": alert_type,
            "operation": operation,
            "message": message,
        })
        
        # Keep only last 50 alerts
        if len(alerts) > 50:
            alerts = alerts[-50:]
        
        cache.set(cache_key, alerts, timeout=24*3600)  # 24 hours
    
    def _identify_optimizations(self, metrics: PerformanceMetrics):
        """Identify potential optimization opportunities based on metrics."""
        optimizations = []
        
        # Check for potential N+1 query issues
        if metrics.query_count > 5 and metrics.execution_time > 1.0:
            optimizations.append(
                f"High query count ({metrics.query_count}) detected - consider using select_related or prefetch_related"
            )
        
        # Check for slow individual queries
        if metrics.query_count <= 3 and metrics.execution_time > 2.0:
            optimizations.append(
                "Slow query detected - consider adding database indexes or optimizing query logic"
            )
        
        # Check for memory-intensive operations
        if metrics.memory_usage_mb > 25.0:
            optimizations.append(
                f"High memory usage ({metrics.memory_usage_mb:.1f}MB) - consider pagination or streaming"
            )
        
        # Store optimization recommendations
        if optimizations:
            metrics.optimization_opportunities.extend(optimizations)
            
            cache_key = f"{self._cache_prefix}:optimizations:{metrics.operation}"
            cache.set(cache_key, optimizations, timeout=3600)
    
    def _log_performance_summary(self, metrics: PerformanceMetrics):
        """Log a comprehensive performance summary."""
        logger.info(
            "Session recordings performance summary",
            operation=metrics.operation,
            team_id=metrics.team_id,
            execution_time=f"{metrics.execution_time:.3f}s",
            query_count=metrics.query_count,
            memory_usage=f"{metrics.memory_usage_mb:.1f}MB",
            optimization_opportunities=len(metrics.optimization_opportunities),
        )
    
    def get_performance_report(self, team_id: Optional[int] = None, operation: Optional[str] = None) -> Dict[str, Any]:
        """
        Get a comprehensive performance report for debugging and analysis.
        
        Args:
            team_id: Filter by specific team
            operation: Filter by specific operation
            
        Returns:
            Dictionary containing performance metrics, alerts, and recommendations
        """
        team_id_str = str(team_id) if team_id else "*"
        
        # Get historical performance data
        if operation:
            cache_key = f"{self._cache_prefix}:history:{operation}:{team_id_str}"
            historical_data = cache.get(cache_key, [])
        else:
            historical_data = []
        
        # Get recent alerts
        alerts_key = f"{self._cache_prefix}:alerts:{team_id_str}"
        recent_alerts = cache.get(alerts_key, [])
        
        # Get optimization recommendations
        opt_key = f"{self._cache_prefix}:optimizations:{operation or '*'}"
        optimizations = cache.get(opt_key, [])
        
        # Calculate performance trends
        trends = self._calculate_performance_trends(historical_data)
        
        return {
            "team_id": team_id,
            "operation": operation,
            "historical_data": historical_data[-20:],  # Last 20 measurements
            "recent_alerts": recent_alerts[-10:],  # Last 10 alerts
            "optimization_recommendations": optimizations,
            "performance_trends": trends,
            "thresholds": {
                "max_query_time": self.thresholds.max_query_time,
                "warning_query_time": self.thresholds.warning_query_time,
                "max_query_count": self.thresholds.max_query_count,
                "warning_query_count": self.thresholds.warning_query_count,
                "max_memory_usage": self.thresholds.max_memory_usage,
            }
        }
    
    def _calculate_performance_trends(self, historical_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate performance trends from historical data."""
        if len(historical_data) < 2:
            return {"trend": "insufficient_data", "samples": len(historical_data)}
        
        # Calculate averages for recent vs older data
        recent_data = historical_data[-10:]  # Last 10 measurements
        older_data = historical_data[-20:-10]  # Previous 10 measurements
        
        if not older_data:
            return {"trend": "insufficient_historical_data", "samples": len(historical_data)}
        
        recent_avg_time = sum(d["execution_time"] for d in recent_data) / len(recent_data)
        older_avg_time = sum(d["execution_time"] for d in older_data) / len(older_data)
        
        recent_avg_queries = sum(d["query_count"] for d in recent_data) / len(recent_data)
        older_avg_queries = sum(d["query_count"] for d in older_data) / len(older_data)
        
        time_trend = "improving" if recent_avg_time < older_avg_time else "degrading"
        queries_trend = "improving" if recent_avg_queries < older_avg_queries else "degrading"
        
        return {
            "execution_time": {
                "trend": time_trend,
                "recent_avg": recent_avg_time,
                "older_avg": older_avg_time,
                "change_percent": ((recent_avg_time - older_avg_time) / older_avg_time) * 100,
            },
            "query_count": {
                "trend": queries_trend,
                "recent_avg": recent_avg_queries,
                "older_avg": older_avg_queries,
                "change_percent": ((recent_avg_queries - older_avg_queries) / older_avg_queries) * 100,
            }
        }


# Global performance monitor instance
performance_monitor = SessionRecordingPerformanceMonitor()


# Convenience decorator for monitoring functions
def monitor_performance(operation: str):
    """
    Decorator to monitor the performance of a function.
    
    Usage:
        @monitor_performance("load_recordings")
        def load_recordings(team_id):
            # function implementation
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Try to extract team_id from function arguments
            team_id = None
            if 'team' in kwargs and hasattr(kwargs['team'], 'id'):
                team_id = kwargs['team'].id
            elif 'team_id' in kwargs:
                team_id = kwargs['team_id']
            elif args and hasattr(args[0], 'team') and hasattr(args[0].team, 'id'):
                team_id = args[0].team.id
            
            with performance_monitor.monitor_operation(operation, team_id=team_id):
                return func(*args, **kwargs)
        return wrapper
    return decorator