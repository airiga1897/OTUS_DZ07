"""Request journal without paths, query strings or user data."""
import logging
from collections.abc import Callable
from django.http import HttpRequest, HttpResponse

LOGGER = logging.getLogger('otus.requests')

class RequestJournalMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        LOGGER.info('HTTP method=%s status=%s', request.method, response.status_code)
        return response
