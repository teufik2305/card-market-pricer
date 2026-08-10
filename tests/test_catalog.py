"""Catalog import, printing→piece resolution, art cache, and scraper settings.

No network: the provider payloads are the real shapes captured from
db.ygoprodeck.com and digimoncard.io on 2026-08-09, fed in through a stubbed
fetcher.
"""

import json
from decimal import Decimal

import pytest

from catalog import catalogs, images, resolution
from catalog.models import CardPiece, CardPieceAlias, Printing
from catalog.normalize import normalize_name, slug_display_name
from scraping.models import ScraperSettings

YGO_PAYLOAD = {
    "data": [
        {
            "id": 46986414, "name": "Dark Magician", "type": "Normal Monster",
            "frameType": "normal", "desc": "The ultimate wizard in terms of attack and defense.",
            "atk": 2500, "def": 2100, "level": 7, "race": "Spellcaster", "attribute": "DARK",
            "archetype": "Dark Magician",
            "card_sets": [{"set_code": "LOB-005", "set_rarity": "Ultra Rare"}],
            "card_images": [
                {"id": 46986414, "image_url": "https://images.ygoprodeck.com/images/cards/46986414.jpg",
                 "image_url_small": "https://images.ygoprodeck.com/images/cards_small/46986414.jpg",
                 "image_url_cropped": "https://images.ygoprodeck.com/images/cards_cropped/46986414.jpg"},
                {"id": 36996508, "image_url": "https://images.ygoprodeck.com/images/cards/36996508.jpg",
                 "image_url_small": "https://images.ygoprodeck.com/images/cards_small/36996508.jpg",
                 "image_url_cropped": ""},
            ],
            "card_prices": [{"cardmarket_price": "0.29", "tcgplayer_price": "0.44"}],
            "banlist_info": {},
            "misc_info": [{"konami_id": 4041, "tcg_date": "2002-03-08"}],
        },
        {
            "id": 89631139, "name": "Blue-Eyes White Dragon", "type": "Normal Monster",
            "frameType": "normal", "desc": "This legendary dragon is a powerful engine of destruction.",
            "atk": 3000, "def": 2500, "level": 8, "race": "Dragon", "attribute": "LIGHT",
            "archetype": "Blue-Eyes",
            "card_images": [{"id": 89631139, "image_url": "https://images.ygoprodeck.com/images/cards/89631139.jpg",
                             "image_url_small": "https://images.ygoprodeck.com/images/cards_small/89631139.jpg",
                             "image_url_cropped": ""}],
            "card_prices": [{"cardmarket_price": "1.50"}],
            "banlist_info": {"ban_tcg": "Limited"},
            "misc_info": [{}],
        },
    ]
}

DIGIMON_PAYLOAD = [
    {"name": "Agumon", "type": "Digimon", "id": "ST1-03", "level": 3, "play_cost": 3,
     "evolution_cost": 0, "color": "Red", "digi_type": "Reptile", "form": "Rookie",
     "dp": 2000, "attribute": "Vaccine", "rarity": "u", "stage": "Rookie",
     "main_effect": "[Your Turn] This Digimon gets +1000 DP.", "source_effect": "",
     "series": "Digimon Card Game", "pretty_url": "agumon-st1-03", "set_name": ["ST-1"]},
    # Same card, second set appearance — must collapse to one piece.
    {"name": "Agumon", "type": "Digimon", "id": "ST1-03", "level": 3, "play_cost": 3,
     "color": "Red", "digi_type": "Reptile", "dp": 2000, "attribute": "Vaccine",
     "series": "Digimon Card Game", "set_name": ["ST-11"]},
    {"name": "Achillesmon", "type": "Digimon", "id": "BT10-040", "level": 5, "play_cost": 8,
     "color": "Blue", "digi_type": "Warrior", "dp": 9000, "attribute": "Data",
     "series": "Digimon Card Game", "set_name": ["BT-10"]},
]


@pytest.fixture
def stub_fetch(monkeypatch):
    """Serve the captured payloads instead of hitting the providers."""
    def fake(url, *, params=None):
        if "ygoprodeck" in url and "checkDBVer" in url:
            return [{"database_version": "146.37"}]
        if "ygoprodeck" in url:
            return json.loads(json.dumps(YGO_PAYLOAD))
        return json.loads(json.dumps(DIGIMON_PAYLOAD))
    monkeypatch.setattr(catalogs, "_fetch_json", fake)


def make_printing(expansion, slug):
    return Printing.objects.create(
        expansion=expansion, slug=slug, display_name=slug_display_name(slug),
        name_normalized=normalize_name(slug),
    )


