from django.urls import path

from desk.api import api

urlpatterns = [
    path("api/", api.urls),
]
