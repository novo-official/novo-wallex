"""URL routes: the read-only dashboard API (no write endpoints, nothing reaches Wallex from here)."""

from django.urls import path

from api import views

urlpatterns = [
    path("api/overview", views.overview),
    path("api/decisions", views.decisions),
]