@pytest.mark.django_db
class TestYgoprodeckImport:
    def test_imports_pieces_with_metadata_and_prices(self, game, stub_fetch):
        result = catalogs.import_ygoprodeck(game, log=lambda *a: None)
        assert result.created == 2
        assert result.version == "146.37"

        piece = CardPiece.objects.get(external_id="46986414")
        assert piece.name == "Dark Magician"
        assert (piece.race, piece.attribute, piece.level) == ("Spellcaster", "DARK", 7)
        assert (piece.atk, piece.defence) == (2500, 2100)
        assert piece.archetype == "Dark Magician"
        assert piece.catalog_price_eur == Decimal("0.29")
        assert piece.konami_id == "4041"
        assert piece.extra["set_codes"] == ["LOB-005"]

    def test_alt_art_passcodes_become_aliases(self, game, stub_fetch):
        """.ydk decklists reference the alt-art id, not the base passcode."""
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        alias = CardPieceAlias.objects.get(alias_passcode="36996508")
        assert alias.piece.name == "Dark Magician"
        # The card's own id is not duplicated as an alias.
        assert not CardPieceAlias.objects.filter(alias_passcode="46986414").exists()

    def test_banlist_status_is_kept(self, game, stub_fetch):
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        assert CardPiece.objects.get(external_id="89631139").ban_tcg == "Limited"

    def test_reimport_updates_rather_than_duplicates(self, game, stub_fetch):
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        second = catalogs.import_ygoprodeck(game, log=lambda *a: None)
        assert (second.created, second.updated) == (0, 2)
        assert CardPiece.objects.count() == 2

    def test_import_never_relinks_printings(self, game, expansion, stub_fetch):
        """A catalog refresh must not silently undo a human's resolution."""
        printing = make_printing(expansion, "Dark-Magician")
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        printing.refresh_from_db()
        assert printing.piece_id is None
        assert printing.resolution_status == Printing.Resolution.UNRESOLVED


@pytest.mark.django_db
class TestDigimonImport:
    def test_duplicate_set_rows_collapse_to_one_piece(self, digimon_game, stub_fetch):
        result = catalogs.import_digimoncard(digimon_game, log=lambda *a: None)
        assert result.created == 2  # Agumon appears twice in the feed
        agumon = CardPiece.objects.get(external_id="ST1-03")
        assert (agumon.colour, agumon.attribute, agumon.atk) == ("Red", "Vaccine", 2000)
        assert agumon.play_cost == 3

    def test_art_url_is_derived_from_the_card_number(self, digimon_game, stub_fetch):
        catalogs.import_digimoncard(digimon_game, log=lambda *a: None)
        piece = CardPiece.objects.get(external_id="BT10-040")
        assert piece.image_url == "https://images.digimoncard.io/images/cards/BT10-040.jpg"

    def test_shared_names_are_flagged_ambiguous(self, digimon_game, stub_fetch):
        """Digimon names repeat constantly, so name matching must never auto-resolve."""
        CardPiece.objects.create(
            game=digimon_game, external_id="BT1-010", name="Agumon",
            normalized_name=normalize_name("Agumon"),
        )
        catalogs.import_digimoncard(digimon_game, log=lambda *a: None)
        assert CardPiece.objects.filter(
            normalized_name="agumon", ambiguous_normalized=True
        ).count() == 2


class TestSlugParsing:
    @pytest.mark.parametrize("slug,expected", [
        ("Achillesmon-BT10-040", "BT10-040"),
        ("A-Delicate-Plan-BT3-097-U", "BT3-097"),
        ("Agumon-BT11-046-V2", "BT11-046"),          # alt-art suffix
        ("AeroVeedramon-Zero-P-047-P-1", "P-047"),   # promo, trailing junk
        ("ADR-02-Searcher-EX2-046", "EX2-046"),      # name looks like a set code
        ("SlashAngemon-BT1-62-AA", "BT1-62"),
        ("Nothing-Here", None),
    ])
    def test_digimon_card_number_extraction(self, slug, expected):
        assert resolution.digimon_external_id(slug) == expected

    @pytest.mark.parametrize("slug,expected", [
        ("Dark-Magician", "Dark-Magician"),
        ("Accesscode-Talker-V1-Secret-Rare", "Accesscode-Talker"),
        ("A-Team-Trap-Disposal-Unit-V-1-Rare", "A-Team-Trap-Disposal-Unit"),
        ("Abyss-sphere-V-2", "Abyss-sphere"),
        ("Number-39-Utopia-Ultra-Parallel-Rare", "Number-39-Utopia"),
    ])
    def test_ygo_candidates_peel_stacked_suffixes(self, slug, expected):
        assert expected in resolution.ygo_candidate_slugs(slug)

    def test_the_untouched_slug_is_always_tried_first(self):
        """Real cards end in rarity words; stripping first would mangle them."""
        for slug in ("Junk-Collector", "Elemental-HERO-Captain-Gold", "Magical-Ghost"):
            assert resolution.ygo_candidate_slugs(slug)[0] == slug


