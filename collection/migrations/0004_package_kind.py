"""Fold the keep-list into packages.

Holding.keeper_quantity and the separate /keep-list/ page are gone: reserving
cards is now a package with kind=KEEP, which is one mechanism instead of two.

Operation order is load-bearing — `kind` is added and every existing
reservation is copied into a keep package BEFORE the column is dropped, so the
data survives the schema change. `migrate collection 0003` puts it back.
"""

from django.db import migrations, models

KEEP_PACKAGE_NAME = "Keep — {game}"


def keepers_to_packages(apps, schema_editor):
    Holding = apps.get_model("collection", "Holding")
    SellPackage = apps.get_model("collection", "SellPackage")
    SellPackageItem = apps.get_model("collection", "SellPackageItem")

    reserved = (
        Holding.objects.filter(keeper_quantity__gt=0)
        .select_related("card__expansion__game")
    )
    packages = {}
    made = 0
    for holding in reserved:
        game = holding.card.expansion.game
        key = (holding.owner_id, game.pk)
        package = packages.get(key)
        if package is None:
            package, _ = SellPackage.objects.get_or_create(
                owner_id=holding.owner_id,
                game=game,
                kind="keep",
                name=KEEP_PACKAGE_NAME.format(game=game.name),
                defaults={
                    "status": "draft",
                    "notes": "Migrated from the keep-list on 2026-08-09. "
                             "Copies here are excluded from what a sell package may commit.",
                },
            )
            packages[key] = package
        SellPackageItem.objects.update_or_create(
            package=package,
            card=holding.card,
            defaults={
                "quantity": holding.keeper_quantity,
                # No listing price: these are not for sale, so freezing a price
                # would be meaningless.
                "unit_price_at_listing": None,
            },
        )
        made += 1
    if made:
        print(f"\n  moved {made} reserved cards into {len(packages)} keep package(s)")


def packages_to_keepers(apps, schema_editor):
    """Reverse: put the reservations back on the holdings."""
    Holding = apps.get_model("collection", "Holding")
    SellPackageItem = apps.get_model("collection", "SellPackageItem")
    for item in SellPackageItem.objects.filter(package__kind="keep").select_related("package"):
        Holding.objects.filter(
            card=item.card_id, owner=item.package.owner_id
        ).update(keeper_quantity=item.quantity)


class Migration(migrations.Migration):

    dependencies = [
        ('collection', '0003_quantitychange_old_keeper_quantity_sellpackage_game'),
    ]

    operations = [
        migrations.AddField(
            model_name='sellpackage',
            name='kind',
            field=models.CharField(
                choices=[('sell', 'For sale'), ('keep', 'Keep — never sell')],
                db_index=True, default='sell', max_length=5,
            ),
        ),
        migrations.RunPython(keepers_to_packages, packages_to_keepers),
        migrations.RemoveConstraint(
            model_name='holding',
            name='keeper_lte_quantity',
        ),
        migrations.RemoveField(
            model_name='holding',
            name='keeper_quantity',
        ),
    ]
