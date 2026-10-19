import os
from pathlib import Path
from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from .models import Article


def index(request: HttpRequest) -> HttpResponse:
    response = render(request, "knowledge/index.html", {"articles": Article.objects.filter(published=True)})
    response["X-Backend-Node"] = settings.NODE_NAME
    return response


def detail(request: HttpRequest, slug: str) -> HttpResponse:
    article = get_object_or_404(Article, slug=slug, published=True)
    response = render(request, "knowledge/detail.html", {"article": article})
    response["X-Backend-Node"] = settings.NODE_NAME
    return response


def live(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "alive", "node": settings.NODE_NAME})


def ready(request: HttpRequest) -> JsonResponse:
    root = Path(settings.STATIC_ROOT).parent
    if settings.REQUIRE_SHARED_MOUNT and not os.path.ismount(root):
        return JsonResponse({"status": "storage_unavailable"}, status=503)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        with (root / ".ready").open("rb") as sentinel:
            if sentinel.read(16).strip() != b"otus-dz04":
                return JsonResponse({"status": "storage_unavailable"}, status=503)
    except (DatabaseError, OSError):
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready", "node": settings.NODE_NAME})