@pytest.mark.django_db
class TestResolution:
    def test_exact_match_wins_over_stripping(self, game, expansion, stub_fetch):
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        printing = make_printing(expansion, "Dark-Magician")
        resolution.resolve_game(game, log=lambda *a: None)
        printing.refresh_from_db()
        assert printing.piece.name == "Dark Magician"
        assert printing.resolution_status == Printing.Resolution.AUTO_EXACT

    def test_rarity_and_version_suffixes_resolve(self, game, expansion, stub_fetch):
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        printing = make_printing(expansion, "Blue-Eyes-White-Dragon-V-2-Ultra-Rare")
        resolution.resolve_game(game, log=lambda *a: None)
        printing.refresh_from_db()
        assert printing.piece.external_id == "89631139"

    def test_tokens_are_ignored_not_failed(self, game, expansion, stub_fetch):
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        printing = make_printing(expansion, "Dark-Magician-Token")
        resolution.resolve_game(game, log=lambda *a: None)
        printing.refresh_from_db()
        assert printing.resolution_status == Printing.Resolution.IGNORED
        assert printing.piece_id is None

    def test_digimon_resolves_by_card_number(self, digimon_game, digimon_expansion, stub_fetch):
        catalogs.import_digimoncard(digimon_game, log=lambda *a: None)
        printing = make_printing(digimon_expansion, "Achillesmon-BT10-040")
        resolution.resolve_game(digimon_game, log=lambda *a: None)
        printing.refresh_from_db()
        assert printing.piece.external_id == "BT10-040"

    def test_manual_resolutions_are_never_overwritten(self, game, expansion, stub_fetch):
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        printing = make_printing(expansion, "Dark-Magician")
        wrong = CardPiece.objects.get(external_id="89631139")
        Printing.objects.filter(pk=printing.pk).update(
            piece=wrong, resolution_status=Printing.Resolution.MANUAL
        )
        resolution.resolve_game(game, log=lambda *a: None)
        printing.refresh_from_db()
        assert printing.piece_id == wrong.pk  # a human said so; leave it alone


@pytest.mark.django_db
class TestArtCache:
    def test_missing_art_falls_back_to_the_placeholder(self, client, superuser, game):
        piece = CardPiece.objects.create(
            game=game, external_id="1", name="Artless", normalized_name="artless"
        )
        client.force_login(superuser)
        response = client.get(f"/card-art/{piece.pk}/small/")
        assert response.status_code == 302
        assert "card-placeholder" in response["Location"]

    def test_unexpected_hosts_are_refused(self, game, settings, tmp_path):
        """The URL comes out of the database; a poisoned row must not turn the
        cache into a request-forgery gadget."""
        settings.CARD_IMAGE_DIR = str(tmp_path)
        piece = CardPiece.objects.create(
            game=game, external_id="2", name="Evil", normalized_name="evil",
            image_small_url="http://169.254.169.254/latest/meta-data/",
        )
        with pytest.raises(images.ImageUnavailable, match="unexpected host"):
            images.ensure_cached(piece, "small")

    def test_a_cached_file_is_served_without_refetching(self, game, settings, tmp_path, monkeypatch):
        settings.CARD_IMAGE_DIR = str(tmp_path)
        piece = CardPiece.objects.create(
            game=game, external_id="3", name="Cached", normalized_name="cached",
            image_small_url="https://images.ygoprodeck.com/images/cards_small/3.jpg",
        )
        path = images.cached_path(piece, "small")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpegbytes")

        def explode(*args, **kwargs):
            raise AssertionError("should not have hit the network")

        monkeypatch.setattr(images.urllib.request, "urlopen", explode)
        assert images.ensure_cached(piece, "small") == path


