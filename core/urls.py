from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI

from blog.api import router as blog_router
from core.health import healthz, readyz

api = NinjaAPI()
api.add_router("/", blog_router)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    # Liveness and readiness probes at the project root, NOT under /api/,
    # so a reverse proxy / orchestrator can hit them without coupling
    # to the application routing. healthz must never touch the DB;
    # readyz must.
    path("healthz", healthz),
    path("readyz", readyz),
]
