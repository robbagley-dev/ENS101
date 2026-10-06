import json
import unittest
from unittest.mock import patch
from tests.app_harness import app


class AssessmentPromptTests(unittest.TestCase):
    def test_readiness_results_are_evidence_in_the_current_user_turn(self):
        assessment = {
            'status': 'incomplete', 'completed_count': 3, 'total': 4,
            'missing': ['Values'], 'holland_code': 'IRC',
            'primary_interests': ['Investigative', 'Realistic', 'Conventional'],
            'personality_scores': {'Extraversion': 2, 'Conscientiousness': 4},
        }
        captured = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({'choices':[{'message':{'content':'Synthetic briefing'}}]}).encode()

        def open_request(request, **kwargs):
            captured.append(json.loads(request.data))
            return Response()

        with patch.object(app, 'LM_STUDIO_URL', 'http://127.0.0.1:1234/v1'), patch.object(app, 'urlopen', open_request):
            app.query_qwen('Generate an Executive Summary and Career & Major recommendations.', 'step2', [], assessment)
        turn = captured[0]['messages'][-1]
        self.assertEqual(turn['role'], 'user')
        self.assertIn('Canonical Assessment Interpretation:', turn['content'])
        self.assertIn('IRC', turn['content'])
        self.assertIn('Systems Analyst', turn['content'])
        self.assertIn('Values', turn['content'])
        self.assertIn('"Extraversion": 2', turn['content'])
        self.assertIn('Do not convert raw scores to normative bands', turn['content'])

    def test_no_assessment_does_not_supply_fabricated_evidence(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"choices":[]}'
        with patch.object(app, 'LM_STUDIO_URL', 'http://127.0.0.1:1234/v1'), patch.object(app, 'urlopen', return_value=Response()) as request:
            app.query_qwen('How should I prepare?', 'step2', [], None)
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['messages'][-1]['content'], 'How should I prepare?')