@pytest.mark.django_db
class TestScraperSettings:
    @pytest.fixture
    def staff(self, client, superuser):
        client.force_login(superuser)
        return client

    def test_chrome_path_must_exist(self, staff):
        response = staff.post("/scrape/settings/", {"chrome_binary": "/nope/chrome"})
        assert "No such file" in response.text
        assert ScraperSettings.load().chrome_binary == ""

    def test_a_folder_is_rejected_with_the_macos_hint(self, staff, tmp_path):
        response = staff.post("/scrape/settings/", {"chrome_binary": str(tmp_path)})
        assert "is a folder" in response.text and "Contents/MacOS" in response.text

    def test_a_valid_path_is_saved_and_used(self, staff, tmp_path):
        from scraping import browser
        chrome = tmp_path / "chrome"
        chrome.write_text("#!/bin/sh\n")
        chrome.chmod(0o755)
        response = staff.post("/scrape/settings/", {
            "chrome_binary": str(chrome), "challenge_timeout": "240",
            "min_item_delay": "10", "max_item_delay": "20",
        })
        assert response.status_code == 302
        config = ScraperSettings.load()
        assert config.chrome_binary == str(chrome)
        assert browser.chrome_binary() == str(chrome)
        assert browser.challenge_timeout() == 240

    def test_blank_falls_back_to_the_platform_default(self, staff, settings):
        from scraping import browser
        assert browser.chrome_binary() == settings.SCRAPE_CHROME_BINARY

    def test_max_delay_cannot_be_below_min(self, staff):
        staff.post("/scrape/settings/", {
            "chrome_binary": "", "challenge_timeout": "180",
            "min_item_delay": "30", "max_item_delay": "5",
        })
        config = ScraperSettings.load()
        assert config.max_item_delay == config.min_item_delay == 30

    def test_settings_are_a_singleton(self, staff):
        ScraperSettings.objects.create(chrome_binary="/a")
        ScraperSettings.objects.create(chrome_binary="/b")
        assert ScraperSettings.objects.count() == 1


@pytest.mark.django_db
class TestBoostBoundaries:
    """hx-boost swaps the <body> only. Any link leaving the htmx app must opt
    out, or the destination renders with the wrong <head> until you refresh."""

    def test_admin_link_opts_out_of_boost(self, client, superuser):
        import re
        client.force_login(superuser)
        body = client.get("/").text
        link = re.search(r'<a[^>]*href="/admin/"[^>]*>', body)
        assert link, "admin link missing from the nav"
        assert 'hx-boost="false"' in link.group(0)

    def test_every_off_app_link_opts_out(self, client, superuser, game):
        """Exports, the admin and outbound links all leave the app shell."""
        import re
        client.force_login(superuser)
        body = client.get("/").text
        for link in re.findall(r"<a\b[^>]*>", body):
            leaves_app = (
                'href="/admin/' in link
                or "/export." in link
                or "http://" in link
                or "https://" in link
            )
            if leaves_app:
                assert 'hx-boost="false"' in link, link


