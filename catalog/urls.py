from django.urls import path

from collection import views as collection_views
from exports import views as export_views

from . import views

urlpatterns = [
    # game-scoped collection management
    path("packages/", collection_views.package_list, name="package-list"),
    path("packages/new/", collection_views.package_create, name="package-create"),
    path("packages/<int:pk>/", collection_views.package_detail, name="package-detail"),
    path("packages/<int:pk>/search/", collection_views.card_search_fragment,
         name="package-search"),
    path("packages/<int:pk>/add/", collection_views.package_add_item, name="package-add-item"),
    path("packages/<int:pk>/rule/preview/", collection_views.package_rule_preview,
         name="package-rule-preview"),
    path("packages/<int:pk>/rule/apply/", collection_views.package_rule_apply,
         name="package-rule-apply"),
    path("packages/<int:pk>/archetypes/", collection_views.package_archetypes,
         name="package-archetypes"),
    path("packages/<int:pk>/items/<int:item_id>/remove/",
         collection_views.package_remove_item, name="package-remove-item"),
    path("packages/<int:pk>/finalize/", collection_views.package_finalize,
         name="package-finalize"),
    # exports
    path("export.xlsx", export_views.export_xlsx, name="export-xlsx"),
    path("export.csv", export_views.export_csv, name="export-csv"),
    path("", views.game_home, name="game-home"),
    path("expansions/", views.expansion_list, name="expansion-list"),
    path("expansions/<slug:slug>/", views.expansion_detail, name="expansion-detail"),
    path("expansions/<slug:slug>/totals/", views.expansion_totals_fragment,
         name="expansion-totals"),
    path("cards/", views.card_list, name="card-list"),
    path("cards/<int:pk>/", views.card_detail, name="card-detail"),
]
