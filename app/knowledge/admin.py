from typing import TYPE_CHECKING

from django.contrib import admin

from .models import Article

if TYPE_CHECKING:
    ArticleModelAdmin = admin.ModelAdmin[Article]
else:
    ArticleModelAdmin = admin.ModelAdmin


@admin.register(Article)
class ArticleAdmin(ArticleModelAdmin):
    list_display = ["title", "lesson", "published", "updated_at"]
    list_filter = ["published", "lesson"]
    search_fields = ["title", "summary", "body"]
    prepopulated_fields = {"slug": ("title",)}