@pytest.mark.django_db
class TestApiJobs:
    """The public APIs are jobs in the same panel as the scraper, so they get
    the same backup, progress, cancel and audit treatment."""

    @pytest.fixture
    def staff(self, client, superuser):
        client.force_login(superuser)
        return client

    @pytest.fixture
    def linked(self, game, expansion, superuser, stub_fetch):
        from collection.services import set_quantity
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        printing = make_printing(expansion, "Dark-Magician")
        set_quantity(printing, 2, user=superuser)
        resolution.resolve_game(game, log=lambda *a: None)
        printing.refresh_from_db()
        return printing

    def test_catalog_and_resolve_are_single_unit_jobs(self, game, superuser):
        from scraping import services
        from scraping.models import ScrapeJob
        for job_type in (ScrapeJob.Type.IMPORT_CATALOG, ScrapeJob.Type.RESOLVE_PRINTINGS):
            job = services.create_job(game=game, job_type=job_type, params={}, user=superuser)
            assert job.total_items == 0 and not job.uses_browser
            job.delete()

    def test_api_jobs_are_marked_as_not_needing_a_browser(self, game, superuser):
        from scraping.models import ScrapeJob
        api = ScrapeJob(game=game, job_type=ScrapeJob.Type.CACHE_ART)
        scrape = ScrapeJob(game=game, job_type=ScrapeJob.Type.REFRESH_PRICES)
        assert api.uses_browser is False and scrape.uses_browser is True

    def test_price_gap_means_no_usable_number_not_never_touched(self, game, linked, superuser):
        """The legacy import stamped prices_updated_at on every row, so keying
        'missing price' off that timestamp would match nothing at all."""
        from django.utils import timezone

        from scraping import services
        from scraping.models import ScrapeJob

        Printing.objects.filter(pk=linked.pk).update(
            current_price_trend=0, prices_updated_at=timezone.now()
        )
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.API_PRICES,
            params={"scope": "owned", "mode": "gaps"}, user=superuser,
        )
        assert job.total_items == 1  # zero counts as missing

    def test_a_real_price_is_not_a_gap(self, game, linked, superuser):
        from decimal import Decimal

        from scraping import services
        from scraping.models import ScrapeJob

        Printing.objects.filter(pk=linked.pk).update(
            current_price_trend=Decimal("12.00"), price_source=Printing.PriceSource.SCRAPE
        )
        with pytest.raises(services.JobError, match="Nothing to do"):
            services.create_job(
                game=game, job_type=ScrapeJob.Type.API_PRICES,
                params={"scope": "owned", "mode": "gaps"}, user=superuser,
            )

    def test_catalog_pricing_is_tagged_and_snapshotted(self, game, linked, superuser, monkeypatch):
        from decimal import Decimal

        from pricing.models import PriceSnapshot
        from scraping import runner, services
        from scraping.models import ScrapeJob

        Printing.objects.filter(pk=linked.pk).update(current_price_trend=None)
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.API_PRICES,
            params={"scope": "owned", "mode": "gaps"}, user=superuser,
        )
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)
        runner.run_job(job.pk)

        linked.refresh_from_db()
        assert linked.current_price_trend == Decimal("0.29")
        assert linked.price_source == Printing.PriceSource.CATALOG
        snapshot = PriceSnapshot.objects.get(card=linked, scrape_job=job)
        assert snapshot.source == PriceSnapshot.Source.CATALOG_API
        # A per-card estimate must never masquerade as a scraped From/30d price.
        assert snapshot.price_from is None and snapshot.price_30d_avg is None
        assert linked.current_price_from is None

    def test_catalog_pricing_refuses_to_clobber_a_scraped_price(
        self, game, linked, superuser, monkeypatch
    ):
        from decimal import Decimal

        from scraping import runner, services
        from scraping.models import ScrapeJob, ScrapeJobItem

        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.API_PRICES,
            params={"scope": "owned", "mode": "all"}, user=superuser,
        )
        # Someone scrapes a real price after the job was queued.
        Printing.objects.filter(pk=linked.pk).update(
            current_price_trend=Decimal("50.00"), price_source=Printing.PriceSource.SCRAPE
        )
        job.params["mode"] = "gaps"
        job.save(update_fields=["params"])
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)
        runner.run_job(job.pk)

        linked.refresh_from_db()
        assert linked.current_price_trend == Decimal("50.00")
        assert job.items.get().status == ScrapeJobItem.Status.SKIPPED

    def test_art_job_only_queues_cards_that_have_art(self, game, expansion, linked, superuser):
        from scraping import services
        from scraping.models import ScrapeJob

        artless = make_printing(expansion, "Artless-Card")
        piece = CardPiece.objects.create(
            game=game, external_id="999", name="Artless", normalized_name="artless"
        )
        Printing.objects.filter(pk=artless.pk).update(piece=piece)
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.CACHE_ART,
            params={"scope": "all", "sizes": ["small"]}, user=superuser,
        )
        assert list(job.items.values_list("card_id", flat=True)) == [linked.pk]


class TestFilterTriggers:
    """htmx's `find` returns ONE element, so `change from:find select` only ever
    armed the first dropdown — every filter after it silently did nothing."""

    def test_no_hx_trigger_uses_from_find(self):
        import pathlib
        import re
        for path in pathlib.Path("templates").rglob("*.html"):
            for trigger in re.findall(r'hx-trigger="([^"]*)"', path.read_text()):
                assert "from:find" not in trigger, f"{path}: {trigger}"

    def test_filter_forms_listen_for_change_on_the_form(self):
        import pathlib
        for name in ("card_list", "expansion_list", "expansion_detail"):
            text = pathlib.Path(f"templates/catalog/{name}.html").read_text()
            assert 'hx-trigger="change, input changed delay:300ms"' in text, name


@pytest.mark.django_db
class TestFilterLabels:
    def test_each_dropdown_is_labelled_and_options_are_bare_values(
        self, client, superuser, game, expansion, stub_fetch
    ):
        import re

        from collection.services import set_quantity
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        printing = make_printing(expansion, "Dark-Magician")
        set_quantity(printing, 1, user=superuser)
        resolution.resolve_game(game, log=lambda *a: None)

        client.force_login(superuser)
        body = client.get(f"/g/{game.code}/cards/").text
        assert '<span class="filter-label">Race</span>' in body
        assert '<option value="">Any</option>' in body
        # The facet name lives in the label, never repeated inside the options.
        assert "Race:" not in body
        option = re.search(r'<option value="Spellcaster"[^>]*>([^<]*)</option>', body)
        assert option and option.group(1) == "Spellcaster"


