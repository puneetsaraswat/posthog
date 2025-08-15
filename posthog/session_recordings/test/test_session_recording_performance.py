"""
Performance benchmarks for session recordings API to ensure N+1 query fixes remain effective.

This test file validates that the person loading optimization in session recordings
listing doesn't regress and maintains good performance characteristics.
"""
import time
from datetime import UTC, datetime
from typing import List
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.test.utils import override_settings
from rest_framework import status

from posthog.api.test.test_team import create_team
from posthog.models import Person, PersonDistinctId, Team, User
from posthog.session_recordings.models.session_recording import SessionRecording
from posthog.session_recordings.session_recording_api import list_recordings_from_query
from posthog.schema import RecordingsQuery
from posthog.test.base import APIBaseTest


class TestSessionRecordingPerformance(APIBaseTest):
    """
    Performance tests to ensure N+1 query optimizations remain effective.
    
    These tests validate:
    1. Person loading uses bulk queries instead of N+1 pattern
    2. Performance scales linearly with batch size
    3. Database query count remains constant regardless of recording count
    """

    def setUp(self):
        super().setUp()
        
        # Create test persons and recordings
        self.test_persons = []
        self.test_recordings = []
        
        # Create 20 test recordings with associated persons to test bulk loading
        for i in range(20):
            person = Person.objects.create(
                team=self.team,
                properties={"email": f"user{i}@example.com", "name": f"User {i}"},
            )
            distinct_id = f"user_{i}_distinct_id"
            PersonDistinctId.objects.create(
                team=self.team,
                person=person,
                distinct_id=distinct_id,
            )
            
            recording = SessionRecording.objects.create(
                team=self.team,
                session_id=f"session_{i}",
                distinct_id=distinct_id,
                start_time=datetime.now(UTC),
                end_time=datetime.now(UTC),
            )
            
            self.test_persons.append(person)
            self.test_recordings.append(recording)

    def test_person_loading_query_count_is_constant(self):
        """
        Validates that person loading uses O(1) queries regardless of recording count.
        This test ensures the N+1 query fix remains effective.
        """
        # Test with different batch sizes to ensure query count stays constant
        test_cases = [
            {"recording_count": 5, "expected_max_queries": 4},   # Small batch
            {"recording_count": 10, "expected_max_queries": 4},  # Medium batch  
            {"recording_count": 20, "expected_max_queries": 4},  # Large batch
        ]
        
        for case in test_cases:
            with self.subTest(recording_count=case["recording_count"]):
                recording_ids = [r.session_id for r in self.test_recordings[:case["recording_count"]]]
                
                query = RecordingsQuery(
                    session_ids=recording_ids,
                    date_from=None,
                    date_to=None,
                )
                
                with self.assertNumQueries(case["expected_max_queries"]):
                    recordings, _, _ = list_recordings_from_query(query, self.user, self.team)
                    
                    # Validate all recordings have person data loaded
                    for recording in recordings:
                        if recording.distinct_id:  # Some recordings might not have persons
                            # Access person data to trigger any lazy loading
                            _ = recording.person.properties if recording.person else None

    def test_performance_scales_linearly(self):
        """
        Validates that performance scales linearly with batch size, not quadratically.
        This ensures the bulk loading optimization is working correctly.
        """
        batch_sizes = [5, 10, 20]
        execution_times = []
        
        for batch_size in batch_sizes:
            recording_ids = [r.session_id for r in self.test_recordings[:batch_size]]
            
            query = RecordingsQuery(
                session_ids=recording_ids,
                date_from=None,
                date_to=None,
            )
            
            start_time = time.time()
            recordings, _, _ = list_recordings_from_query(query, self.user, self.team)
            
            # Force evaluation of person data to ensure it's loaded
            for recording in recordings:
                if recording.person:
                    _ = recording.person.properties
                    
            execution_time = time.time() - start_time
            execution_times.append(execution_time)
            
        # Ensure performance doesn't degrade quadratically
        # The ratio of execution times should be roughly linear
        if len(execution_times) >= 2:
            # Time ratio should be less than batch size ratio squared
            time_ratio = execution_times[-1] / execution_times[0]
            batch_ratio = batch_sizes[-1] / batch_sizes[0]
            
            # Performance should scale better than quadratically
            self.assertLess(
                time_ratio, 
                batch_ratio ** 1.5,  # Allow some overhead but prevent quadratic growth
                f"Performance scaling is worse than expected. Time ratio: {time_ratio}, Batch ratio: {batch_ratio}"
            )

    def test_person_data_correctness_after_optimization(self):
        """
        Validates that the bulk loading optimization doesn't compromise data correctness.
        """
        recording_ids = [r.session_id for r in self.test_recordings[:10]]
        
        query = RecordingsQuery(
            session_ids=recording_ids,
            date_from=None,
            date_to=None,
        )
        
        recordings, _, _ = list_recordings_from_query(query, self.user, self.team)
        
        # Validate each recording has correct person data
        for recording in recordings:
            if recording.distinct_id and recording.person:
                # Find the expected person
                expected_person = None
                for person in self.test_persons:
                    person_distinct_ids = PersonDistinctId.objects.filter(
                        person=person, 
                        distinct_id=recording.distinct_id
                    )
                    if person_distinct_ids.exists():
                        expected_person = person
                        break
                
                if expected_person:
                    self.assertEqual(
                        recording.person.id, 
                        expected_person.id,
                        f"Person mismatch for recording {recording.session_id}"
                    )

    @patch('posthog.session_recordings.session_recording_api.PersonDistinctId.objects')
    def test_select_related_is_used(self, mock_person_distinct_id):
        """
        Validates that select_related is being used in the person loading query.
        This is a regression test to ensure the N+1 fix doesn't get accidentally removed.
        """
        # Configure the mock to track method calls
        mock_queryset = mock_person_distinct_id.db_manager.return_value.filter.return_value
        mock_queryset.select_related.return_value = mock_queryset
        mock_queryset.__iter__ = lambda self: iter([])  # Return empty iterator
        
        recording_ids = [r.session_id for r in self.test_recordings[:5]]
        
        query = RecordingsQuery(
            session_ids=recording_ids,
            date_from=None,
            date_to=None,
        )
        
        list_recordings_from_query(query, self.user, self.team)
        
        # Verify select_related("person") was called
        mock_queryset.select_related.assert_called_with("person")

    def test_api_endpoint_performance(self):
        """
        Integration test to ensure the API endpoint performs well under load.
        """
        # Create URL with multiple session IDs
        session_ids = [r.session_id for r in self.test_recordings[:15]]
        session_ids_param = ",".join(session_ids)
        
        start_time = time.time()
        
        response = self.client.get(
            f"/api/projects/{self.team.id}/session_recordings/",
            {"session_ids": session_ids_param}
        )
        
        execution_time = time.time() - start_time
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Ensure reasonable response time (adjust threshold as needed)
        self.assertLess(
            execution_time, 
            2.0,  # 2 seconds max for 15 recordings
            f"API response time too slow: {execution_time}s"
        )
        
        # Ensure all recordings have person data
        data = response.json()
        recordings_with_persons = [
            r for r in data["results"] 
            if r.get("person") is not None
        ]
        
        # At least some recordings should have person data
        self.assertGreater(
            len(recordings_with_persons), 
            0,
            "No recordings have person data loaded"
        )


