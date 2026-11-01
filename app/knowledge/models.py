from django.core.validators import FileExtensionValidator
from django.db import models

class Article(models.Model):
    title = models.CharField("Заголовок", max_length=180)
    slug = models.SlugField("Адрес", unique=True)
    lesson = models.PositiveSmallIntegerField("Номер ДЗ")
    summary = models.CharField("Краткое описание", max_length=320)
    body = models.TextField("Материал")
    image = models.ImageField("Изображение", upload_to="articles/%Y/%m/", blank=True,
                              validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"])])
    published = models.BooleanField("Опубликовано", default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["lesson", "title"]
        verbose_name = "Материал"
        verbose_name_plural = "Материалы"

    def __str__(self) -> str:
        return self.title


class InitialDataState(models.Model):
    key = models.CharField(max_length=80, primary_key=True)
    status = models.CharField(max_length=32)
    fixture_sha256 = models.CharField(max_length=64)

    class Meta:
        default_permissions = ()
