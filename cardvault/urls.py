from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from catalog import views as catalog_views

admin.site.site_header = "Cardvault records"
admin.site.site_title = "Cardvault records"
admin.site.index_title = "Every table, raw. For fixes the app itself doesn't cover."

urlpatterns = [
    path("admin/", admin.site.urls),
    # Card art is game-agnostic, so it sits above the /g/<game>/ scope.
    path("card-art/<int:piece_id>/<slug:size>/", catalog_views.card_art, name="card-art"),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("core.urls")),
    path("g/<slug:game>/", include("catalog.urls")),
    path("holdings/", include("collection.urls")),
    path("scrape/", include("scraping.urls")),
]