# Benchmarking utilities for manual performance testing
class SessionRecordingPerformanceBenchmark:
    """
    Utility class for running performance benchmarks manually.
    Use this for more detailed performance analysis during development.
    """
    
    @staticmethod
    def benchmark_person_loading(team: Team, user: User, recording_count: int = 50):
        """
        Benchmark person loading performance with a specific number of recordings.
        
        Args:
            team: Team instance
            user: User instance  
            recording_count: Number of recordings to test with
            
        Returns:
            dict: Performance metrics including execution time and query count
        """
        from django.test.utils import override_settings
        from django.db import connection
        from django.conf import settings
        
        # Create test data
        recordings = []
        for i in range(recording_count):
            person = Person.objects.create(
                team=team,
                properties={"name": f"Benchmark User {i}"}
            )
            distinct_id = f"benchmark_user_{i}"
            PersonDistinctId.objects.create(
                team=team,
                person=person, 
                distinct_id=distinct_id
            )
            recording = SessionRecording.objects.create(
                team=team,
                session_id=f"benchmark_session_{i}",
                distinct_id=distinct_id,
                start_time=datetime.now(UTC),
                end_time=datetime.now(UTC),
            )
            recordings.append(recording)
        
        try:
            # Benchmark the optimized implementation
            query = RecordingsQuery(
                session_ids=[r.session_id for r in recordings],
                date_from=None,
                date_to=None,
            )
            
            # Clear query log
            connection.queries_log.clear()
            
            start_time = time.time()
            results, _, _ = list_recordings_from_query(query, user, team)
            
            # Force person data access
            for recording in results:
                if recording.person:
                    _ = recording.person.properties
                    
            execution_time = time.time() - start_time
            query_count = len(connection.queries)
            
            return {
                "recording_count": recording_count,
                "execution_time": execution_time,
                "query_count": query_count,
                "queries_per_second": query_count / execution_time if execution_time > 0 else 0,
                "recordings_per_second": recording_count / execution_time if execution_time > 0 else 0,
            }
        finally:
            # Cleanup test data
            SessionRecording.objects.filter(session_id__startswith="benchmark_session_").delete()
            PersonDistinctId.objects.filter(distinct_id__startswith="benchmark_user_").delete()
            Person.objects.filter(properties__name__startswith="Benchmark User").delete()