@pytest.mark.django_db
class TestJobPickers:
    """Job forms let you search what you already have instead of typing a slug
    from memory — a guess that fails silently is not an input control."""

    @pytest.fixture
    def staff(self, client, superuser):
        client.force_login(superuser)
        return client

    @pytest.fixture
    def owned_card(self, expansion, superuser):
        from collection.services import set_quantity
        printing = make_printing(expansion, "Dark-Magician")
        set_quantity(printing, 3, user=superuser)
        return printing

    def test_expansion_search_finds_your_sets(self, staff, game, expansion):
        r = staff.get("/scrape/find/expansions/", {"game": game.code, "q": expansion.slug[:4]})
        assert f'value="{expansion.slug}"' in r.text
        assert expansion.display_name in r.text

    def test_card_search_returns_a_pickable_id(self, staff, game, owned_card):
        r = staff.get("/scrape/find/cards/", {"game": game.code, "q": "dark"})
        assert f'name="card_id" value="{owned_card.pk}"' in r.text
        assert "3 owned" in r.text

    def test_search_needs_a_game_and_two_letters(self, staff, game):
        assert "Pick a game first" in staff.get("/scrape/find/cards/", {"q": "dark"}).text
        assert "at least 2 letters" in staff.get(
            "/scrape/find/cards/", {"game": game.code, "q": "d"}
        ).text

    def test_pickers_are_staff_only(self, client, django_user_model):
        client.force_login(django_user_model.objects.create_user("plain", password="pw"))
        assert client.get("/scrape/find/cards/").status_code in (302, 403)

    def test_single_card_scope_queues_exactly_that_card(self, staff, game, owned_card):
        from scraping.models import ScrapeJob
        r = staff.post("/scrape/jobs/", {
            "job_type": "refresh_prices", "game": game.code,
            "scope": "card", "card_id": str(owned_card.pk),
        })
        assert r.status_code == 302
        job = ScrapeJob.objects.latest("pk")
        assert job.total_items == 1
        assert job.items.get().card_id == owned_card.pk

    def test_single_card_scope_without_a_card_says_so(self, staff, game, owned_card):
        r = staff.post("/scrape/jobs/", {
            "job_type": "refresh_prices", "game": game.code, "scope": "card", "card_id": "",
        })
        assert "Pick a card first" in r.text

    def test_a_card_from_another_game_is_rejected(self, staff, game, owned_card, digimon_game):
        r = staff.post("/scrape/jobs/", {
            "job_type": "refresh_prices", "game": digimon_game.code,
            "scope": "card", "card_id": str(owned_card.pk),
        })
        assert "Pick a card first" in r.text

    def test_a_non_numeric_card_id_cannot_crash_the_form(self, staff, game, owned_card):
        r = staff.post("/scrape/jobs/", {
            "job_type": "refresh_prices", "game": game.code,
            "scope": "card", "card_id": "'; DROP TABLE--",
        })
        assert r.status_code == 200 and "Pick a card first" in r.text


class TestPickerWiring:
    """htmx sends an input's value only if it has a name — a nameless search box
    fires requests that carry no query and quietly return nothing. This bit the
    bulk-quantity field in Phase 2; the same shape must not come back."""

    def _panel(self):
        import pathlib
        return pathlib.Path("templates/scraping/panel.html").read_text()

    def test_every_search_input_has_a_name(self):
        import re
        for tag in re.findall(r"<input[^>]*type=\"search\"[^>]*>", self._panel()):
            assert 'name="' in tag, tag

    def test_searches_include_one_specific_game_select(self):
        """'closest form' swept up BOTH q inputs in the price form, so each
        search overwrote the other's query."""
        body = self._panel()
        assert 'hx-include="closest form"' not in body
        assert body.count('hx-include="#rp-game"') == 2
        assert body.count('hx-include="#dc-game"') == 1

    def test_referenced_game_selects_exist_and_are_unique(self):
        body = self._panel()
        for ident in ("rp-game", "dc-game"):
            assert body.count(f'id="{ident}"') == 1


@pytest.mark.django_db
class TestPickerQueries:
    def test_search_returns_matches_for_a_real_query(self, client, superuser, game, expansion):
        from collection.services import set_quantity
        printing = make_printing(expansion, "Exodia-the-Forbidden-One")
        set_quantity(printing, 1, user=superuser)
        client.force_login(superuser)

        r = client.get("/scrape/find/cards/", {"game": game.code, "q": "exodia"})
        assert printing.display_name in r.text
        r = client.get("/scrape/find/expansions/", {"game": game.code, "q": expansion.slug[:4]})
        assert expansion.display_name in r.text

    def test_a_blank_query_renders_nothing_rather_than_everything(
        self, client, superuser, game, expansion
    ):
        client.force_login(superuser)
        r = client.get("/scrape/find/expansions/", {"game": game.code, "q": ""})
        assert "picker-option" not in r.text


