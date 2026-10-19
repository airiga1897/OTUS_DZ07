from django.contrib import admin
from .models import Article

@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ["title", "lesson", "published", "updated_at"]
    list_filter = ["published", "lesson"]
    search_fields = ["title", "summary", "body"]
    prepopulated_fields = {"slug": ("title",)}
