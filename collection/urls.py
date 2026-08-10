from django.urls import path

from . import views

urlpatterns = [
    path("qty/<int:card_id>/", views.update_quantity, name="update-quantity"),
    path("changes/<int:pk>/undo/", views.undo_change, name="undo-change"),
    path("bulk/preview/", views.bulk_preview, name="bulk-preview"),
    path("bulk/apply/", views.bulk_apply, name="bulk-apply"),
    path("bulk/<uuid:batch_id>/undo/", views.bulk_undo, name="bulk-undo"),
    path("quick-add/", views.quick_add, name="quick-add"),
]
