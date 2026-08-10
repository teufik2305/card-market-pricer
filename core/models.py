from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AppendOnlyQuerySet(models.QuerySet):
    """Blocks the queryset-level escape hatches around the instance guard.
    (FK cascades are closed separately: every FK pointing AT an append-only
    row, and every FK an append-only row points at, must be PROTECT.)"""

    def update(self, **kwargs):
        raise TypeError(f"{self.model.__name__} rows are append-only and cannot be updated.")

    def delete(self):
        raise TypeError(f"{self.model.__name__} rows are append-only and cannot be deleted.")

    def bulk_update(self, objs, fields, **kwargs):
        raise TypeError(f"{self.model.__name__} rows are append-only and cannot be updated.")


class AppendOnly(models.Model):
    """History rows are written once and never updated or deleted."""

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise TypeError(f"{type(self).__name__} rows are append-only and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError(f"{type(self).__name__} rows are append-only and cannot be deleted.")


class ImportRun(models.Model):
    game = models.ForeignKey("catalog.Game", null=True, blank=True, on_delete=models.SET_NULL)
    command = models.CharField(max_length=50)
    ok = models.BooleanField(default=False)
    counts = models.JSONField(default=dict)
    report = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.command} @ {self.created_at:%Y-%m-%d %H:%M} ({'ok' if self.ok else 'FAILED'})"
