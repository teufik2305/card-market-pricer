from django.urls import path

from . import views

urlpatterns = [
    path("", views.panel, name="scrape-panel"),
    path("settings/", views.save_settings, name="scrape-settings"),
    path("find/expansions/", views.find_expansions, name="scrape-find-expansions"),
    path("find/cards/", views.find_cards, name="scrape-find-cards"),
    path("jobs/", views.create_job, name="scrape-job-create"),
    path("jobs/<int:pk>/", views.job_detail, name="scrape-job-detail"),
    path("jobs/<int:pk>/progress/", views.job_progress, name="scrape-job-progress"),
    path("jobs/<int:pk>/cancel/", views.cancel_job, name="scrape-job-cancel"),
    path("jobs/<int:pk>/force-fail/", views.force_fail_job, name="scrape-job-force-fail"),
    path("jobs/<int:pk>/retry-failed/", views.retry_failed, name="scrape-job-retry-failed"),
]