@pytest.mark.django_db
class TestResolutionReporting:
    """A retry run reports on the retry batch; a human needs the collection-wide
    number too, or '0.0% resolved' reads like everything came unlinked."""

    @pytest.fixture
    def mostly_linked(self, game, expansion, superuser, stub_fetch):
        catalogs.import_ygoprodeck(game, log=lambda *a: None)
        make_printing(expansion, "Dark-Magician")
        make_printing(expansion, "Blue-Eyes-White-Dragon")
        make_printing(expansion, "Albaz-the-Shrouded")   # OCG-only: never matches
        resolution.resolve_game(game, log=lambda *a: None)
        return game

    def test_game_stats_describe_the_collection_not_the_batch(self, mostly_linked):
        stats = resolution.game_stats(mostly_linked)
        assert (stats["linked"], stats["unmatched"], stats["eligible"]) == (2, 1, 3)
        assert round(stats["rate"]) == 67

    def test_a_retry_that_matches_nothing_leaves_the_collection_intact(self, mostly_linked):
        before = resolution.game_stats(mostly_linked)
        report = resolution.resolve_game(mostly_linked, log=lambda *a: None, redo=True)
        after = resolution.game_stats(mostly_linked)

        assert report.total == 1 and report.resolved == 0  # only the leftover retried
        assert report.rate == 0.0                          # ...of the batch
        assert after == before                             # collection unchanged

    def test_no_catalog_entry_is_not_a_job_failure(
        self, mostly_linked, superuser, monkeypatch
    ):
        """A Speed Duel Skill or an OCG-only card has no catalog card to match.
        Counting that as failure made a clean retry read '234 done, 234 failed'."""
        from scraping import runner, services
        from scraping.models import ScrapeJob

        job = services.create_job(
            game=mostly_linked, job_type=ScrapeJob.Type.RESOLVE_PRINTINGS,
            params={"redo": True}, user=superuser,
        )
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED
        assert job.failed_items == 0

    def test_the_job_log_states_both_numbers(self, mostly_linked, superuser, monkeypatch, tmp_path):
        from scraping import runner, services
        from scraping.models import ScrapeJob

        job = services.create_job(
            game=mostly_linked, job_type=ScrapeJob.Type.RESOLVE_PRINTINGS,
            params={"redo": True}, user=superuser,
        )
        job.log_path = str(tmp_path / "job.log")
        job.save(update_fields=["log_path"])
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)
        runner.run_job(job.pk)

        log = (tmp_path / "job.log").read_text()
        assert "newly linked out of" in log      # what this run did
        assert "overall:" in log                 # where the collection stands
        assert "no catalog match" in log
        assert "OCG-only" in log                 # why the leftovers never match


class TestModalsAreRealModals:
    """A <dialog open> lays out in normal document flow — which is why quick-add
    appeared at the bottom of the page. showModal() puts it in the top layer."""

    def _partials(self):
        import pathlib
        return {
            p.name: p.read_text()
            for p in pathlib.Path("templates/collection/partials").glob("*.html")
            if "<dialog" in p.read_text()
        }

    def test_no_dialog_is_pre_opened_in_markup(self):
        for name, text in self._partials().items():
            assert "<dialog open" not in text, name

    def test_javascript_promotes_them(self):
        import pathlib
        js = pathlib.Path("static/app.js").read_text()
        assert "showModal()" in js

    def test_closing_clears_the_host_so_reopening_works(self):
        import pathlib
        js = pathlib.Path("static/app.js").read_text()
        assert 'addEventListener("close"' in js and 'host.innerHTML = ""' in js

    def test_close_buttons_close_the_dialog(self):
        for name, text in self._partials().items():
            assert "innerHTML=''" not in text, name


class TestLayoutRegressions:
    """The .card-detail grid was silently dropped in a CSS cleanup, so full art
    expanded to the width of the page."""

    def _css(self):
        import pathlib
        return pathlib.Path("static/app.css").read_text()

    def test_card_art_column_is_capped(self):
        css = self._css()
        assert ".card-detail {" in css
        assert "minmax(0, 260px)" in css

    def test_the_page_is_not_capped_but_prose_is(self):
        css = self._css()
        assert "max-width: 1500px" not in css       # wide monitors get used
        assert ".subtitle, .hint" in css and "78ch" in css


