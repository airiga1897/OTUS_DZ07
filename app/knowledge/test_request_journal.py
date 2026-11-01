from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from .middleware import RequestJournalMiddleware


class RequestJournalTest(SimpleTestCase):
    def test_request_metadata_excludes_url_and_body(self):
        request = RequestFactory().post('/private?token=secret-value', {'password': 'hidden-value'})
        with self.assertLogs('otus.requests', 'INFO') as captured:
            response = RequestJournalMiddleware(lambda _: HttpResponse(status=201))(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(captured.output, ['INFO:otus.requests:HTTP method=POST status=201'])
