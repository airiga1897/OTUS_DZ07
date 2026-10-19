from django.urls import path
from . import views
urlpatterns = [path("", views.index), path("articles/<slug:slug>/", views.detail, name="article"),
               path("health/live", views.live), path("health/ready", views.ready)]