class TestAdminThemeStaysCosmetic:
    """The admin index puts <h1>, #content-main and #content-related inside
    #content. Giving #content a grid auto-placed all three into cells and broke
    the page. The theme re-colours; it does not lay out."""

    def _css(self):
        import pathlib
        return pathlib.Path("static/admin-theme.css").read_text()

    def test_content_is_never_given_a_layout_mode(self):
        import re
        css = self._css()
        for match in re.finditer(r"#content\s*\{([^}]*)\}", css):
            body = match.group(1)
            for prop in ("display:", "grid-template", "flex-direction", "float:"):
                assert prop not in body, f"#content must not set {prop}"

    def test_only_content_main_is_arranged(self):
        css = self._css()
        assert ".dashboard #content-main {" in css
        assert ".dashboard #content {" not in css

    def test_the_admin_is_not_called_django_admin(self):
        import pathlib
        nav = pathlib.Path("templates/base.html").read_text()
        assert "Django admin" not in nav
        assert "Records" in nav


@pytest.mark.django_db
class TestAddingAGameNeedsNoCode:
    """Adding Riftbound in the admin used to raise 'No catalog provider is
    configured for Riftbound' from a hard-coded dict keyed by game code."""

    @pytest.fixture
    def riftbound(self, db):
        from catalog.models import Game
        return Game.objects.create(code="riftbound", name="Riftbound",
                                   cardmarket_segment="Riftbound")

    def test_a_new_game_simply_has_no_catalog(self, riftbound):
        from catalog.catalogs import importer_for
        assert riftbound.has_catalog is False
        assert importer_for(riftbound) is None

    def test_the_panel_does_not_offer_jobs_it_cannot_run(self, client, superuser, riftbound):
        client.force_login(superuser)
        body = client.get("/scrape/").text
        assert "No card catalog is configured for" in body and "Riftbound" in body

    def test_the_job_explains_instead_of_crashing(self, riftbound, superuser, monkeypatch):
        from scraping import runner, services
        from scraping.models import ScrapeJob
        job = services.create_job(game=riftbound, job_type=ScrapeJob.Type.IMPORT_CATALOG,
                                  params={}, user=superuser)
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert "no card catalog configured" in job.error
        assert "no code needed" in job.error.lower()

    def test_a_custom_json_api_needs_no_code(self, riftbound, monkeypatch):
        """The whole point: a URL and a field map, entered in the admin."""
        from catalog import catalogs
        riftbound.catalog_provider = "custom"
        riftbound.catalog_config = {
            "url": "https://api.riftbound.test/cards",
            "results_path": "results",
            "fields": {
                "external_id": "code", "name": "title", "card_type": "kind",
                "colour": "domain", "text": "rules", "image_url": "art.full",
            },
        }
        riftbound.save()

        payload = {"results": [
            {"code": "OGN-001", "title": "Ashe", "kind": "Champion", "domain": "Fury",
             "rules": "Deals 2 damage.", "art": {"full": "https://api.riftbound.test/a.jpg"}},
            {"code": "OGN-002", "title": "Yasuo", "kind": "Champion", "domain": "Body",
             "rules": "", "art": {"full": "https://api.riftbound.test/b.jpg"}},
        ]}
        monkeypatch.setattr(catalogs, "_fetch_json", lambda url, params=None: payload)

        result = catalogs.importer_for(riftbound)(riftbound, log=lambda *a: None)
        assert result.created == 2
        ashe = CardPiece.objects.get(external_id="OGN-001")
        assert (ashe.name, ashe.card_type, ashe.colour) == ("Ashe", "Champion", "Fury")
        assert ashe.image_url == "https://api.riftbound.test/a.jpg"
        assert ashe.normalized_name == "ashe"

    def test_a_custom_provider_without_a_url_says_so(self, riftbound):
        from catalog.catalogs import CatalogError, importer_for
        riftbound.catalog_provider = "custom"
        riftbound.save()
        with pytest.raises(CatalogError, match="no URL is configured"):
            importer_for(riftbound)(riftbound, log=lambda *a: None)

    def test_required_field_mappings_are_checked(self, riftbound, monkeypatch):
        from catalog.catalogs import CatalogError, importer_for
        riftbound.catalog_provider = "custom"
        riftbound.catalog_config = {"url": "https://x/y", "fields": {"name": "title"}}
        riftbound.save()
        with pytest.raises(CatalogError, match="external_id"):
            importer_for(riftbound)(riftbound, log=lambda *a: None)

    def test_the_art_allowlist_follows_the_configured_provider(self, riftbound):
        """A custom provider's image host must be fetchable — but only because
        the game's own config names it, never because a row claims it."""
        riftbound.catalog_provider = "custom"
        riftbound.catalog_config = {"url": "https://api.riftbound.test/cards"}
        riftbound.save()
        hosts = images.allowed_hosts()
        assert "api.riftbound.test" in hosts
        assert "images.ygoprodeck.com" in hosts      # built-ins survive
        assert "evil.example.com" not in hosts
